"""Backtest da estratégia Ceifa SOBRE OS NOSSOS SNAPSHOTS (dados/), não sobre o
arquivo reconstruído de APIs.

Regra (decisão do Lucas, 15/07): a entrada é SÓ em H-1 — a hora local anterior
ao pico previsto pelo modelo (H = pico_hora da base previsao). Nessa hora, se o
preço do NÃO está na banda (CEIFA_PRICE_MIN, CEIFA_PRICE_MAX), é uma entrada.
Perto do pico há pouca incerteza — é onde o mercado quase-certo é confiável.

Resolução pela convergência do preço: o NÃO venceu se o preço do NÃO no fim do
dia foi para ~1,0. Stop: se depois da entrada o preço do NÃO cair
STOP_EXIT_FRAC abaixo da entrada, sai a −STOP_EXIT_FRAC (alerta a −10%, saída a
−15% pelo delay de reação).
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

import pandas as pd

from . import config

ARCHIVE = config.ROOT / "dados"
STAKE_FRAC = 0.10


def _load(base: str) -> pd.DataFrame:
    root = ARCHIVE / base
    files = sorted(root.rglob("*.parquet")) if root.exists() else []
    if not files:
        return pd.DataFrame()
    df = pd.concat((pd.read_parquet(f) for f in files), ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    return df.sort_values("ts")


def _local_hour(g: pd.DataFrame) -> pd.Series:
    tz = (config.STATIONS[g.name].tz if g.name in config.STATIONS
          else dt.timezone.utc)
    return g["ts"].dt.tz_convert(tz).dt.hour


def simulate(log=lambda m: None) -> dict:
    """Roda a Ceifa (entrada em H-1) nos snapshots e devolve estatísticas no
    formato que backtest.ceifa_report_text espera."""
    mkt = _load("mercado")
    prev = _load("previsao")
    if mkt.empty or prev.empty:
        log("ceifa (snapshots): sem dados capturados suficientes ainda.")
        return {"n": 0, "days": 0, "signals": []}

    # H (hora do pico previsto) por cidade-dia = moda da pico_hora
    Hs = (prev.dropna(subset=["pico_hora"]).groupby(["icao", "dia"])["pico_hora"]
             .agg(lambda s: int(s.mode().iat[0])).to_dict())
    mkt["hloc"] = mkt.groupby("icao", group_keys=False).apply(_local_hour)

    pmin, pmax = config.CEIFA_PRICE_MIN, config.CEIFA_PRICE_MAX
    stop = config.STOP_EXIT_FRAC
    signals = []
    for (icao, dia, faixa), g in mkt.groupby(["icao", "dia", "faixa"]):
        H = Hs.get((icao, dia))
        if H is None:
            continue
        h1 = g[g["hloc"] == ((H - 1) % 24)]      # snapshots na hora H-1
        if h1.empty:
            continue
        e = h1.iloc[-1]                            # último da hora H-1
        entry = float(e["preco_nao"])
        if not (pmin < entry < pmax):
            continue
        nao_final = float(g["preco_nao"].iloc[-1])
        resolvido = nao_final > 0.90 or nao_final < 0.10
        depois = g[g["ts"] > e["ts"]]
        stopped = bool((depois["preco_nao"] <= entry * (1 - stop)).any())
        if not (resolvido or stopped):
            continue                              # dia ainda em aberto
        won = (not stopped) and (nao_final > 0.5)
        signals.append({"icao": icao, "day": dia, "faixa": faixa,
                        "price": entry, "won": won, "stopped": stopped})
    log(f"ceifa (snapshots, entrada em H-1): {len(signals)} apostas.")
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

    # Rendimento REALISTA (sem alavancar): a cada dia, espalha 100% do capital
    # entre as apostas DAQUELE dia (nunca aposta mais do que tem) e compõe dia
    # a dia. Também o drawdown "de verdade" dessa curva.
    by_day: dict = defaultdict(list)
    for s in signals:
        by_day[s["day"]].append(s)
    real, rpeak, real_dd = 1.0, 1.0, 0.0
    per_day = []
    for day in sorted(by_day):
        bets = by_day[day]
        nb = len(bets)
        stake = real / nb
        novo = 0.0
        for s in bets:
            if s["stopped"]:
                novo += stake * (1 - config.STOP_EXIT_FRAC)
            elif s["won"]:
                novo += stake / s["price"]
            # NÃO perdeu inteiro → 0
        ret = (novo / real - 1.0) if real else 0.0
        real = novo
        rpeak = max(rpeak, real)
        dd_after = (1 - real / rpeak) if rpeak else 0.0
        real_dd = max(real_dd, dd_after)
        per_day.append({"day": day, "n": nb,
                        "wins": sum(1 for s in bets if s["won"]),
                        "ret": ret, "cap": real, "dd": dd_after})

    by = defaultdict(lambda: [0, 0, 0.0])
    for s in signals:
        by[s["icao"]][0] += 1
        by[s["icao"]][1] += 1 if s["won"] else 0
        by[s["icao"]][2] += _pnl_flat(s)
    return {"n": n, "days": days, "wins": wins, "hit": wins / n,
            "n_stopped": n_stopped,
            "avg_price": sum(s["price"] for s in signals) / n,
            "flat": flat, "compounded": cap, "maxdd": maxdd,
            "real_mult": real, "real_dd": real_dd, "per_day": per_day,
            "by_city": dict(by), "signals": signals}
