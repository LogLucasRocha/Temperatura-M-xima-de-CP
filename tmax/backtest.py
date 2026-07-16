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

from . import calibration, config, distribution, polymarket

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


def _collect_rows(log=lambda m: None) -> tuple[list, int, int]:
    """Reconstrói, para cada hora pré-trava de cada dia arquivado e cada faixa
    resolvida, a probabilidade CRUA do modelo e o último preço negociado.

    Retorna (rows, dias_vistos, divergências_de_resolução). Cada row:
    {icao, day, hour, ts, settle, bi, label, yes_won, mp, yes} — `yes` é None
    quando não houve negócio recente (ainda serve para calibração)."""
    rows = []
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
                bands.append(dict(bi=bi, lo=pm["lo"], hi=pm["hi"],
                                  mode=pm["mode"], unit=pm["unit"],
                                  yes_won=yes_won, hist=hist,
                                  label=m["question"]))
            if not bands:
                continue

            for b in bands:
                mx = (round(day_max_obs * 9 / 5 + 32) if b["unit"] == "F"
                      else round(day_max_obs))
                met = (b["lo"] <= mx <= b["hi"] if b["mode"] == "exact"
                       else mx >= b["lo"] if b["mode"] == "atleast"
                       else mx <= b["hi"])
                if met != b["yes_won"]:
                    res_mismatch += 1

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
                settle = dt.datetime.combine(
                    day + dt.timedelta(days=1), dt.time(0, 30),
                    tzinfo=tz).timestamp()
                for b in bands:
                    mp = distribution.market_prob(
                        dist, b["lo"], b["hi"], b["mode"], b["unit"])
                    if mp is None:
                        continue
                    pts = [p for ts, p in b["hist"]
                           if ts <= t_epoch and ts >= t_epoch - PRICE_STALENESS_S]
                    rows.append(dict(
                        icao=icao, day=day.isoformat(), hour=t.hour,
                        ts=t_epoch, settle=settle, bi=b["bi"],
                        label=b["label"], yes_won=b["yes_won"], mp=mp,
                        yes=pts[-1] if pts else None, hist=b["hist"],
                        med=dist["quantiles"][50],
                        lo=b["lo"], hi=b["hi"], mode=b["mode"],
                        unit=b["unit"]))
    log(f"{days_seen} dias reconstruídos, {len(rows)} pares "
        "probabilidade × desfecho.")
    return rows, days_seen, res_mismatch


def fit_calibration(log=lambda m: None, data=None) -> dict:
    """Ajusta a calibração completa sobre o arquivo: isotônica por período do
    dia (todas as faixas × horas) + blend logístico com o preço do mercado
    (o backtest mostrou que o preço carrega informação que o modelo não vê).
    Grava tmax/calibration.json e retorna o resumo com Brier antes/depois.
    `data` opcional: resultado de _collect_rows já pronto (evita repetir a
    reconstrução quando várias análises rodam em sequência)."""
    rows, days, _ = data if data is not None else _collect_rows(log)
    pairs: dict[str, list] = defaultdict(list)
    blend_rows = []
    for r in rows:
        y = 1 if r["yes_won"] else 0
        pairs[calibration.bucket_for_hour(r["hour"])].append((r["mp"], y))
        blend_rows.append((r["mp"], r["hour"], r["yes"], y))
    summary = calibration.fit(pairs, blend_rows=blend_rows,
                              meta={"days": days})
    for name, s in sorted(summary.items()):
        if name.startswith("blend"):
            log(f"calibração {name}: n={s['n']} Brier {s['brier_cal']:.4f} -> "
                f"{s['brier_post']:.4f} (a={s['a']} modelo, b={s['b']} preço)")
        else:
            log(f"calibração {name}: n={s['n']} Brier {s['brier_raw']:.4f} -> "
                f"{s['brier_cal']:.4f} ({s['points']} pontos)")
    return summary


