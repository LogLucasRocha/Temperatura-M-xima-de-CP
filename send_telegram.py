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

from sbgr import config, notify, pipeline


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--station", action="append", choices=sorted(config.STATIONS),
                    help="estação a incluir (repetível; padrão: todas)")
    ap.add_argument("--force-bias", action="store_true",
                    help="recalcula a correção de viés mesmo com cache válido")
    args = ap.parse_args()

    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("ERRO: defina TELEGRAM_TOKEN e TELEGRAM_CHAT_ID no ambiente.",
              file=sys.stderr)
        return 2

    icaos = args.station or list(config.STATIONS)
    stations = [config.STATIONS[i] for i in icaos]

    # Rótulo de horário no fuso da primeira estação (referência).
    now = dt.datetime.now(stations[0].tz)
    notify.send_message(
        token, chat_id, notify.digest_header(now.strftime("%d/%m/%Y %H:%M")))

    failures = 0
    for station in stations:
        def log(msg: str, _s=station) -> None:
            print(f"[{_s.icao}] {msg}")
        try:
            ctx = pipeline.build_context(
                station, force_bias=args.force_bias, log=log)
        except Exception as exc:  # noqa: BLE001 — falha de uma estação não derruba o resto
            failures += 1
            print(f"[{station.icao}] ERRO: {exc}", file=sys.stderr)
            notify.send_message(
                token, chat_id,
                f"{station.flag} <b>{station.city} ({station.icao})</b>\n"
                f"⚠️ Sem dados suficientes agora ({exc}).")
            continue

        notify.send_photo(token, chat_id, notify.station_chart_png(ctx),
                          notify.station_lines(ctx))
        notify.send_message(token, chat_id, notify.station_hourly_lines(ctx))
        print(f"[{station.icao}] enviado.")

    return 1 if failures == len(stations) else 0


if __name__ == "__main__":
    sys.exit(main())
