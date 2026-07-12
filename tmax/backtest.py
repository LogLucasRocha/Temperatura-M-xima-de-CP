"""Backtest da regra de sinais + arquivo permanente de dados históricos.

O arquivo vive em backtest_data/{ICAO}/{data}.json.gz — um arquivo
autocontido por cidade-dia com tudo que um backtest precisa:

    {"event":  evento do Polymarket (faixas, tokens, resultado),
     "prices": histórico horário de preço do Yes por faixa,
     "obs":    METARs do dia local [[iso, temp], ...],
     "ens":    fatia do ensemble arquivado {"time": [...], "members": {...}}}

Além disso, backtest_data/{ICAO}/bias_daily.json acumula, por dia, a máxima
observada e as máximas previstas pelos determinísticos — o suficiente para
recalcular o viés "como era" em qualquer data.

A coleta é INCREMENTAL e append-only: dias já arquivados nunca são refeitos.
Como o ensemble arquivado da Open-Meteo só alcança ~92 dias e o histórico do
Polymarket não tem garantia de retenção, rodar a coleta periodicamente é o
que estende o arquivo além dessas janelas.

A simulação reproduz a regra de produção do digest (send_telegram.py):
projeção reconstruída hora a hora com o código real (nowcast, obs_floor,
frac_remaining, sigma travado, viés da época), sinal quando
|projetado − mercado| ≥ EDGE_ALERT_MIN e o lado comprado passa de
EDGE_MIN_CONFIDENCE, uma aposta por faixa/dia no primeiro cruzamento.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
import re
import time
from collections import defaultdict
from pathlib import Path

import requests

from . import config, distribution, polymarket

ARCHIVE = config.ROOT / "backtest_data"
ENS_PAST_DAYS = 92        # alcance do arquivo de ensemble da Open-Meteo
BIAS_EXTRA_DAYS = 70      # folga de histórico p/ viés rolante de 60 dias
STAKE_FRAC = 0.10         # fração do capital por aposta na simulação
PRICE_STALENESS_S = 7200  # idade máxima do último preço para valer no sinal

_session = requests.Session()
_session.headers["User-Agent"] = config.USER_AGENT


def _get(url: str, params=None, tries: int = 4, timeout: int = 120):
    for i in range(tries):
        try:
            r = _session.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(3 * (i + 1))
                continue
            r.raise_for_status()
            return r
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def _as_list(v):
    if isinstance(v, list):
        return v
    try:
        out = json.loads(v)
        return out if isinstance(out, list) else []
    except Exception:
        return []


def day_file(icao: str, day: dt.date) -> Path:
    return ARCHIVE / icao / f"{day.isoformat()}.json.gz"


def write_day(icao: str, day: dt.date, payload: dict) -> None:
    path = day_file(icao, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")))


def read_day(icao: str, day: dt.date) -> dict | None:
    path = day_file(icao, day)
    if not path.exists():
        return None
    return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


def _bias_daily_path(icao: str) -> Path:
    return ARCHIVE / icao / "bias_daily.json"


def _load_bias_daily(icao: str) -> dict:
    path = _bias_daily_path(icao)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


# ---------------------------------------------------------------- coleta

def _fetch_obs_range(station, start: dt.date, end: dt.date) -> dict:
    """METARs horários da IEM agrupados por dia local: {date: [[iso, t]]}."""
    r = _get("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py", {
        "station": station.icao, "data": "tmpc",
        "year1": start.year, "month1": start.month, "day1": start.day,
        "year2": end.year, "month2": end.month, "day2": end.day,
        "tz": station.timezone, "format": "onlycomma", "latlon": "no",
        "missing": "M", "trace": "T", "report_type": ["3", "4"],
    }, timeout=180)
    out: dict[str, list] = defaultdict(list)
    for row in csv.DictReader(io.StringIO(r.text)):
        val = (row.get("tmpc") or "M").strip()
        if val in ("M", ""):
            continue
        try:
            out[row["valid"][:10]].append([row["valid"], float(val)])
        except (ValueError, KeyError):
            continue
    return out


def _fetch_ens_archive(station) -> tuple[list, dict]:
    """Ensemble arquivado (past_days máximos): (times_iso, {model:member: [v]})."""
    r = _get("https://ensemble-api.open-meteo.com/v1/ensemble", {
        "latitude": station.lat, "longitude": station.lon,
        "hourly": "temperature_2m", "models": ",".join(config.ENS_MODELS),
        "timezone": station.timezone,
        "past_days": ENS_PAST_DAYS, "forecast_days": 1,
    }, timeout=300)
    hourly = r.json()["hourly"]
    name_map = {**{m: m for m in config.ENS_MODELS},
                **config.ENS_RESPONSE_ALIASES}
    model_names = sorted(name_map, key=len, reverse=True)
    members = {}
    for key, values in hourly.items():
        if key == "time" or not key.startswith("temperature_2m"):
            continue
        rest = key[len("temperature_2m"):]
        model = next((name_map[m] for m in model_names if m in rest), None)
        if model is None:
            continue
        m = re.search(r"member(\d+)", rest)
        members[f"{model}:{m.group(1) if m else '00'}"] = values
    return hourly["time"], members


def _fetch_det_range(station, start: dt.date, end: dt.date) -> dict:
    """Máximas diárias previstas (as-issued): {date: {model: valor}}."""
    r = _get("https://historical-forecast-api.open-meteo.com/v1/forecast", {
        "latitude": station.lat, "longitude": station.lon,
        "daily": "temperature_2m_max",
        "models": ",".join(config.DET_MODELS),
        "timezone": station.timezone,
        "start_date": start.isoformat(), "end_date": end.isoformat(),
    }, timeout=180)
    daily = r.json().get("daily", {})
    out: dict[str, dict] = {}
    for i, d in enumerate(daily.get("time", [])):
        vals = {}
        for model in config.DET_MODELS:
            arr = daily.get(f"temperature_2m_max_{model}")
            if arr is not None and arr[i] is not None:
                vals[model] = arr[i]
        out[d] = vals
    return out


def _fetch_event(icao: str, day: dt.date) -> dict | None:
    slug = polymarket.event_slug(icao, day)
    data = _get("https://gamma-api.polymarket.com/events",
                {"slug": slug}).json()
    ev = data[0] if isinstance(data, list) and data else None
    if not isinstance(ev, dict):
        return None
    return {"title": ev.get("title"), "markets": [{
        "question": m.get("question"),
        "outcomes": _as_list(m.get("outcomes")),
        "outcomePrices": _as_list(m.get("outcomePrices")),
        "clobTokenIds": _as_list(m.get("clobTokenIds")),
        "closed": m.get("closed"),
    } for m in ev.get("markets", [])]}


def _fetch_prices(station, day: dt.date, event: dict) -> dict:
    """Histórico horário do preço do Yes por índice de faixa."""
    t0 = int(dt.datetime.combine(day, dt.time(0), tzinfo=station.tz).timestamp())
    t1 = int(dt.datetime.combine(day + dt.timedelta(days=1), dt.time(6),
                                 tzinfo=station.tz).timestamp())
    prices = {}
    for bi, m in enumerate(event["markets"]):
        outs = [str(o).lower() for o in m["outcomes"]]
        toks = m["clobTokenIds"]
        if "yes" not in outs or len(toks) != len(outs):
            continue
        try:
            h = _get("https://clob.polymarket.com/prices-history", {
                "market": toks[outs.index("yes")],
                "startTs": t0, "endTs": t1, "fidelity": 60,
            }).json().get("history", [])
        except Exception:
            continue
        if h:
            prices[str(bi)] = [[p["t"], p["p"]] for p in h]
    return prices


def harvest(log=lambda m: None) -> int:
    """Arquiva os dias que faltam (append-only). Retorna quantos dias novos."""
    today = dt.date.today()
    added = 0
    for icao, station in config.STATIONS.items():
        wanted = [today - dt.timedelta(days=k)
                  for k in range(1, ENS_PAST_DAYS)]
        missing = [d for d in wanted if not day_file(icao, d).exists()]

        # bias_daily: estende para trás o suficiente para o viés dos dias novos
        bias_daily = _load_bias_daily(icao)
        bias_start = min(wanted) - dt.timedelta(days=BIAS_EXTRA_DAYS)
        bias_missing = [bias_start + dt.timedelta(days=i)
                        for i in range((today - bias_start).days)
                        if (bias_start + dt.timedelta(days=i)).isoformat()
                        not in bias_daily]

        if not missing and not bias_missing:
            log(f"[{icao}] arquivo em dia.")
            continue

        fetch_start = min(missing + bias_missing)
        log(f"[{icao}] coletando {len(missing)} dia(s) de evento "
            f"(obs/det desde {fetch_start})...")
        obs_by_day = _fetch_obs_range(station, fetch_start, today)
        det_by_day = _fetch_det_range(station, fetch_start,
                                      today - dt.timedelta(days=1))
        ens_times, ens_members = (None, None)

        for dstr, det_vals in det_by_day.items():
            obs = obs_by_day.get(dstr, [])
            bias_daily[dstr] = {
                "obs_max": max((t for _, t in obs), default=None),
                "n_obs": len(obs),
                "det": det_vals,
            }
        _bias_daily_path(icao).parent.mkdir(parents=True, exist_ok=True)
        _bias_daily_path(icao).write_text(
            json.dumps(bias_daily, separators=(",", ":")), encoding="utf-8")

        for day in sorted(missing):
            dstr = day.isoformat()
            obs = obs_by_day.get(dstr, [])
            if not obs:
                continue  # sem METAR ainda; tenta na próxima rodada
            event = _fetch_event(icao, day)
            if event is None:
                continue  # mercado não existia nesse dia
            if ens_times is None:
                log(f"[{icao}] baixando ensemble arquivado...")
                ens_times, ens_members = _fetch_ens_archive(station)
            idx = [i for i, t in enumerate(ens_times) if t[:10] == dstr]
            if not idx:
                continue  # fora do alcance do arquivo de ensemble
            payload = {
                "icao": icao, "date": dstr,
                "event": event,
                "prices": _fetch_prices(station, day, event),
                "obs": obs,
                "ens": {"time": [ens_times[i] for i in idx],
                        "members": {k: [v[i] for i in idx]
                                    for k, v in ens_members.items()}},
            }
            write_day(icao, day, payload)
            added += 1
            log(f"[{icao}] {dstr} arquivado "
                f"({len(payload['prices'])} faixas com preço).")
    return added


# ------------------------------------------------------------- simulação

def _bias_asof(bias_daily: dict, day: dt.date) -> dict:
    end = day - dt.timedelta(days=2)
    start = end - dt.timedelta(days=config.BIAS_LOOKBACK_DAYS)
    out = {}
    for model, family in config.DET_MODELS.items():
        errors = []
        for i in range((end - start).days + 1):
            d = start + dt.timedelta(days=i)
            rec = bias_daily.get(d.isoformat())
            if (not rec or rec["obs_max"] is None
                    or rec["n_obs"] < config.MIN_OBS_PER_DAY
                    or model not in rec["det"]):
                continue
            errors.append(rec["det"][model] - rec["obs_max"])
        if len(errors) < 15:
            continue
        n = len(errors)
        mean = sum(errors) / n
        var = sum((e - mean) ** 2 for e in errors) / max(n - 1, 1)
        out[family] = {"bias": mean, "resid_std": var ** 0.5, "n_days": n}
    return out


def simulate(log=lambda m: None) -> dict:
    """Roda a regra de sinais sobre o arquivo inteiro. Retorna estatísticas e a
    lista de apostas simuladas."""
    signals = []
    res_mismatch = 0
    days_seen = 0
    for icao, station in config.STATIONS.items():
        folder = ARCHIVE / icao
        if not folder.exists():
            continue
        bias_daily = _load_bias_daily(icao)
        for path in sorted(folder.glob("*.json.gz")):
            day = dt.date.fromisoformat(path.stem.replace(".json", ""))
            rec = read_day(icao, day)
            if not rec or not rec.get("obs"):
                continue
            days_seen += 1
            tz = station.tz
            obs_all = [{"time": dt.datetime.strptime(ts, "%Y-%m-%d %H:%M")
                        .replace(tzinfo=tz), "temp": v}
                       for ts, v in rec["obs"]]
            day_times = [dt.datetime.fromisoformat(t).replace(tzinfo=tz)
                         for t in rec["ens"]["time"]]
            day_members = {tuple(k.split(":")): v
                           for k, v in rec["ens"]["members"].items()}
            bias = _bias_asof(bias_daily, day)
            day_max_obs = max(o["temp"] for o in obs_all)

            bands = []
            for bi, m in enumerate(rec["event"]["markets"]):
                pm = polymarket.parse_temp_market(m["question"])
                if not pm or pm["icao"] != icao or not m.get("closed"):
                    continue
                outs = [str(o).lower() for o in m["outcomes"]]
                if "yes" not in outs or len(m["outcomePrices"]) != len(outs):
                    continue
                try:
                    yes_won = float(
                        m["outcomePrices"][outs.index("yes")]) > 0.5
                except (TypeError, ValueError):
                    continue
                hist = rec["prices"].get(str(bi))
                if not hist:
                    continue
                bands.append(dict(bi=bi, threshold=pm["threshold"],
                                  mode=pm["mode"], yes_won=yes_won,
                                  hist=hist, label=m["question"]))
            if not bands:
                continue

            mx = round(day_max_obs)
            for b in bands:
                met = (mx == b["threshold"] if b["mode"] == "exact"
                       else mx >= b["threshold"] if b["mode"] == "atleast"
                       else mx <= b["threshold"])
                if met != b["yes_won"]:
                    res_mismatch += 1

            done: set = set()
            for hour in range(0, 24):
                t = dt.datetime.combine(day, dt.time(hour, 5), tzinfo=tz)
                obs_today = [o for o in obs_all if o["time"] <= t]
                if not obs_today:
                    continue
                obs_max = max(o["temp"] for o in obs_today)
                hour_max: dict = {}
                for o in obs_today:
                    h = o["time"].replace(minute=0, second=0, microsecond=0)
                    hour_max[h] = max(hour_max.get(h, o["temp"]), o["temp"])
                last3 = sorted(hour_max)[-config.TMAX_LOCK_HOURS:]
                if (len(last3) >= config.TMAX_LOCK_HOURS
                        and all(hour_max[h] < obs_max for h in last3)):
                    break  # máxima travada: D0 sai de cena

                nc = distribution.compute_nowcast_offset(
                    obs_today, day_times, day_members, bias)
                shift = nc["shift"] if nc else 0.0
                frac = min(max((17 - t.hour) / 12.0, 0.2), 1.0)
                mm = distribution.member_maxima_for_day(
                    day, day_times, day_members, bias,
                    now=t, shift=shift, obs_max=obs_max)
                dist = distribution.build_distribution(
                    mm, bias, obs_floor=obs_max, std_scale=frac)
                if not dist:
                    continue

                t_epoch = t.timestamp()
                for b in bands:
                    if b["bi"] in done:
                        continue
                    pts = [p for ts, p in b["hist"]
                           if ts <= t_epoch and ts >= t_epoch - PRICE_STALENESS_S]
                    if not pts:
                        continue
                    yes = pts[-1]
                    mp = distribution.market_prob(
                        dist, b["threshold"], b["mode"])
                    if mp is None or ((yes or 0) < 0.01 and mp < 0.01):
                        continue
                    diff = mp - yes
                    if abs(diff) < config.EDGE_ALERT_MIN:
                        continue
                    side_prob = mp if diff > 0 else 1.0 - mp
                    if side_prob <= config.EDGE_MIN_CONFIDENCE:
                        continue
                    side = "SIM" if diff > 0 else "NAO"
                    price = yes if side == "SIM" else 1.0 - yes
                    if price <= 0.005 or price >= 0.995:
                        continue
                    done.add(b["bi"])
                    won = b["yes_won"] if side == "SIM" else not b["yes_won"]
                    signals.append(dict(
                        icao=icao, day=day.isoformat(), hour=t.hour,
                        label=b["label"], side=side, price=price,
                        model=side_prob, won=won, bet_ts=t_epoch,
                        settle=dt.datetime.combine(
                            day + dt.timedelta(days=1), dt.time(0, 30),
                            tzinfo=tz).timestamp()))
    log(f"{days_seen} dias simulados, {len(signals)} sinais.")
    return _stats(signals, res_mismatch, days_seen)


def _stats(signals: list, res_mismatch: int, days_seen: int) -> dict:
    if not signals:
        return {"n": 0, "days": days_seen, "signals": [],
                "res_mismatch": res_mismatch}
    n = len(signals)
    wins = sum(1 for s in signals if s["won"])
    flat = sum((STAKE_FRAC * (1 / s["price"] - 1)) if s["won"] else -STAKE_FRAC
               for s in signals)

    evs = sorted([(s["bet_ts"], 0, i) for i, s in enumerate(signals)]
                 + [(s["settle"], 1, i) for i, s in enumerate(signals)])
    cap, open_stake, peak, maxdd = 1.0, {}, 1.0, 0.0
    for ts, kind, i in evs:
        s = signals[i]
        if kind == 0:
            stake = STAKE_FRAC * cap
            open_stake[i] = stake
            cap -= stake
        else:
            stake = open_stake.pop(i, 0.0)
            if s["won"]:
                cap += stake / s["price"]
            equity = cap + sum(open_stake.values())
            peak = max(peak, equity)
            maxdd = max(maxdd, 1 - equity / peak)
    cap += sum(open_stake.values())

    def agg(keyfn):
        g: dict = defaultdict(lambda: [0, 0, 0.0])
        for s in signals:
            k = keyfn(s)
            g[k][0] += 1
            g[k][1] += 1 if s["won"] else 0
            g[k][2] += ((STAKE_FRAC * (1 / s["price"] - 1)) if s["won"]
                        else -STAKE_FRAC)
        return dict(g)

    return {
        "n": n, "days": days_seen, "wins": wins, "hit": wins / n,
        "avg_model": sum(s["model"] for s in signals) / n,
        "avg_price": sum(s["price"] for s in signals) / n,
        "flat": flat, "compounded": cap, "maxdd": maxdd,
        "res_mismatch": res_mismatch,
        "by_city": agg(lambda s: s["icao"]),
        "by_side": agg(lambda s: s["side"]),
        "by_month": agg(lambda s: s["day"][:7]),
        "by_hour": agg(lambda s: f"{s['hour']:02d}h"),
        "signals": signals,
    }


def report_text(st: dict) -> str:
    """Relatório compacto (HTML do Telegram)."""
    if st["n"] == 0:
        return (f"🧪 <b>Backtest de sinais</b> — {st['days']} dias no arquivo, "
                "nenhum sinal no período.")
    lines = [
        f"🧪 <b>Backtest de sinais</b> · {st['days']} dias-cidade · "
        f"{st['n']} apostas simuladas (10% do capital cada)",
        f"Acerto: <b>{st['hit']:.0%}</b> ({st['wins']}/{st['n']}) · "
        f"modelo médio {st['avg_model']:.0%} · preço médio "
        f"{st['avg_price']:.2f}",
        f"P&amp;L flat: <b>{st['flat']:+.2f}x</b> o capital inicial · "
        f"composto: <b>{st['compounded']:.2f}x</b> · "
        f"drawdown máx {st['maxdd']:.0%}",
    ]
    if st["res_mismatch"]:
        lines.append(f"⚠️ {st['res_mismatch']} faixa(s) com resolução "
                     "divergente do METAR arredondado.")
    for title, key in (("Por cidade", "by_city"), ("Por lado", "by_side")):
        parts = [f"{k}: {v[0]} ({v[1] / v[0]:.0%}, {v[2]:+.2f})"
                 for k, v in sorted(st[key].items())]
        lines.append(f"<b>{title}:</b> " + " · ".join(parts))
    return "\n".join(lines)
