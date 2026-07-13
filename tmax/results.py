"""Persistência dos resultados do backtest e geração dos números do
documento LaTeX (docs/tmax.tex).

Chamado por run_backtest.py a cada rodada (a cada 3 dias + watchdog):
  - backtest_results/<estrategia>.json guarda o ÚLTIMO resultado de cada
    estratégia, para consulta e versionamento;
  - docs/generated_numbers.tex traz os mesmos números como macros LaTeX, de
    modo que o documento didático nunca fique desatualizado — o workflow
    commita ambos.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from . import config

RESULTS_DIR = config.ROOT / "backtest_results"
NUMBERS_TEX = config.ROOT / "docs" / "generated_numbers.tex"

_KEEP = ("n", "days", "hit", "avg_model", "avg_price", "flat", "compounded",
         "maxdd", "n_stopped", "res_mismatch", "by_city", "by_side")

_HOUR_WORD = {12: "twelve", 14: "fourteen", 16: "sixteen"}


def _slim(st: dict) -> dict:
    return {k: st[k] for k in _KEEP if k in st}


def persist(edge: dict, harvests: dict, combined: dict, conf: dict,
            cal: dict, days: int) -> None:
    """Grava backtest_results/*.json e regenera docs/generated_numbers.tex."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    span = max(round(days / max(len(config.STATIONS), 1)), 1)

    def w(name: str, obj: dict) -> None:
        (RESULTS_DIR / f"{name}.json").write_text(
            json.dumps({**obj, "generated_at": stamp}, indent=1,
                       ensure_ascii=False), encoding="utf-8")

    w("edge", _slim(edge))
    for h, st in harvests.items():
        w(f"harvest_{h}h", _slim(st))
    w("combined", _slim(combined))
    w("confidence", conf)
    w("calibration", {"summary": cal})
    w("meta", {"days_city": days, "span_days": span,
               "stations": len(config.STATIONS)})

    _write_numbers(edge, harvests, combined, conf, span, stamp)


def _write_numbers(edge, harvests, combined, conf, span, stamp) -> None:
    lines = ["% Gerado por tmax/results.py a cada backtest — NÃO editar à mão.",
             f"% Atualizado em {stamp}", ""]

    def cmd(name, value):
        lines.append(f"\\newcommand{{\\{name}}}{{{value}}}")

    def pct(x):
        return f"{x * 100:.0f}\\%"

    def strat(prefix, st):
        cmd(f"{prefix}N", st["n"])
        cmd(f"{prefix}Hit", pct(st["hit"]))
        cmd(f"{prefix}Comp", f"{st['compounded']:.2f}")
        cmd(f"{prefix}Flat", f"{st['flat']:+.2f}")
        cmd(f"{prefix}DD", pct(st["maxdd"]))
        cmd(f"{prefix}Stops", st["n_stopped"])
        cmd(f"{prefix}Price", f"{st.get('avg_price', 0):.2f}")

    strat("edge", edge)
    strat("comb", combined)
    monthly = combined["compounded"] ** (30.0 / span) - 1.0
    cmd("combMonthly", f"{monthly * 100:+.0f}\\%")

    for h, st in harvests.items():
        strat(f"harv{_HOUR_WORD.get(h, 'x')}", st)
    active = harvests.get(config.HARVEST_MIN_HOUR)
    if active:
        strat("harv", active)          # alias da variante ATIVA
    cmd("harvActiveHour", config.HARVEST_MIN_HOUR)

    fd = conf.get("por_faixa_dia", {})
    if fd:
        accs = [v["acerto"] for v in fd.values()]
        cmd("confMin", pct(min(accs)))
        cmd("confMax", pct(max(accs)))

    cmd("btDaysCity", edge.get("days", 0))
    cmd("btSpan", span)
    cmd("btStations", len(config.STATIONS))
    cmd("btUpdated", stamp.replace("T", " ").replace("+00:00", " UTC"))

    NUMBERS_TEX.parent.mkdir(parents=True, exist_ok=True)
    NUMBERS_TEX.write_text("\n".join(lines) + "\n", encoding="utf-8")
