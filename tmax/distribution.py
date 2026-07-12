"""Núcleo probabilístico: transforma membros de ensemble + correção de viés +
observações intradiárias em uma distribuição da temperatura máxima do dia."""
from __future__ import annotations

import datetime as dt
import math

from . import config

SQRT2 = math.sqrt(2.0)


def _norm_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * SQRT2)))


def compute_nowcast_offset(obs_today: list[dict], ens_times: list[dt.datetime],
                           member_series: dict, bias: dict) -> dict | None:
    """Desvio médio entre o observado nas últimas horas e a média do ensemble
    (já corrigida de viés) nas mesmas horas. Positivo = dia rodando mais quente
    que o previsto."""
    if not obs_today:
        return None
    # média do ensemble corrigida, hora a hora
    ens_mean: dict[dt.datetime, float] = {}
    for i, t in enumerate(ens_times):
        vals = []
        for (model, _mid), series in member_series.items():
            v = series[i]
            if v is None:
                continue
            fam = config.ENS_MODELS.get(model)
            b = bias.get(fam, {}).get("bias", 0.0) if fam else 0.0
            vals.append(v - b)
        if vals:
            ens_mean[t.replace(minute=0, second=0, microsecond=0)] = sum(vals) / len(vals)

    diffs = []
    for ob in obs_today[-(config.NOWCAST_HOURS * 3):]:  # inclui SPECIs
        hour = ob["time"].replace(minute=0, second=0, microsecond=0)
        if hour in ens_mean:
            diffs.append((ob["time"], ob["temp"] - ens_mean[hour]))
    if not diffs:
        return None
    # mantém apenas as N horas distintas mais recentes
    seen_hours: list = []
    kept = []
    for when, d in reversed(diffs):
        h = when.replace(minute=0, second=0, microsecond=0)
        if h not in seen_hours:
            seen_hours.append(h)
            kept.append(d)
        if len(seen_hours) >= config.NOWCAST_HOURS:
            break
    offset = sum(kept) / len(kept)
    # Desvio de manhã cedo (nevoeiro, resfriamento noturno) correlaciona fraco
    # com o erro da máxima da tarde; o peso cresce conforme o dia esquenta.
    last_hour = obs_today[-1]["time"].hour
    hour_weight = min(max((last_hour - 6) / 6.0, 0.25), 1.0)
    return {
        "offset": offset,
        "shift": config.NOWCAST_DAMPING * hour_weight * offset,
        "hour_weight": hour_weight,
        "n_hours": len(kept),
    }


def member_maxima_for_day(day: dt.date, ens_times: list[dt.datetime],
                          member_series: dict, bias: dict,
                          now: dt.datetime | None = None,
                          shift: float = 0.0,
                          obs_max: float | None = None) -> list[dict]:
    """Máxima diária corrigida por membro. Para D0 (quando `now` é passado),
    horas já decorridas são substituídas pela máxima observada e as horas
    restantes recebem o deslocamento do nowcast."""
    idx = [i for i, t in enumerate(ens_times) if t.date() == day]
    results = []
    for (model, mid), series in member_series.items():
        fam = config.ENS_MODELS.get(model)
        b = bias.get(fam, {}).get("bias", 0.0) if fam else 0.0
        future = [
            series[i] - b + shift
            for i in idx
            if series[i] is not None and (now is None or ens_times[i] > now)
        ]
        candidates = [v for v in ([obs_max] if obs_max is not None else []) + future]
        if not candidates:
            continue
        results.append({"model": model, "member": mid, "family": fam,
                        "tmax": max(candidates),
                        "future_max": max(future) if future else None})
    return results