def check_resolution_sources(log=lambda m: None) -> list[str]:
    """Confere se a descrição do mercado de HOJE de cada cidade ainda cita o
    ICAO da estação que usamos. Fonte trocada em silêncio (especialmente as
    que dependem de página de terceiro — Wunderground, NOAA timeseries) é
    risco direto de resolução. Retorna a lista de avisos."""
    avisos = []
    today = dt.date.today()
    for icao, station in config.STATIONS.items():
        slug = polymarket.event_slug(icao, today)
        if not slug:
            continue
        try:
            data = _get("https://gamma-api.polymarket.com/events",
                        {"slug": slug}, tries=2, timeout=30).json()
        except Exception as exc:  # noqa: BLE001 — checagem é acessória
            log(f"[fonte] {icao}: erro ao buscar evento ({exc})")
            continue
        ev = data[0] if isinstance(data, list) and data else None
        if not isinstance(ev, dict):
            continue  # sem evento hoje (mercado pode ainda não existir)
        descs = " ".join(str(m.get("description") or "")
                         for m in ev.get("markets", []))
        if descs and icao.lower() not in descs.lower():
            avisos.append(f"{station.city} ({icao})")
            log(f"[fonte] ⚠️ {icao}: descrição do mercado não cita mais a "
                "estação esperada!")
    return avisos


def confidence_report(min_conf: float | None = None,
                      log=lambda m: None, data=None) -> dict:
    """Acerto do modelo no D0 quando a confiança (Probabilidade Real,
    calibrada) esteve acima do corte, POR CIDADE.

    Duas contagens, porque horas do mesmo dia são correlacionadas:
      - por faixa-dia: cada faixa conta UMA vez, no primeiro momento em que a
        confiança cruzou o corte (o mais parecido com "recebi um número
        confiante no Telegram");
      - por avaliação: cada rodada horária conta (mostra persistência).

    Grava backtest_data/confidence_report.json (o workflow commita) e
    retorna o mesmo dicionário."""
    min_conf = min_conf if min_conf is not None else config.EDGE_MIN_CONFIDENCE
    rows, days, _ = data if data is not None else _collect_rows(log)

    def novo():
        return {"n": 0, "acertos": 0, "conf_soma": 0.0}

    per_band: dict[str, dict] = {}
    per_eval: dict[str, dict] = {}
    seen: set = set()
    for r in rows:
        p = calibration.apply(r["mp"], r["hour"])
        if p >= min_conf:
            side_yes, conf = True, p
        elif p <= 1.0 - min_conf:
            side_yes, conf = False, 1.0 - p
        else:
            continue
        hit = r["yes_won"] == side_yes
        g = per_eval.setdefault(r["icao"], novo())
        g["n"] += 1
        g["acertos"] += 1 if hit else 0
        g["conf_soma"] += conf
        key = (r["icao"], r["day"], r["bi"])
        if key not in seen:
            seen.add(key)
            g = per_band.setdefault(r["icao"], novo())
            g["n"] += 1
            g["acertos"] += 1 if hit else 0
            g["conf_soma"] += conf

    def fecha(d):
        return {icao: {"n": g["n"],
                       "acerto": round(g["acertos"] / g["n"], 4),
                       "conf_media": round(g["conf_soma"] / g["n"], 4)}
                for icao, g in sorted(d.items()) if g["n"]}

    out = {"min_conf": min_conf, "days": days,
           "por_faixa_dia": fecha(per_band),
           "por_avaliacao": fecha(per_eval),
           "gerado_em": dt.datetime.now().isoformat(timespec="seconds")}
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    (ARCHIVE / "confidence_report.json").write_text(
        json.dumps(out, indent=1), encoding="utf-8")
    for icao, g in out["por_faixa_dia"].items():
        log(f"confiança ≥{min_conf:.0%} {icao}: {g['n']} faixas-dia, "
            f"acerto {g['acerto']:.1%} (declarado {g['conf_media']:.1%})")
    return out


