"""Envia a previsão de máxima das estações configuradas para o Telegram.

Uso local:
    set TELEGRAM_TOKEN=123:abc      (Windows: use "set"; Linux/CI: export)
    set TELEGRAM_CHAT_ID=987654321
    python send_telegram.py [--station SBGR ...]

Na nuvem roda pelo GitHub Actions (.github/workflows/telegram.yml), com o
token e o chat_id guardados como *secrets* do repositório.

Um envio = uma mensagem-resumo com as estações + um gráfico (PNG) por estação.
"""
from __future__ import annotations

# Proxy corporativo (máquina Windows local): usa os certificados do sistema.
# Em ambientes sem o proxy (GitHub Actions) o import pode não existir/ser
# desnecessário — por isso é opcional.
try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

import argparse
import datetime as dt
import os
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sbgr import config, distribution, notify, pipeline, polymarket


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--station", action="append", choices=sorted(config.STATIONS),
                    help="estação a incluir (repetível; padrão: todas)")
    ap.add_argument("--force-bias", action="store_true",
                    help="recalcula a correção de viés mesmo com cache válido")
    ap.add_argument("--no-positions", action="store_true",
                    help="não anexa o resumo de posições da Polymarket "
                         "(mesmo com POLYMARKET_WALLET definido)")
    args = ap.parse_args()

    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("ERRO: defina TELEGRAM_TOKEN e TELEGRAM_CHAT_ID no ambiente.",
              file=sys.stderr)
        return 2

    icaos = args.station or list(config.STATIONS)
    stations = [config.STATIONS[i] for i in icaos]

    # 1) Monta o contexto de todas as estações antes de enviar qualquer coisa:
    # as posições (que vão primeiro) dependem das distribuições para estimar a
    # chance de cada aposta dar certo.
    contexts: dict[str, dict] = {}
    errors: dict[str, str] = {}
    for station in stations:
        def log(msg: str, _s=station) -> None:
            print(f"[{_s.icao}] {msg}")
        try:
            contexts[station.icao] = pipeline.build_context(
                station, force_bias=args.force_bias, log=log)
        except Exception as exc:  # noqa: BLE001 — falha de uma estação não derruba o resto
            errors[station.icao] = str(exc)
            print(f"[{station.icao}] ERRO: {exc}", file=sys.stderr)

    def position_success_prob(p: dict) -> float | None:
        """P(a posição dar certo) segundo a previsão atual (última observação +
        ensemble corrigido); None se o mercado não casa com uma estação/dia que
        temos."""
        ev = polymarket.parse_temp_market(p.get("title"))
        if not ev:
            return None
        ctx = contexts.get(ev["icao"])
        if not ctx:
            return None
        try:
            end_date = dt.date.fromisoformat(str(p.get("endDate") or "")[:10])
        except ValueError:
            return None
        dist = (ctx["dist_d0"] if end_date == ctx["d0"]
                else ctx["dist_d1"] if end_date == ctx["d1"] else None)
        p_yes = distribution.market_prob(dist, ev["threshold"], ev["mode"])
        if p_yes is None:
            return None
        outcome = str(p.get("outcome") or "").strip().lower()
        if outcome == "yes":
            return p_yes
        if outcome == "no":
            return 1.0 - p_yes
        return None

    # 2) Posições da Polymarket PRIMEIRO (somente-leitura), já com a chance de
    # cada uma dar certo. Uma falha aqui não impede o envio da previsão.
    wallet = os.environ.get("POLYMARKET_WALLET")
    if wallet and not args.no_positions:
        try:
            positions = polymarket.fetch_positions(wallet)
            notify.send_message(
                token, chat_id,
                polymarket.positions_message(positions, position_success_prob))
            print("[polymarket] posições enviadas.")
        except Exception as exc:  # noqa: BLE001 — leitura da carteira é acessório
            print(f"[polymarket] ERRO ao ler posições: {exc}", file=sys.stderr)

    # 3) Previsão: cabeçalho + gráfico (com nowcast) e hora-a-hora por estação,
    # exatamente como hoje.
    now = dt.datetime.now(stations[0].tz)
    notify.send_message(
        token, chat_id, notify.digest_header(now.strftime("%d/%m/%Y %H:%M")))

    for station in stations:
        ctx = contexts.get(station.icao)
        if ctx is None:
            notify.send_message(
                token, chat_id,
                f"{station.flag} <b>{station.city} ({station.icao})</b>\n"
                f"⚠️ Sem dados suficientes agora ({errors.get(station.icao, '')}).")
            continue
        notify.send_photo(token, chat_id, notify.station_chart_png(ctx),
                          notify.station_lines(ctx))
        notify.send_message(token, chat_id, notify.station_hourly_lines(ctx))
        print(f"[{station.icao}] enviado.")

    return 1 if len(errors) == len(stations) else 0


if __name__ == "__main__":
    sys.exit(main())