def build_distribution(member_maxima: list[dict], bias: dict,
                       obs_floor: float | None = None,
                       std_scale: float = 1.0,
                       obs_locked: bool = False) -> dict | None:
    """Mistura de gaussianas: cada membro vira N(tmax, resid_std da família).
    `obs_floor` trunca a distribuição (a máxima não pode ficar abaixo do já
    observado); `obs_locked` diz que o pico OBSERVADO já passou (critério das
    3 horas do pipeline). Retorna quantis e probabilidade por faixa de 1 °C."""
    if not member_maxima:
        return None

    comps = []
    for m in member_maxima:
        s = bias.get(m["family"], {}).get("resid_std", 1.5) * std_scale
        s = max(s, 0.3)
        # Máxima já travada: nenhuma hora restante do membro chega perto do
        # observado, então a "máxima do dia" virou fato medido, não previsão.
        # Sem encolher o sigma, o piso de 0.3 vaza cauda para a faixa acima
        # (ex.: 5% em 26 °C com obs 25.0 e noite a 18 °C). O colapso exige
        # `obs_locked` (pico observado já passou): sem isso ele dispararia de
        # manhã só porque o ensemble PREVÊ tarde mais fria que o já observado
        # — e o backtest mostrou que essa certeza prematura erra feio.
        fut = m.get("future_max")
        if (obs_locked and obs_floor is not None
                and (fut is None or fut + 2 * s < obs_floor)):
            s = 0.05
        comps.append((m["tmax"], s))

    def cdf(x: float) -> float:
        if obs_floor is not None and x < obs_floor:
            return 0.0
        return sum(_norm_cdf(x, mu, s) for mu, s in comps) / len(comps)

    lo = min(mu for mu, _ in comps) - 4 * max(s for _, s in comps)
    hi = max(mu for mu, _ in comps) + 4 * max(s for _, s in comps)
    if obs_floor is not None:
        lo = max(lo, obs_floor - 1)

    def quantile(q: float) -> float:
        a, b = lo, hi
        for _ in range(60):
            mid = (a + b) / 2
            if cdf(mid) < q:
                a = mid
            else:
                b = mid
        return (a + b) / 2

    quantiles = {q: round(quantile(q / 100), 1) for q in (5, 10, 25, 50, 75, 90, 95)}

    b_lo = math.floor(lo) - 1
    b_hi = math.ceil(hi) + 1
    buckets = []
    for t in range(b_lo, b_hi):
        p = cdf(t + 1) - cdf(t)
        if obs_floor is not None and t <= obs_floor < t + 1:
            p = cdf(t + 1)  # massa truncada colapsa na faixa do observado
        if p >= 0.005:
            buckets.append({"low": t, "high": t + 1, "prob": p})
    total = sum(b["prob"] for b in buckets)
    if total > 0:
        for b in buckets:
            b["prob"] /= total

    exceed = {}
    for t in range(b_lo, b_hi + 1):
        p = 1.0 - cdf(float(t))
        if 0.01 <= p <= 0.99:
            exceed[t] = p

    mean = sum(mu for mu, _ in comps) / len(comps)
    if obs_floor is not None:
        mean = max(mean, obs_floor)
    return {
        "quantiles": quantiles,
        "buckets": buckets,
        "exceed": exceed,
        "mean": round(mean, 1),
        "n_members": len(member_maxima),
        # Guardados para reavaliar a distribuição em pontos arbitrários (ex.:
        # probabilidade de um mercado do Polymarket com arredondamento ao meio
        # grau). Mesma definição de CDF usada acima (sem renormalizar a massa
        # truncada), para bater com os `exceed` já exibidos.
        "comps": comps,
        "obs_floor": obs_floor,
    }


def dist_cdf(dist: dict, x: float) -> float | None:
    """P(Tmax ≤ x) da mistura guardada em `dist` (mesma CDF do build). None se a
    distribuição não trouxer os componentes."""
    comps = dist.get("comps")
    if not comps:
        return None
    floor = dist.get("obs_floor")
    if floor is not None and x < floor:
        return 0.0
    return sum(_norm_cdf(x, mu, s) for mu, s in comps) / len(comps)


def market_prob(dist: dict | None, threshold: float, mode: str) -> float | None:
    """Probabilidade do YES de um mercado de máxima resolver, dada a distribuição.

    Convenção do Polymarket "highest temperature be N°C": a máxima reportada é
    arredondada ao inteiro, então a faixa exata de N é [N-0.5, N+0.5).
      - 'exact'   → P(N-0.5 ≤ Tmax < N+0.5)
      - 'atleast' → P(Tmax ≥ N-0.5)   ("N°C or higher")
      - 'atmost'  → P(Tmax < N+0.5)    ("N°C or lower")
    Retorna None se a distribuição não permite o cálculo."""
    if dist is None:
        return None
    if mode == "atleast":
        c = dist_cdf(dist, threshold - 0.5)
        return None if c is None else 1.0 - c
    if mode == "atmost":
        return dist_cdf(dist, threshold + 0.5)
    hi = dist_cdf(dist, threshold + 0.5)
    lo = dist_cdf(dist, threshold - 0.5)
    return None if hi is None or lo is None else max(0.0, hi - lo)