def simulate(log=lambda m: None, hour_min: int | None = None,
             hour_max: int | None = None, data=None) -> dict:
    """Roda a regra de sinais sobre o arquivo inteiro, com a Probabilidade
    Real (modelo calibrado), igual à produção: só cruzamentos NOVOS dentro da
    janela local (config.SIGNAL_HOURS por padrão) viram aposta; quem cruza o
    corte fora dela é consumido em silêncio (edge de madrugada não é
    executável). Retorna estatísticas e as apostas."""
    hour_min = config.SIGNAL_HOURS[0] if hour_min is None else hour_min
    hour_max = config.SIGNAL_HOURS[1] if hour_max is None else hour_max
    rows, days_seen, res_mismatch = (data if data is not None
                                     else _collect_rows(log))
    signals = []
    done: set = set()
    for r in rows:
        key = (r["icao"], r["day"], r["bi"])
        if key in done:
            continue
        yes = r["yes"]
        if yes is None:
            continue
        mp = calibration.apply(r["mp"], r["hour"])
        if (yes or 0) < 0.01 and mp < 0.01:
            continue
        diff = mp - yes
        if abs(diff) < config.EDGE_ALERT_MIN:
            continue
        side_prob = mp if diff > 0 else 1.0 - mp
        if side_prob <= config.EDGE_MIN_CONFIDENCE:
            continue
        done.add(key)  # cruzou o corte: consome, dentro ou fora da janela
        if not (hour_min <= r["hour"] <= hour_max):
            continue   # cruzamento fora da janela: sem aposta
        side = "SIM" if diff > 0 else "NAO"
        if side not in config.SIGNAL_SIDES:
            continue   # lado fora de operação (decisão: só NÃO)
        if side == "NAO" and (1.0 - yes) < config.NAO_MIN_PRICE:
            continue   # não brigar com mercado quase-certo do Yes
        price = yes if side == "SIM" else 1.0 - yes
        if price <= 0.005 or price >= 0.995:
            continue
        won = r["yes_won"] if side == "SIM" else not r["yes_won"]
        # Stop loss: se, depois da entrada, o preço do LADO COMPRADO cair a
        # STOP_EXIT_FRAC abaixo do preço pago, sai realizando a perda fixa.
        stopped = False
        stop_ts = None
        stop_level = price * (1.0 - config.STOP_EXIT_FRAC)
        for ts, p in r["hist"]:
            if ts <= r["ts"] or ts >= r["settle"]:
                continue
            side_p = p if side == "SIM" else 1.0 - p
            if side_p <= stop_level:
                stopped, stop_ts = True, ts
                break
        signals.append(dict(
            icao=r["icao"], day=r["day"], hour=r["hour"], label=r["label"],
            side=side, price=price, model=side_prob, won=won,
            stopped=stopped, bet_ts=r["ts"],
            settle=stop_ts if stopped else r["settle"]))
    log(f"{days_seen} dias simulados, {len(signals)} sinais "
        "(modelo calibrado).")
    return _stats(signals, res_mismatch, days_seen)


