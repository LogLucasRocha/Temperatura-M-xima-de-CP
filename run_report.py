"""Gera o relatório de previsão da temperatura máxima em SBGR para D0 e D+1.

Uso:
    python run_report.py [--force-bias] [--no-open]

Pipeline:
  1. METAR/TAF em tempo real (aviationweather.gov)
  2. Previsões multi-modelo + ensembles ECMWF ENS / GEFS (Open-Meteo)
  3. Correção de viés aprendida dos últimos 60 dias (Open-Meteo histórico vs METAR/IEM)
  4. Nowcast intradiário: ajusta D0 pelo desvio observado nas últimas horas
  5. Distribuição de probabilidade da máxima + relatório HTML

Para o painel interativo (hover + botão de atualizar): streamlit run app.py
"""
from __future__ import annotations

import truststore

truststore.inject_into_ssl()  # usa os certificados do Windows (proxy corporativo)

import argparse
import datetime as dt
import sys
import webbrowser

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sbgr import config, pipeline, report
from sbgr.pipeline import TZ


def log(msg: str) -> None:
    print(f"[{dt.datetime.now(TZ).strftime('%H:%M:%S')}] {msg}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force-bias", action="store_true",
                    help="recalcula a correção de viés mesmo com cache válido")
    ap.add_argument("--no-open", action="store_true",
                    help="não abre o relatório no navegador ao final")
    args = ap.parse_args()

    try:
        ctx = pipeline.build_context(force_bias=args.force_bias, log=log)
    except RuntimeError as exc:
        log(f"ERRO: {exc}.")
        return 1

    now, d0, d1 = ctx["now"], ctx["d0"], ctx["d1"]
    ens, bias, shift = ctx["ens"], ctx["bias"], ctx["shift"]
    dist_d0, dist_d1 = ctx["dist_d0"], ctx["dist_d1"]

    # -------------------------------------------------------------- gráficos
    log("Gerando gráficos e relatório...")
    ctx["chart_hourly"] = report.chart_hourly(
        ens["time"], ens["members"], bias, shift, now,
        ctx["obs_today"], days={d0, d1})
    det_pts_d0 = {m: v["corrected"] for m, v in ctx["det_corrected"]["d0"].items()}
    det_pts_d1 = {m: v["corrected"] for m, v in ctx["det_corrected"]["d1"].items()}
    ctx["chart_d0"] = report.chart_distribution(
        dist_d0, f"Distribuição da máxima — hoje ({d0.strftime('%d/%m')})",
        det_points=det_pts_d0, taf_tx=ctx["taf_tx_d0"])
    ctx["chart_d1"] = report.chart_distribution(
        dist_d1, f"Distribuição da máxima — amanhã ({d1.strftime('%d/%m')})",
        det_points=det_pts_d1, taf_tx=ctx["taf_tx_d1"])

    html_out = report.render_html(ctx)

    config.REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_file = config.REPORTS_DIR / f"relatorio_{now.strftime('%Y-%m-%d_%H%M')}.html"
    out_file.write_text(html_out, encoding="utf-8")
    latest_file = config.REPORTS_DIR / "latest.html"
    latest_file.write_text(html_out, encoding="utf-8")

    # ------------------------------------------------------------- resumo
    q0, q1 = dist_d0["quantiles"], dist_d1["quantiles"]
    top0 = max(dist_d0["buckets"], key=lambda b: b["prob"])
    top1 = max(dist_d1["buckets"], key=lambda b: b["prob"])
    latest = ctx["latest_metar"]
    obs_max_today = ctx["obs_max_today"]
    taf_tx_d0, taf_tx_d1 = ctx["taf_tx_d0"], ctx["taf_tx_d1"]
    print()
    print("=" * 62)
    print(f"  MÁXIMA EM SBGR — resumo ({now.strftime('%d/%m %H:%M')})")
    print("=" * 62)
    if latest:
        print(f"  Agora: {latest['temp']:.0f} °C"
              + (f" | máx. já observada hoje: {obs_max_today:.0f} °C"
                 if obs_max_today is not None else ""))
    print(f"  D0  ({d0.strftime('%d/%m')}): mediana {q0[50]:.1f} °C "
          f"| P10-P90 {q0[10]:.1f}-{q0[90]:.1f} "
          f"| faixa mais provável {top0['low']}-{top0['high']} °C "
          f"({top0['prob'] * 100:.0f}%)")
    print(f"  D+1 ({d1.strftime('%d/%m')}): mediana {q1[50]:.1f} °C "
          f"| P10-P90 {q1[10]:.1f}-{q1[90]:.1f} "
          f"| faixa mais provável {top1['low']}-{top1['high']} °C "
          f"({top1['prob'] * 100:.0f}%)")
    if taf_tx_d0 is not None:
        print(f"  TAF TX hoje: {taf_tx_d0} °C", end="")
        print(f" | amanhã: {taf_tx_d1} °C" if taf_tx_d1 is not None else "")
    elif taf_tx_d1 is not None:
        print(f"  TAF TX amanhã: {taf_tx_d1} °C")
    print(f"  Relatório: {out_file}")
    print("=" * 62)

    if not args.no_open:
        webbrowser.open(latest_file.as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
