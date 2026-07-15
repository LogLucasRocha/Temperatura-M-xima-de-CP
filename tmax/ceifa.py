"""Backtest da estratégia Ceifa SOBRE OS NOSSOS SNAPSHOTS (dados/mercado), não
sobre o arquivo reconstruído de APIs.

Regra: uma entrada por contrato — a PRIMEIRA vez que o preço do NÃO entra na
banda (CEIFA_PRICE_MIN, CEIFA_PRICE_MAX), em QUALQUER hora. Resolução pela
convergência do preço: o NÃO venceu se o preço do NÃO no fim do dia foi para
~1,0. Stop: se depois da entrada o preço do NÃO cair STOP_EXIT_FRAC abaixo da
entrada, sai realizando −STOP_EXIT_FRAC (o delay de reação: alerta a −10%,
saída a −15%).
"""
from __future__ import annotations

from collections import defaultdict

import pandas as pd

from . import config

ARCHIVE = config.ROOT / "dados"
STAKE_FRAC = 0.10


def _load_market() -> pd.DataFrame:
    root = ARCHIVE / "mercado"
    files = sorted(root.rglob("*.parquet")) if root.exists() else []
    if not files:
        return pd.DataFrame()
    df = pd.concat((pd.read_parquet(f) for f in files), ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    return df.sort_values("ts")


def simulate(log=lambda m: None) -> dict:
    """Roda a Ceifa nos snapshots e devolve estatísticas no mesmo formato que
    backtest.ceifa_report_text espera."""
    mkt = _load_market()
    if mkt.empty:
        log("ceifa (snapshots): sem dados capturados ainda.")
        return {"n": 0, "days": 0, "signals": []}

    # resolução por contrato: preço do NÃO no fim do dia
    fin = (mkt.groupby(["icao", "dia", "faixa"])
              .agg(nao_final=("preco_nao", "last")).reset_index())
    fin["resolvido"] = (fin["nao_final"] > 0.90) | (fin["nao_final"] < 0.10)
    fin["nao_venceu"] = fin["nao_final"] > 0.5
    finm = fin.set_index(["icao", "dia", "faixa"])

    pmin, pmax = config.CEIFA_PRICE_MIN, config.CEIFA_PRICE_MAX
    stop = config.STOP_EXIT_FRAC
    signals = []
    for (icao, dia, faixa), g in mkt.groupby(["icao", "dia", "faixa"]):
        ent = g[(g["preco_nao"] > pmin) & (g["preco_nao"] < pmax)]
        if ent.empty:
            continue
        e = ent.iloc[0]
        entry = float(e["preco_nao"])
        try:
            r = finm.loc[(icao, dia, faixa)]
        except KeyError:
            continue
        depois = g[g["ts"] > e["ts"]]
        stopped = bool((depois["preco_nao"] <= entry * (1 - stop)).any())
        if not (bool(r["resolvido"]) or stopped):
            continue                      # ainda não resolveu (dia em aberto)
        won = (not stopped) and bool(r["nao_venceu"])
        signals.append({"icao": icao, "day": dia, "faixa": faixa,
                        "price": entry, "won": won, "stopped": stopped})
    log(f"ceifa (snapshots): {len(signals)} apostas.")
    return _stats(signals, mkt["dia"].nunique())


def _pnl_flat(s: dict) -> float:
    if s["stopped"]:
        return -STAKE_FRAC * config.STOP_EXIT_FRAC
    return STAKE_FRAC * (1.0 / s["price"] - 1.0) if s["won"] else -STAKE_FRAC


def _stats(signals: list, days: int) -> dict:
    n = len(signals)
    if n == 0:
        return {"n": 0, "days": days, "signals": []}
    wins = sum(1 for s in signals if s["won"])
    n_stopped = sum(1 for s in signals if s["stopped"])
    flat = sum(_pnl_flat(s) for s in signals)

    cap, peak, maxdd = 1.0, 1.0, 0.0
    for s in sorted(signals, key=lambda x: x["day"]):
        bet = STAKE_FRAC * cap
        if s["stopped"]:
            cap -= bet * config.STOP_EXIT_FRAC
        elif s["won"]:
            cap += bet * (1.0 / s["price"] - 1.0)
        else:
            cap -= bet
        peak = max(peak, cap)
        maxdd = max(maxdd, 1 - cap / peak)

    by = defaultdict(lambda: [0, 0, 0.0])
    for s in signals:
        by[s["icao"]][0] += 1
        by[s["icao"]][1] += 1 if s["won"] else 0
        by[s["icao"]][2] += _pnl_flat(s)
    return {"n": n, "days": days, "wins": wins, "hit": wins / n,
            "n_stopped": n_stopped,
            "avg_price": sum(s["price"] for s in signals) / n,
            "flat": flat, "compounded": cap, "maxdd": maxdd,
            "by_city": dict(by), "signals": signals}