def simulate_harvest(log=lambda m: None, hour_min: int | None = None,
                     data=None) -> dict:
    """Estratégia complementar de colheita: comprar o NÃO quase-certo (preço
    na faixa HARVEST_PRICE_*) após `hour_min` local (padrão:
    HARVEST_MIN_HOUR), com o modelo calibrado concordando. Uma aposta por
    faixa/dia; stop a STOP_EXIT_FRAC."""
    hour_min = config.HARVEST_MIN_HOUR if hour_min is None else hour_min
    rows, days_seen, _ = (data if data is not None else _collect_rows(log))
    signals = []
    done: set = set()
    for r in rows:
        key = (r["icao"], r["day"], r["bi"])
        if key in done:
            continue
        yes = r["yes"]
        if (yes is None or r["hour"] < hour_min
                or r["hour"] > config.SIGNAL_HOURS[1]):
            continue
        price = 1.0 - yes
        if not (config.HARVEST_PRICE_MIN <= price < config.HARVEST_PRICE_MAX):
            continue
        conc = 1.0 - calibration.apply(r["mp"], r["hour"])
        if conc < config.HARVEST_MIN_CONF:
            continue
        done.add(key)
        stopped, st_ts = False, None
        stop_lv = price * (1.0 - config.STOP_EXIT_FRAC)
        for ts, p in r["hist"]:
            if ts <= r["ts"] or ts >= r["settle"]:
                continue
            if (1.0 - p) <= stop_lv:
                stopped, st_ts = True, ts
                break
        signals.append(dict(
            icao=r["icao"], day=r["day"], hour=r["hour"], label=r["label"],
            side="NAO", price=price, model=conc, won=not r["yes_won"],
            stopped=stopped, bet_ts=r["ts"],
            settle=st_ts if stopped else r["settle"]))
    log(f"colheita: {len(signals)} apostas simuladas.")
    return _stats(signals, 0, days_seen)


def simulate_ceifa(log=lambda m: None, data=None) -> dict:
    """Estratégia Ceifa (a ativa): comprar o NÃO quando o preço do NÃO está
    entre CEIFA_PRICE_MIN e CEIFA_PRICE_MAX — critério SÓ de preço, sem filtro
    de hora nem de modelo. Uma aposta por faixa/dia, no primeiro momento em que
    o preço entra na faixa; stop a STOP_EXIT_FRAC. Acerto = o NÃO resolveu (o
    preço do NÃO convergiu para 1,0)."""
    rows, days_seen, _ = (data if data is not None else _collect_rows(log))
    signals = []
    done: set = set()
    for r in rows:
        key = (r["icao"], r["day"], r["bi"])
        if key in done:
            continue
        yes = r["yes"]
        if yes is None:
            continue
        price = 1.0 - yes                       # preço do NÃO
        if not (config.CEIFA_PRICE_MIN < price < config.CEIFA_PRICE_MAX):
            continue
        done.add(key)                            # primeiro cruzamento na faixa
        stopped, st_ts = False, None
        stop_lv = price * (1.0 - config.STOP_EXIT_FRAC)
        for ts, p in r["hist"]:
            if ts <= r["ts"] or ts >= r["settle"]:
                continue
            if (1.0 - p) <= stop_lv:
                stopped, st_ts = True, ts
                break
        signals.append(dict(
            icao=r["icao"], day=r["day"], hour=r["hour"], label=r["label"],
            side="NAO", price=price,
            model=1.0 - calibration.apply(r["mp"], r["hour"]),
            won=not r["yes_won"], stopped=stopped, bet_ts=r["ts"],
            settle=st_ts if stopped else r["settle"]))
    log(f"ceifa: {len(signals)} apostas simuladas.")
    return _stats(signals, 0, days_seen)


