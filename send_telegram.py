"""Envia a previsão de máxima das estações configuradas para o Telegram.

Uso local:
    set TELEGRAM_TOKEN=123:abc      (Windows: use "set"; Linux/CI: export)
    set TELEGRAM_CHAT_ID=987654321
    python send_telegram.py [--station SBGR ...]

Na nuvem roda pelo GitHub Actions (.github/workflows/main.yml), com o
token e o chat_id guardados como *secrets* do repositório.

Estrutura do envio:
  1. Resumo geral das posições da Polymarket (todas as cidades) + probabilidade
     de cada uma dar certo — só quando alguma estação tem novidade no
     observado; rodada parada não repete o resumo.
  2. Sinais, uma mensagem POR CIDADE: faixas do dia operável em que projetado e
     mercado divergem ≥ EDGE_ALERT_MIN e o lado indicado tem mais de
     EDGE_MIN_CONFIDENCE de chance de acertar (avisado uma vez por faixa).
  3. Um bloco por estação: posições daquela cidade, tabela mercado × projetado
     e o gráfico (nowcast + distribuições) com o hora a hora.

Quando a máxima de hoje já travou (TMAX_LOCK_HOURS), o D0 sai de cena na
estação: sinais, tabela, distribuição e hora a hora focam no dia seguinte.

Estações sem novidade desde o último envio (mesmo observado e mesma projeção,
comparados via data/digest_state.json) são omitidas do digest.
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
import html
import json
import os
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tmax import calibration, config, distribution, notify, pipeline, polymarket


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

    def yes_prob(title: str | None, end_iso) -> float | None:
        """Nossa P(o Yes acontecer) para um mercado de máxima, pela previsão
        atual (última observação + ensemble corrigido). None se o mercado não
        casa com uma estação/dia que temos."""
        ev = polymarket.parse_temp_market(title)
        if not ev:
            return None
        ctx = contexts.get(ev["icao"])
        if not ctx:
            return None
        try:
            end_date = dt.date.fromisoformat(str(end_iso or "")[:10])
        except ValueError:
            return None
        dist = (ctx["dist_d0"] if end_date == ctx["d0"]
                else ctx["dist_d1"] if end_date == ctx["d1"] else None)
        p = distribution.market_prob(dist, ev["threshold"], ev["mode"])
        # Probabilidade CALIBRADA (curva empírica do backtest): D0 usa o
        # período do dia local; D+1 usa o período menos informado, porque a
        # projeção de amanhã sabe ainda menos que a madrugada de hoje.
        hour = ctx["now"].hour if end_date == ctx["d0"] else None
        return calibration.apply(p, hour)

    def position_success_prob(p: dict) -> float | None:
        """P(a posição dar certo): P(Yes) se apostou Yes, senão 1−P(Yes).
        Usa o posterior (modelo calibrado + preço atual do mercado)."""
        p_yes = yes_prob(p.get("title"), p.get("endDate"))
        if p_yes is None:
            return None
        p_yes = calibration.posterior(p_yes, float(p.get("curPrice") or 0))
        outcome = str(p.get("outcome") or "").strip().lower()
        if outcome == "yes":
            return p_yes
        if outcome == "no":
            return 1.0 - p_yes
        return None

    state = _load_digest_state()
    station_state = state.get("stations", {})

    # Novidade por estação: observado/projeção diferentes do último envio.
    # Estação com contexto quebrado conta como novidade (envia o aviso).
    fps = {s.icao: (_station_fingerprint(contexts[s.icao])
                    if s.icao in contexts else None) for s in stations}
    novidade = {s.icao for s in stations
                if fps[s.icao] is None
                or station_state.get(s.icao) != fps[s.icao]}

    # 2) Bloco geral: posição consolidada na Polymarket (todas as cidades), já
    # com a chance de cada aposta dar certo. Só quando alguma estação tem
    # novidade no observado — rodada parada não repete o resumo. Uma falha
    # aqui não impede o resto.
    positions: list[dict] = []
    wallet = os.environ.get("POLYMARKET_WALLET")
    if wallet and not args.no_positions and novidade:
        try:
            positions = polymarket.fetch_positions(wallet)
            notify.send_message(
                token, chat_id,
                polymarket.positions_message(positions, position_success_prob))
            print("[polymarket] posições enviadas.")
        except Exception as exc:  # noqa: BLE001 — leitura da carteira é acessório
            print(f"[polymarket] ERRO ao ler posições: {exc}", file=sys.stderr)

    # 3) Sinais, uma mensagem por cidade: faixas do dia operável (D0, ou D+1
    # quando a máxima de hoje travou) em que projetado e mercado divergem o
    # suficiente E o lado indicado tem confiança alta. Cada faixa avisa UMA
    # vez ao cruzar o corte (o estado guarda as que já estão acima; a data na
    # chave re-arma tudo na virada do dia). O estado também guarda o projetado
    # de TODAS as faixas da rodada anterior, para mostrar de onde veio.
    signal_rows = _collect_signal_rows(stations, contexts, yes_prob)
    prev_probs = state.get("signal_probs", {})
    edges_now = {k: v for k, v in signal_rows.items() if _is_edge(v)}
    prev_edges = state.get("edges", {})
    new_edges = {k: v for k, v in edges_now.items() if k not in prev_edges}
    for icao, text in _edges_messages(new_edges, prev_probs):
        try:
            notify.send_message(token, chat_id, text)
            print(f"[sinais] {icao}: sinal(is) enviado(s).")
        except Exception as exc:  # noqa: BLE001 — sinal é acessório
            print(f"[sinais] {icao}: ERRO ao enviar: {exc}", file=sys.stderr)
            # não marca as faixas dessa cidade como avisadas; tenta na próxima
            for k in [k for k, v in new_edges.items() if v["icao"] == icao]:
                edges_now.pop(k, None)

    # 4) Um bloco por estação: divisor, posições da cidade, tabela mercado ×
    # projetado e, por fim, gráfico (nowcast) + hora a hora. Falha de uma
    # parte/estação não derruba o resto. Estação sem novidade (mesma assinatura
    # do último envio bem-sucedido) é omitida.
    for station in stations:
        ctx = contexts.get(station.icao)
        fp = fps[station.icao]
        if station.icao not in novidade:
            print(f"[{station.icao}] sem novidade na projeção; bloco omitido.")
            continue
        try:
            _send_station_block(token, chat_id, station, ctx, positions,
                                errors, yes_prob, position_success_prob)
        except Exception as exc:  # noqa: BLE001 — falha de envio de uma estação não derruba as demais
            errors[station.icao] = str(exc)
            print(f"[{station.icao}] ERRO no bloco: {exc}", file=sys.stderr)
        else:
            if fp is not None:
                station_state[station.icao] = fp
    _save_digest_state({"stations": station_state, "edges": edges_now,
                        "signal_probs": signal_rows})

    return 1 if len(errors) == len(stations) else 0


def _collect_signal_rows(stations, contexts, yes_prob) -> dict:
    """Todas as faixas do dia operável de cada estação (D0; D+1 quando a
    máxima de hoje já travou), com preço e projeção, indexadas por
    "icao:data:faixa" (a data na chave re-arma os sinais na virada do dia).
    Cada valor: {icao, day_label, label, yes, mp}. Normalizado via JSON para
    comparar com o estado salvo em disco."""
    rows: dict = {}
    for station in stations:
        ctx = contexts.get(station.icao)
        if ctx is None:
            continue
        if ctx["tmax_locked"]:
            day, day_label = ctx["d1"], "amanhã"
            hour = None  # D+1: período menos informado na calibração
        else:
            day, day_label = ctx["d0"], "hoje"
            hour = ctx["now"].hour
        slug = polymarket.event_slug(station.icao, day)
        if not slug:
            continue
        try:
            event = polymarket.fetch_event(slug)
        except Exception as exc:  # noqa: BLE001 — sinal é acessório
            print(f"[sinais] ERRO evento {slug}: {exc}", file=sys.stderr)
            continue
        for r in polymarket.odds_rows(event, yes_prob):
            if r["yes"] is None or r["mp"] is None:
                continue
            key = f"{station.icao}:{day.isoformat()}:{r['label']}"
            rows[key] = {"icao": station.icao, "label": r["label"],
                         "day_label": f"{day_label} {day.strftime('%d/%m')}",
                         "yes": r["yes"], "mp": r["mp"],
                         # melhor estimativa: modelo calibrado + preço
                         "post": calibration.posterior(r["mp"], r["yes"],
                                                       hour=hour)}
    return json.loads(json.dumps(rows))


def _is_edge(row: dict) -> bool:
    """Sinal acionável: divergência mínima E o lado indicado (comprar Yes se
    está barato, No se está caro) com mais de EDGE_MIN_CONFIDENCE de chance de
    acertar segundo o POSTERIOR (modelo calibrado + preço do mercado)."""
    post = row.get("post", row["mp"])
    diff = post - row["yes"]
    if abs(diff) < config.EDGE_ALERT_MIN:
        return False
    side_prob = post if diff > 0 else 1.0 - post
    return side_prob > config.EDGE_MIN_CONFIDENCE


def _edges_messages(edges: dict, prev_probs: dict) -> list[tuple[str, str]]:
    """Mensagens de sinais, uma POR CIDADE: [(icao, texto HTML)]. Cada linha é
    uma ação (Comprar SIM ou Comprar NÃO) com as probabilidades DO LADO
    COMPRADO — mercado × modelo — e o modelo da rodada anterior."""
    by_icao: dict[str, list[str]] = {}
    for key, e in edges.items():
        post = e.get("post", e["mp"])
        prev = prev_probs.get(key, {})
        prev_post = prev.get("post")
        if prev_post is None and prev.get("mp") is not None:
            prev_post = calibration.posterior(prev["mp"], prev.get("yes"))
        if post > e["yes"]:              # Yes subvalorizado pelo mercado
            dot, acao = "🟢", "Comprar SIM"
            mkt, mdl = e["yes"], post
            prev_side = prev_post
        else:                            # Yes sobrevalorizado → comprar o Não
            dot, acao = "🔴", "Comprar NÃO"
            mkt, mdl = 1.0 - e["yes"], 1.0 - post
            prev_side = None if prev_post is None else 1.0 - prev_post
        antes = ("antes —" if prev_side is None
                 else f"antes {prev_side * 100:.0f}%")
        by_icao.setdefault(e["icao"], []).append(
            f"{dot} <b>{acao}</b> — <b>{html.escape(e['label'])}</b> "
            f"({e['day_label']}): mercado {mkt * 100:.0f}% × "
            f"modelo {mdl * 100:.0f}% ({antes})")
    out = []
    for icao, lines in by_icao.items():
        st = config.STATIONS[icao]
        head = (f"🚨 <b>Sinais — {html.escape(st.city)}</b> {st.flag} · "
                f"mercado × modelo no lado comprado · edge ≥ "
                f"{config.EDGE_ALERT_MIN * 100:.0f} p.p., modelo &gt; "
                f"{config.EDGE_MIN_CONFIDENCE * 100:.0f}%")
        out.append((icao, "\n".join([head, *lines])))
    return out


def _station_fingerprint(ctx: dict) -> dict:
    """Assinatura do que o digest comunica de uma estação: muda quando o
    observado muda (nova temperatura ou nova máxima) ou quando a projeção muda
    (quantis de hoje/amanhã). Normalizada via JSON para comparar com o estado
    salvo em disco (chaves viram string)."""
    lm = ctx["latest_metar"]
    fp = {
        "obs_max": ctx["obs_max_today"],
        "latest_temp": lm["temp"] if lm else None,
        "q_d0": ctx["dist_d0"]["quantiles"],
        "q_d1": ctx["dist_d1"]["quantiles"],
    }
    return json.loads(json.dumps(fp))


def _load_digest_state() -> dict:
    try:
        return json.loads(config.DIGEST_STATE_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _save_digest_state(state: dict) -> None:
    try:
        config.DIGEST_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        config.DIGEST_STATE_FILE.write_text(json.dumps(state), "utf-8")
    except OSError as exc:
        print(f"AVISO: não salvou o estado do digest ({exc})", file=sys.stderr)


def _send_station_block(token, chat_id, station, ctx, positions,
                        errors, yes_prob, position_success_prob) -> None:
    """Envia o bloco completo de UMA estação (divisor, posições, tabela de
    odds, gráfico e hora a hora). Levanta na primeira falha de envio."""
    notify.send_message(token, chat_id, notify.station_divider(station))

    # 4a) posições abertas desta cidade, com a chance de dar certo
    if positions:
        msg = polymarket.station_positions_message(
            station, positions, position_success_prob)
        if msg:
            notify.send_message(token, chat_id, msg)

    if ctx is None:
        notify.send_message(
            token, chat_id,
            f"⚠️ Sem dados suficientes agora "
            f"({errors.get(station.icao, '')}).")
        return

    # 4b) tabela: probabilidade real vs. preço do mercado. Com a máxima de
    # hoje travada o mercado do D0 está resolvido — mostra só o de amanhã.
    days = ((("Amanhã", ctx["d1"]),) if ctx["tmax_locked"]
            else (("Hoje", ctx["d0"]), ("Amanhã", ctx["d1"])))
    day_tables: list[tuple] = []
    for day_label, date in days:
        slug = polymarket.event_slug(station.icao, date)
        if not slug:
            continue
        try:
            event = polymarket.fetch_event(slug)
        except Exception as exc:  # noqa: BLE001 — tabela é acessória
            print(f"[polymarket] ERRO evento {slug}: {exc}", file=sys.stderr)
            continue
        rows = polymarket.odds_rows(event, yes_prob)
        if rows:
            day_tables.append((day_label, date, rows))
    if day_tables:
        try:
            notify.send_photo(token, chat_id,
                              notify.odds_table_png(station, day_tables),
                              notify.odds_caption(station))
            print(f"[{station.icao}] tabela de odds enviada "
                  f"({len(day_tables)} dia(s)).")
        except Exception as exc:  # noqa: BLE001 — tabela é acessória
            print(f"[{station.icao}] ERRO ao enviar tabela: {exc}",
                  file=sys.stderr)
    else:
        print(f"[{station.icao}] sem mercado de odds relevante.")

    # 4c) gráfico com nowcast + distribuições e o hora a hora
    notify.send_photo(token, chat_id, notify.station_chart_png(ctx),
                      notify.station_lines(ctx))
    notify.send_message(token, chat_id, notify.station_hourly_lines(ctx))
    print(f"[{station.icao}] enviado.")


if __name__ == "__main__":
    sys.exit(main())
