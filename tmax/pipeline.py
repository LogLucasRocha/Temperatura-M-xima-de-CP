"""Pipeline compartilhado: coleta os dados e monta o contexto da previsão.

Usado pelo relatório estático (run_report.py) e pelo painel Streamlit (app.py).
"""
from __future__ import annotations

import datetime as dt

from . import bias as bias_mod
from . import config, distribution, fetch
from .config import Station


def hourly_percentiles(ens_times, member_series, bias, shift, now, days):
    """P10/P50/P90 horários do ensemble corrigido de viés (horas futuras de
    hoje recebem o ajuste do nowcast) + mediana bruta, sem correção alguma.
    Retorna (times, p10, p50, p90, p50_raw)."""
    idx = [i for i, t in enumerate(ens_times) if t.date() in days]
    times = [ens_times[i] for i in idx]

    p10, p50, p90, p50_raw = [], [], [], []
    for i in idx:
        vals, raw = [], []
        for (model, _mid), series in member_series.items():
            v = series[i]
            if v is None:
                continue
            fam = config.ENS_MODELS.get(model)
            b = bias.get(fam, {}).get("bias", 0.0) if fam else 0.0
            adj = shift if (now is not None and ens_times[i] > now
                            and ens_times[i].date() == now.date()) else 0.0
            vals.append(v - b + adj)
            raw.append(v)
        vals.sort()
        raw.sort()
        n = len(vals)
        if n == 0:
            p10.append(None); p50.append(None); p90.append(None)
            p50_raw.append(None)
            continue
        p10.append(vals[max(0, int(0.10 * n) - 1)])
        p50.append(vals[n // 2])
        p90.append(vals[min(n - 1, int(0.90 * n))])
        p50_raw.append(raw[n // 2])
    return times, p10, p50, p90, p50_raw


def build_context(station: Station = config.DEFAULT_STATION,
                  force_bias: bool = False, log=lambda msg: None) -> dict:
    """Executa a coleta e o cálculo completos e devolve o contexto da previsão.

    Levanta RuntimeError se não houver membros de ensemble suficientes.
    """
    now = dt.datetime.now(station.tz)
    d0, d1 = now.date(), now.date() + dt.timedelta(days=1)

    # ------------------------------------------------------------- coleta
    log(f"Buscando METARs de {station.icao}...")
    metars = fetch.fetch_metars(station, hours=48)
    obs_today = [m for m in metars if m["time"].date() == d0]
    latest = metars[-1] if metars else None
    obs_max_today = max((m["temp"] for m in obs_today), default=None)

    # Máxima "travada": as últimas N horas observadas ficaram todas abaixo da
    # máxima do dia — o pico passou e o hora a hora restante de hoje vira ruído.
    hour_max: dict[dt.datetime, float] = {}
    for ob in obs_today:
        h = ob["time"].replace(minute=0, second=0, microsecond=0)
        hour_max[h] = max(hour_max.get(h, ob["temp"]), ob["temp"])
    last_hours = sorted(hour_max)[-config.TMAX_LOCK_HOURS:]
    tmax_locked = (obs_max_today is not None
                   and len(last_hours) >= config.TMAX_LOCK_HOURS
                   and all(hour_max[h] < obs_max_today for h in last_hours))

    log("Buscando TAF...")
    taf = fetch.fetch_taf(station)
    taf_tx = fetch.parse_taf_tx(taf, now, station) if taf else []
    taf_tx_d0 = next((t["temp"] for t in taf_tx if t["local_date"] == d0), None)
    taf_tx_d1 = next((t["temp"] for t in taf_tx if t["local_date"] == d1), None)

    log("Calculando correção de viés (cache diário)...")
    try:
        bias = bias_mod.get_bias(station, force=force_bias)
    except Exception as exc:
        log(f"AVISO: falha ao calcular viés ({exc}); seguindo sem correção.")
        bias = {}
    for fam, b in bias.items():
        log(f"  {fam}: viés {b['bias']:+.2f} °C, desvio residual "
            f"{b['resid_std']:.2f} °C ({b['n_days']} dias)")

    log("Buscando previsão determinística multi-modelo...")
    det = fetch.fetch_deterministic(station, forecast_days=3)

    log("Buscando ensembles (ECMWF ENS + GEFS)...")
    ens = fetch.fetch_ensemble(station, forecast_days=3)
    log(f"  {len(ens['members'])} membros carregados.")

    # ------------------------------------------------------------ nowcast
    nowcast = distribution.compute_nowcast_offset(
        obs_today, ens["time"], ens["members"], bias)
    shift = nowcast["shift"] if nowcast else 0.0
    if nowcast:
        log(f"Nowcast: desvio {nowcast['offset']:+.1f} °C "
            f"({nowcast['n_hours']}h) -> ajuste {shift:+.1f} °C nas horas restantes.")

    # ------------------------------------------------------- distribuições
    # D0: incerteza encolhe conforme o dia avança (máxima ocorre ~14-16h)
    frac_remaining = min(max((17 - now.hour) / 12.0, 0.2), 1.0)
    mm_d0 = distribution.member_maxima_for_day(
        d0, ens["time"], ens["members"], bias,
        now=now, shift=shift, obs_max=obs_max_today)
    dist_d0 = distribution.build_distribution(
        mm_d0, bias, obs_floor=obs_max_today, std_scale=frac_remaining,
        obs_locked=tmax_locked)

    mm_d1 = distribution.member_maxima_for_day(
        d1, ens["time"], ens["members"], bias)
    dist_d1 = distribution.build_distribution(
        mm_d1, bias, std_scale=config.D1_STD_INFLATION)

    if not dist_d0 or not dist_d1:
        raise RuntimeError(
            "sem membros de ensemble suficientes para montar a distribuição")

    # -------------------------------------- determinísticos corrigidos
    daily = det.get("daily", {})
    daily_dates = daily.get("time", [])
    det_corrected: dict[str, dict] = {"d0": {}, "d1": {}}
    for day_key, day in (("d0", d0), ("d1", d1)):
        if day.isoformat() not in daily_dates:
            continue
        i = daily_dates.index(day.isoformat())
        for model, family in config.DET_MODELS.items():
            vals = daily.get(f"temperature_2m_max_{model}")
            if vals is None or vals[i] is None:
                continue
            b = bias.get(family, {}).get("bias", 0.0)
            det_corrected[day_key][model] = {
                "raw": vals[i], "bias": -b, "corrected": vals[i] - b}

    return {
        "station": station,
        "generated": now,
        "now": now,
        "d0": d0, "d1": d1,
        "dist_d0": dist_d0, "dist_d1": dist_d1,
        "det_corrected": det_corrected,
        "bias": bias,
        "shift": shift,
        "ens": ens,
        "obs_today": obs_today,
        "latest_metar": latest,
        "obs_max_today": obs_max_today,
        "tmax_locked": tmax_locked,
        "nowcast": nowcast,
        "taf": taf, "taf_tx": taf_tx,
        "taf_tx_d0": taf_tx_d0, "taf_tx_d1": taf_tx_d1,
    }