def ceifa_report_text(st: dict) -> str:
    """Relatório da Ceifa (HTML do Telegram) com os 4 números: quantidade de
    testes, assertividade, rendimento (realista, sem alavancar) e drawdown."""
    faixa = f"{config.CEIFA_PRICE_MIN:.3f}–{config.CEIFA_PRICE_MAX:.3f}"
    if st["n"] == 0:
        return (f"🌾 <b>Ceifa — desempenho</b> · {st.get('days', 0)} dia(s) · "
                f"nenhuma entrada (NÃO em {faixa}, na H-1) ainda.")
    real = st.get("real_mult", 1.0)
    dd_max = st.get("real_dd", st.get("maxdd", 0.0))
    per_day = st.get("per_day", [])
    ndias = len(per_day) if per_day else st.get("days", 0)
    ret_med = (sum(d["ret"] for d in per_day) / len(per_day)) if per_day else 0.0
    dd_med = (sum(d["dd"] for d in per_day) / len(per_day)) if per_day else 0.0

    return "\n".join([
        "🌾 <b>Ceifa — desempenho (nossos snapshots)</b>",
        f"Comprar NÃO em <b>{faixa}</b>, na hora antes do pico (H-1) · "
        f"{ndias} dia(s) com apostas",
        f"• <b>Testes:</b> {st['n']} · <b>Assertividade:</b> "
        f"{st['hit']:.1%} ({st['wins']}/{st['n']})",
        f"• <b>Rendimento total (sem alavancar):</b> R$100 → "
        f"<b>R${real * 100:.2f}</b> ({(real - 1) * 100:+.1f}%)",
        f"• <b>Retorno diário médio:</b> {ret_med * 100:+.2f}%",
        f"• <b>Drawdown diário médio:</b> {dd_med:.1%} (máximo {dd_max:.1%})",
        f"• {st.get('n_stopped', 0)} stop(s) a −{config.STOP_EXIT_FRAC:.0%}",
        "<i>Cada aposta = 10% do capital disponível (trava até o dia fechar); "
        "a banca liquida no fim do dia e compõe dia a dia. Sem alavancar.</i>",
    ])


def _stats(signals: list, res_mismatch: int, days_seen: int) -> dict:
    if not signals:
        return {"n": 0, "days": days_seen, "signals": [],
                "res_mismatch": res_mismatch}
    n = len(signals)
    wins = sum(1 for s in signals if s["won"])
    n_stopped = sum(1 for s in signals if s.get("stopped"))

    def pnl_flat(s):
        """P&L (fração do capital inicial) de uma aposta com stake fixo."""
        if s.get("stopped"):
            return -STAKE_FRAC * config.STOP_EXIT_FRAC
        return (STAKE_FRAC * (1 / s["price"] - 1)) if s["won"] else -STAKE_FRAC

    flat = sum(pnl_flat(s) for s in signals)

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
            if s.get("stopped"):
                cap += stake * (1.0 - config.STOP_EXIT_FRAC)
            elif s["won"]:
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
            g[k][2] += pnl_flat(s)
        return dict(g)

    return {
        "n": n, "days": days_seen, "wins": wins, "hit": wins / n,
        "n_stopped": n_stopped,
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
        return (f"🧪 <b>Estratégia Edge — simulação histórica</b> · "
                f"{st['days']} dias-cidade no arquivo, nenhuma entrada "
                "no período.")
    lines = [
        f"🧪 <b>Estratégia Edge — simulação histórica</b> · "
        f"{st['days']} dias-cidade de arquivo · {st['n']} entradas que a "
        "regra teria feito (10% do capital cada)",
        f"Acerto: <b>{st['hit']:.0%}</b> ({st['wins']}/{st['n']}) · "
        f"modelo médio {st['avg_model']:.0%} · preço médio "
        f"{st['avg_price']:.2f}",
        f"P&amp;L flat: <b>{st['flat']:+.2f}x</b> o capital inicial · "
        f"composto: <b>{st['compounded']:.2f}x</b> · "
        f"drawdown máx {st['maxdd']:.0%} · "
        f"{st.get('n_stopped', 0)} stopada(s) a "
        f"−{config.STOP_EXIT_FRAC:.0%}",
    ]
    if st["res_mismatch"]:
        lines.append(f"⚠️ {st['res_mismatch']} faixa(s) com resolução "
                     "divergente do METAR arredondado.")
    for title, key in (("Por cidade", "by_city"), ("Por lado", "by_side")):
        parts = [f"{k}: {v[0]} ({v[1] / v[0]:.0%}, {v[2]:+.2f})"
                 for k, v in sorted(st[key].items())]
        lines.append(f"<b>{title}:</b> " + " · ".join(parts))
    return "\n".join(lines)
