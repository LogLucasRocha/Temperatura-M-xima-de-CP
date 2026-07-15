"""Roda o backtest de sinais: arquiva os dias que faltam, simula a regra de
produção sobre o arquivo inteiro e (opcionalmente) manda o relatório pro
Telegram.

Uso local:
    python run_backtest.py [--no-harvest] [--no-telegram]

Na nuvem roda pelo GitHub Actions (.github/workflows/backtest.yml) a cada
3 dias; o workflow commita de volta os dias novos de backtest_data/ — é isso
que estende o arquivo além das janelas de retenção das fontes (ensemble
Open-Meteo: ~92 dias).
"""
from __future__ import annotations

try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

import argparse
import os
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tmax import backtest, config, notify, results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-harvest", action="store_true",
                    help="não coleta dias novos; só simula o arquivo atual")
    ap.add_argument("--no-telegram", action="store_true",
                    help="não envia o relatório (só imprime)")
    args = ap.parse_args()

    log = lambda msg: print(msg, flush=True)  # noqa: E731

    if not args.no_harvest:
        added = backtest.harvest(log)
        log(f"harvest: {added} dia(s) novo(s) arquivado(s).")

    # Reconstrução única compartilhada por todas as análises; a calibração é
    # reajustada antes das simulações (que usam a probabilidade calibrada).
    data = backtest._collect_rows(log)
    cal = backtest.fit_calibration(log, data=data)
    stats = backtest.simulate(log, data=data)
    conf = backtest.confidence_report(log=log, data=data)
    fontes = backtest.check_resolution_sources(log)
    # A Ceifa (estratégia ativa) é avaliada SÓ nos nossos snapshots, no
    # relatório diário (run_ceifa.py). Este backtest de 3 dias fica com o
    # arquivo histórico como benchmark de Edge/Colheita e recalibração.
    text = backtest.report_text(stats)
    # Colheita: a variante ATIVA + as alternativas 14h e 12h, para comparar.
    harvests: dict[int, dict] = {}
    for h in sorted({config.HARVEST_MIN_HOUR, 14, 12}, reverse=True):
        hv = backtest.simulate_harvest(log, hour_min=h, data=data)
        harvests[h] = hv
        if not hv["n"]:
            continue
        marca = " ✅ATIVA" if h == config.HARVEST_MIN_HOUR else ""
        text += (f"\n🌾 <b>Colheita {h}h</b>{marca}: {hv['n']} apostas · "
                 f"{hv['hit']:.0%} acerto · composto "
                 f"{hv['compounded']:.2f}x · dd {hv['maxdd']:.0%} · "
                 f"{hv['n_stopped']} stops")

    # Estratégia combinada (Edge + colheita ativa na mesma banca) e persistência
    # dos resultados + números do documento LaTeX.
    active = harvests.get(config.HARVEST_MIN_HOUR, {"signals": []})
    combined = backtest._stats(
        stats["signals"] + active.get("signals", []), 0, stats["days"])
    try:
        results.persist(stats, harvests, combined, conf, cal, stats["days"])
        log("resultados persistidos (backtest_results/ + docs/).")
    except Exception as exc:  # noqa: BLE001 — persistência é acessória
        log(f"AVISO: falha ao persistir resultados ({exc}).")
    if fontes:
        text += ("\n🚨 <b>FONTE DE RESOLUÇÃO MUDOU</b> — a descrição do "
                 "mercado não cita mais a estação esperada: "
                 + ", ".join(fontes) + ". Confira antes de operar!")
    if conf.get("por_faixa_dia"):
        partes = [f"{icao} {g['acerto']:.0%} (n={g['n']}, "
                  f"declarado {g['conf_media']:.0%})"
                  for icao, g in conf["por_faixa_dia"].items()]
        text += (f"\n📏 <b>Confiança ≥ {conf['min_conf']:.0%} no D0</b> — "
                 "acerto por cidade (faixa-dia): " + " · ".join(partes))
    if cal:
        partes = [f"{nome} {s['brier_raw']:.3f}→{s['brier_cal']:.3f}"
                  for nome, s in sorted(cal.items()) if "brier_raw" in s]
        partes += [f"{nome} {s['brier_cal']:.3f}→{s['brier_post']:.3f}"
                   for nome, s in sorted(cal.items())
                   if nome.startswith("blend")]
        text += ("\n🎯 <b>Calibração reajustada</b> (Brier antes→depois): "
                 + " · ".join(partes))
    print("\n" + text.replace("<b>", "").replace("</b>", "")
          .replace("&amp;", "&").replace("&gt;", ">"))

    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not args.no_telegram and token and chat_id:
        notify.send_message(token, chat_id, text)
        print("[telegram] relatório enviado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
