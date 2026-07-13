"""Envia a previsão de máxima das estações configuradas para o Telegram.

Uso local:
    set TELEGRAM_TOKEN=123:abc      (Windows: use "set"; Linux/CI: export)
    set TELEGRAM_CHAT_ID=987654321
    python send_telegram.py [--station SBGR ...]

Na nuvem roda pelo GitHub Actions (.github/workflows/main.yml), com o
token e o chat_id guardados como *secrets* do repositório.

Estrutura do envio (foco exclusivo no D0 — o D+1 não aparece no digest):
  1. Resumo geral das posições da Polymarket (todas as cidades) + probabilidade
     de cada uma dar certo — só quando alguma estação tem novidade no
     observado; rodada parada não repete o resumo.
  2. Sinais, uma mensagem POR CIDADE: faixas de HOJE em que a Probabilidade
     Real (modelo calibrado, a mesma da tabela) e o mercado divergem ≥
     EDGE_ALERT_MIN e o lado indicado tem mais de EDGE_MIN_CONFIDENCE de
     chance (avisado uma vez por faixa). Só cruzamentos novos dentro da
     janela local SIGNAL_HOURS; edge de madrugada é consumido em silêncio.
  3. Um bloco por estação: posições daquela cidade, tabela mercado × projetado
     de hoje e o gráfico (nowcast + distribuição de hoje) com o hora a hora.

Quando a máxima de hoje já travou (TMAX_LOCK_HOURS), o mercado do dia está
resolvido: sinais e tabela somem e o hora a hora corta as horas restantes.

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
import concurrent.futures as cf
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

    def _build(station):
        def log(msg: str, _s=station) -> None:
            print(f"[{_s.icao}] {msg}")
        return pipeline.build_context(station, force_bias=args.force_bias,
                                      log=log)

    # Paralelo: com ~25 estações, sequencial estouraria o timeout do job.
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_build, s): s for s in stations}
        for fut in cf.as_completed(futs):
            s = futs[fut]
            try:
                contexts[s.icao] = fut.result()
            except Exception as exc:  # noqa: BLE001 — falha de uma estação não derruba o resto
                errors[s.icao] = str(exc)
                print(f"[{s.icao}] ERRO: {exc}", file=sys.stderr)

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
        p = distribution.market_prob(dist, ev["lo"], ev["hi"], ev["mode"],
                                     ev["unit"])
        # Probabilidade CALIBRADA (curva empírica do backtest): D0 usa o
        # período do dia local; D+1 usa o período menos informado, porque a
        # projeção de amanhã sabe ainda menos que a madrugada de hoje.
        hour = ctx["now"].hour if end_date == ctx["d0"] else None
        return calibration.apply(p, hour)

    def position_success_prob(p: dict) -> float | None:
        """P(a posição dar certo): P(Yes) se apostou Yes, senão 1−P(Yes) —
        a mesma Probabilidade Real (modelo calibrado) da tabela de odds."""
        p_yes = yes_prob(p.get("title"), p.get("endDate"))
        if p_yes is None:
            return None
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

    # Sinais são computados cedo porque também decidem o que mais é enviado:
    # um sinal NOVO (não estava na rodada anterior) puxa junto o resumo de
    # posições e o bloco completo da cidade; as repetições vêm sozinhas.
    signal_rows = _collect_signal_rows(stations, contexts, yes_prob)
    prev_probs = state.get("signal_probs", {})
    edges_now = {k: v for k, v in signal_rows.items() if _is_edge(v)}
    new_edges = {k: v for k, v in edges_now.items()
                 if _in_signal_window(v["icao"])}
    prev_edges = state.get("edges", {})
    fresh_icaos = {v["icao"] for k, v in new_edges.items()
                   if k not in prev_edges}

    # 2) Posições: buscadas TODA rodada (o stop loss precisa do preço atual);
    # o resumo geral é enviado quando há novidade no observado OU quando um
    # sinal novo apareceu (contexto para decidir a entrada).
    positions: list[dict] = []
    wallet = os.environ.get("POLYMARKET_WALLET")
    if wallet and not args.no_positions:
        try:
            positions = polymarket.fetch_positions(wallet)
        except Exception as exc:  # noqa: BLE001 — leitura da carteira é acessório
            print(f"[polymarket] ERRO ao ler posições: {exc}", file=sys.stderr)
        if positions and (novidade or fresh_icaos):
            try:
                notify.send_message(
                    token, chat_id,
                    polymarket.positions_message(positions,
                                                 position_success_prob))
                print("[polymarket] posições enviadas.")
            except Exception as exc:  # noqa: BLE001
                print(f"[polymarket] ERRO no resumo: {exc}", file=sys.stderr)

    # 2b) Stop loss: mercado precificando a posição STOP_ALERT_FRAC (ou mais)
    # abaixo da entrada → alerta em TODA rodada enquanto persistir (pedido
    # explícito: não parar de mandar até sumir).
    stop_msg = _stop_alerts(positions)
    if stop_msg:
        try:
            notify.send_message(token, chat_id, stop_msg)
            print("[stop] alerta de stop loss enviado.")
        except Exception as exc:  # noqa: BLE001
            print(f"[stop] ERRO: {exc}", file=sys.stderr)

    # 2c) Alertas de condição observada — platô de 2h e fuga do envelope do
    # ensemble. Computados aqui; a ENTREGA é junto do bloco da cidade (não
    # separada). Cada episódio avisa uma vez (chave no estado; re-arma quando
    # a condição muda).
    cond_state = state.get("cond_alerts", {})
    cond_pending: dict[str, list] = {}
    for station in stations:
        ctx = contexts.get(station.icao)
        if ctx is None:
            continue
        for kind, res in (("flat", _flat_alert(ctx)),
                          ("ens", _ens_escape_alert(ctx))):
            skey = f"{station.icao}:{kind}"
            if res is None:
                cond_state.pop(skey, None)
                continue
            key, texto = res
            if cond_state.get(skey) == key:
                continue  # mesmo episódio já avisado
            cond_pending.setdefault(station.icao, []).append(
                (skey, key, texto))
    alert_icaos = set(cond_pending)

    # 3) Sinais: as mensagens são entregues DENTRO do bloco da cidade quando
    # há bloco nesta rodada; repetição de sinal em cidade sem bloco sai
    # sozinha (sem re-enviar gráficos).
    sig_msgs = dict(_edges_messages(new_edges, prev_probs))

    # 4) Um bloco por estação: divisor, posições da cidade, tabela mercado ×
    # projetado e, por fim, gráfico (nowcast) + hora a hora. Falha de uma
    # parte/estação não derruba o resto. Estação sem novidade (mesma assinatura
    # do último envio bem-sucedido) é omitida.
    # Bloco completo (tabela + gráfico + hora a hora) vai para: cidades com
    # POSIÇÃO aberta (quando há novidade no observado) e cidades com sinal
    # NOVO nesta rodada (ignorando novidade — o gráfico é o contexto da
    # decisão de entrada). Repetições de sinal não re-enviam o bloco.
    pos_icaos = set()
    for p in positions:
        if p.get("redeemable"):
            continue
        pm = polymarket.parse_temp_market(p.get("title"))
        if pm:
            pos_icaos.add(pm["icao"])

    for station in stations:
        icao = station.icao
        ctx = contexts.get(icao)
        fp = fps[icao]
        fresh_signal = icao in fresh_icaos or icao in alert_icaos
        has_block = not (
            (icao not in novidade and not fresh_signal)
            or (config.FULL_BLOCK_ONLY_WITH_ACTIVITY
                and icao not in pos_icaos and not fresh_signal))
        if not has_block:
            # repetição de sinal em cidade sem bloco: só a mensagem do sinal
            if icao in sig_msgs:
                try:
                    notify.send_message(token, chat_id, sig_msgs[icao])
                    print(f"[sinais] {icao}: sinal (repetição) enviado.")
                except Exception as exc:  # noqa: BLE001
                    print(f"[sinais] {icao}: ERRO: {exc}", file=sys.stderr)
            continue
        # sinal + alertas de condição entram no TOPO do bloco da cidade
        pre = ([sig_msgs[icao]] if icao in sig_msgs else [])
        pre += [texto for _sk, _k, texto in cond_pending.get(icao, [])]
        try:
            _send_station_block(token, chat_id, station, ctx, positions,
                                errors, yes_prob, position_success_prob,
                                pre_msgs=pre)
        except Exception as exc:  # noqa: BLE001 — falha de envio de uma estação não derruba as demais
            errors[icao] = str(exc)
            print(f"[{icao}] ERRO no bloco: {exc}", file=sys.stderr)
        else:
            if fp is not None:
                station_state[icao] = fp
            for skey, key, _texto in cond_pending.get(icao, []):
                cond_state[skey] = key
    _save_digest_state({"stations": station_state, "edges": edges_now,
                        "signal_probs": signal_rows,
                        "cond_alerts": cond_state})

    return 1 if len(errors) == len(stations) else 0


def _collect_signal_rows(stations, contexts, yes_prob) -> dict:
    """Todas as faixas do D0 de cada estação (nada quando a máxima de hoje já
    travou — mercado resolvido), com preço e projeção, indexadas por
    "icao:data:faixa" (a data na chave re-arma os sinais na virada do dia).
    Cada valor: {icao, day_label, label, yes, mp}. Normalizado via JSON para
    comparar com o estado salvo em disco."""
    rows: dict = {}
    for station in stations:
        ctx = contexts.get(station.icao)
        if ctx is None or ctx["tmax_locked"]:
            continue
        day = ctx["d0"]
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
                         "day_label": f"hoje {day.strftime('%d/%m')}",
                         "yes": r["yes"], "mp": r["mp"]}
    return json.loads(json.dumps(rows))


def _stop_alerts(positions: list) -> str | None:
    """Mensagem de stop loss (ou None): posições abertas cujo preço atual está
    STOP_ALERT_FRAC (ou mais) abaixo do preço médio de entrada."""
    linhas = []
    for p in positions:
        if p.get("redeemable"):
            continue
        try:
            avg = float(p.get("avgPrice") or 0)
            cur = float(p.get("curPrice") or 0)
            val = float(p.get("currentValue") or 0)
        except (TypeError, ValueError):
            continue
        if avg <= 0 or val < polymarket.DUST_USD:
            continue
        dd = 1.0 - cur / avg
        if dd < config.STOP_ALERT_FRAC:
            continue
        linhas.append(
            f"• {html.escape(str(p.get('title', '?'))[:80])} — "
            f"<b>{html.escape(str(p.get('outcome', '?')))}</b>: entrada "
            f"${avg:.3f} → agora ${cur:.3f} (<b>−{dd * 100:.0f}%</b>)")
    if not linhas:
        return None
    return (f"🛑 <b>STOP LOSS</b> — mercado ≥ {config.STOP_ALERT_FRAC:.0%} "
            "abaixo da sua entrada\n" + "\n".join(linhas) +
            f"\n<i>referência de saída usada no backtest: "
            f"−{config.STOP_EXIT_FRAC:.0%}</i>")


def _flat_alert(ctx) -> tuple[str, str] | None:
    """(chave, texto) se o observado está de lado — mesma temperatura — há
    pelo menos 2 horas. A chave (dia+temp+início) evita repetir o aviso do
    mesmo platô; um platô novo re-arma."""
    obs = ctx["obs_today"]
    if len(obs) < 3:
        return None
    last = obs[-1]
    start = last["time"]
    for o in reversed(obs):
        if o["temp"] != last["temp"]:
            break
        start = o["time"]
    horas = (last["time"] - start).total_seconds() / 3600.0
    if horas < 2.0:
        return None
    s = ctx["station"]
    key = f"{ctx['d0'].isoformat()}:{last['temp']:g}:{start:%H%M}"
    texto = (f"⏸️ <b>{html.escape(s.city)} ({s.icao})</b>: observado de lado "
             f"em <b>{last['temp']:.0f} °C</b> há {horas:.1f}h "
             f"(desde {start:%H:%M}) · máx. do dia "
             f"{ctx['obs_max_today']:.0f} °C.")
    return key, texto


def _ens_escape_alert(ctx) -> tuple[str, str] | None:
    """(chave, texto) se a última observação saiu do envelope do ensemble
    corrigido (média ± 1 desvio entre os membros na hora correspondente).
    Avisa na entrada (ou troca de lado) e re-arma quando volta para dentro."""
    lm = ctx["latest_metar"]
    if not lm or lm["time"].date() != ctx["d0"]:
        return None
    hour = lm["time"].replace(minute=0, second=0, microsecond=0)
    try:
        i = ctx["ens"]["time"].index(hour)
    except ValueError:
        return None
    vals = []
    for (model, _mid), series in ctx["ens"]["members"].items():
        v = series[i]
        if v is None:
            continue
        fam = config.ENS_MODELS.get(model)
        b = ctx["bias"].get(fam, {}).get("bias", 0.0) if fam else 0.0
        vals.append(v - b)
    if len(vals) < 10:
        return None
    piso, teto = min(vals), max(vals)
    if piso <= lm["temp"] <= teto:
        return None
    acima = lm["temp"] > teto
    lado = "acima do TETO" if acima else "abaixo do PISO"
    s = ctx["station"]
    key = f"{ctx['d0'].isoformat()}:{'acima' if acima else 'abaixo'}"
    texto = (f"{'📈' if acima else '📉'} <b>{html.escape(s.city)} "
             f"({s.icao})</b>: observado <b>{lado} do ensemble</b> — "
             f"{lm['temp']:.0f} °C às {lm['time']:%H:%M} vs envelope "
             f"[{piso:.1f}, {teto:.1f}] °C dos {len(vals)} membros. "
             "Nenhum membro previu isso: a projeção tende a se mover.")
    return key, texto


def _in_signal_window(icao: str) -> bool:
    """Hora local da estação dentro da janela de envio de sinais."""
    h = dt.datetime.now(config.STATIONS[icao].tz).hour
    return config.SIGNAL_HOURS[0] <= h <= config.SIGNAL_HOURS[1]


def _is_edge(row: dict) -> bool:
    """Sinal acionável: divergência mínima E o lado indicado (comprar Yes se
    está barato, No se está caro) com mais de EDGE_MIN_CONFIDENCE de chance
    segundo a Probabilidade Real (modelo calibrado) — o MESMO número exibido
    na tabela de odds, para o alerta e a tabela nunca discordarem."""
    diff = row["mp"] - row["yes"]
    if abs(diff) < config.EDGE_ALERT_MIN:
        return False
    side = "SIM" if diff > 0 else "NAO"
    if side not in config.SIGNAL_SIDES:
        return False
    if side == "NAO" and (1.0 - row["yes"]) < config.NAO_MIN_PRICE:
        return False  # não brigar com mercado quase-certo do Yes
    side_prob = row["mp"] if diff > 0 else 1.0 - row["mp"]
    return side_prob > config.EDGE_MIN_CONFIDENCE


def _edges_messages(edges: dict, prev_probs: dict) -> list[tuple[str, str]]:
    """Mensagens de sinais, uma POR CIDADE: [(icao, texto HTML)]. Cada linha é
    uma ação (Comprar SIM ou Comprar NÃO) com as probabilidades DO LADO
    COMPRADO — mercado × modelo — e o modelo da rodada anterior."""
    by_icao: dict[str, list[str]] = {}
    for key, e in edges.items():
        prev_mp = prev_probs.get(key, {}).get("mp")
        if e["mp"] > e["yes"]:           # Yes subvalorizado pelo mercado
            dot, acao = "🟢", "Comprar SIM"
            mkt, mdl = e["yes"], e["mp"]
            prev_side = prev_mp
        else:                            # Yes sobrevalorizado → comprar o Não
            dot, acao = "🔴", "Comprar NÃO"
            mkt, mdl = 1.0 - e["yes"], 1.0 - e["mp"]
            prev_side = None if prev_mp is None else 1.0 - prev_mp
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
                        errors, yes_prob, position_success_prob,
                        pre_msgs=None) -> None:
    """Envia o bloco completo de UMA estação (divisor, sinais/alertas da
    rodada, posições, tabela de odds, gráfico e hora a hora). Levanta na
    primeira falha de envio."""
    notify.send_message(token, chat_id, notify.station_divider(station))
    for m in pre_msgs or []:
        notify.send_message(token, chat_id, m)

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

    # 4b) tabela: probabilidade real vs. preço do mercado — só o D0; com a
    # máxima travada o mercado de hoje está resolvido e não há o que comparar.
    day_tables: list[tuple] = []
    days = () if ctx["tmax_locked"] else (("Hoje", ctx["d0"]),)
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
