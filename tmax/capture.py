"""Captura ao vivo dos dados diários para um arquivo permanente próprio.

Objetivo: parar de depender das janelas de retenção das APIs históricas
(Open-Meteo alcança ~92 dias; Polymarket não garante o histórico de preços) e
guardar, no instante em que vemos, tudo que um backtest futuro precisa.

Fluxo em duas etapas (para não commitar a cada 10 min):
  1. A cada rodada do digest, os `record_*` gravam num BUFFER dentro de
     ``data/capture/`` (que o cache do GitHub Actions já persiste entre rodadas):
       - séries tabulares (mercado, previsão, alertas, stops) → JSONL append;
       - arquivos por evento (ensemble, relatórios) → JSON.gz staged.
  2. ``flush()`` (chamado toda rodada, mas só age em dias JÁ FECHADOS) consolida
     o buffer no arquivo definitivo em ``dados/`` (Parquet + JSON.gz), que o
     workflow então commita — na prática, um commit por dia.

Bases (em ``dados/``):
  mercado/{ICAO}/{AAAA-MM}.parquet     preço de todas as faixas, a cada 10 min
  previsao/{ICAO}/{AAAA-MM}.parquet    previsão derivada, a cada 10 min
  alertas/{AAAA-MM}.parquet            todo alerta de edge/colheita (evento)
  stops/{AAAA-MM}.parquet              posição caindo >10% (evento)
  ensemble/{ICAO}/{dia}/{ts}.json.gz   ensemble bruto, só quando o ciclo muda
  relatorios/{ICAO}/{dia}/{ts}.json.gz contexto completo no momento do alerta
"""
from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import json
import shutil
from pathlib import Path

import pandas as pd

from . import config

ARCHIVE_DIR = config.ROOT / "dados"                 # definitivo (vai pro git)
CACHE_DIR = config.DATA_DIR / "capture"             # buffer (cache do Actions)
BUFFER_DIR = CACHE_DIR / "buffer"                   # séries tabulares (JSONL)
FILES_DIR = CACHE_DIR / "files"                     # arquivos por evento (staged)
STATE_FILE = CACHE_DIR / "state.json"               # hash do último ciclo, etc.

# Séries tabulares particionadas por cidade (as de evento não são).
_PER_ICAO = {"mercado", "previsao"}
# Chave natural de cada base, para deduplicar re-execuções da mesma rodada.
_KEYS = {
    "mercado": ["ts_utc", "icao", "faixa"],
    "previsao": ["ts_utc", "icao"],
    "alertas": ["ts_utc", "icao", "faixa", "estrategia"],
    "stops": ["ts_utc", "icao", "faixa"],
}


def _iso(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp(now: dt.datetime) -> str:
    return now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def _utc_date(now: dt.datetime) -> dt.date:
    return now.astimezone(dt.timezone.utc).date()


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state), "utf-8")


# ------------------------------------------------------- gravação (por rodada)

def _append_jsonl(base: str, now: dt.datetime, rows: list[dict]) -> None:
    if not rows:
        return
    path = BUFFER_DIR / base / f"{_utc_date(now).isoformat()}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, default=str) + "\n")


def record_market(now: dt.datetime, icao: str, dia: dt.date,
                  odds: list[dict]) -> None:
    """Todas as faixas do evento de `dia` com seus preços Sim/Não."""
    rows = [{"ts_utc": _iso(now), "icao": icao, "dia": dia.isoformat(),
             "faixa": r.get("label"), "preco_sim": r.get("yes"),
             "preco_nao": r.get("no")} for r in odds]
    _append_jsonl("mercado", now, rows)


def record_forecast(now: dt.datetime, icao: str, dia: dt.date, *,
                    media, mediana, piso_ens, teto_ens, p10, p90,
                    pico_hora, obs_max, nowcast_shift, travada) -> None:
    """Previsão derivada do ensemble corrigido no instante `now`."""
    _append_jsonl("previsao", now, [{
        "ts_utc": _iso(now), "icao": icao, "dia": dia.isoformat(),
        "media": media, "mediana": mediana,
        "piso_ens": piso_ens, "teto_ens": teto_ens, "p10": p10, "p90": p90,
        "pico_hora": pico_hora, "obs_max": obs_max,
        "nowcast_shift": nowcast_shift, "travada": bool(travada)}])


def record_alerts(now: dt.datetime, rows: list[dict]) -> None:
    """Alertas de estratégia disparados nesta rodada (edge/colheita).
    Cada `row`: {icao, dia, estrategia, faixa, lado, preco, modelo, edge_pp,
    hora_local, repeticao}."""
    _append_jsonl("alertas", now,
                  [{"ts_utc": _iso(now), **r} for r in rows])


def record_stops(now: dt.datetime, rows: list[dict]) -> None:
    """Posições >10% abaixo da entrada nesta rodada. Cada `row`:
    {icao, dia, faixa, lado, entrada, atual, queda_pct}."""
    _append_jsonl("stops", now,
                  [{"ts_utc": _iso(now), **r} for r in rows])


def record_ensemble(now: dt.datetime, icao: str, dia: dt.date,
                    times: list, members: dict, bias: dict) -> bool:
    """Grava o ensemble bruto SÓ quando o ciclo muda (dedup por hash do
    conteúdo). Devolve True se gravou. `members`: {chave: [valores]}."""
    payload_members = {str(k): v for k, v in members.items()}
    h = hashlib.sha1(
        json.dumps(payload_members, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
    state = _load_state()
    skey = f"ens:{icao}:{dia.isoformat()}"
    if state.get(skey) == h:
        return False  # mesmo ciclo — nada a fazer
    stage = FILES_DIR / _utc_date(now).isoformat() / "ensemble" / icao / dia.isoformat()
    stage.mkdir(parents=True, exist_ok=True)
    payload = {"issued_utc": _iso(now), "icao": icao, "dia": dia.isoformat(),
               "hash": h, "time": [str(t) for t in times],
               "members": payload_members, "bias": bias}
    (stage / f"{_stamp(now)}.json.gz").write_bytes(
        gzip.compress(json.dumps(payload, default=str).encode("utf-8")))
    state[skey] = h
    _save_state(state)
    return True


def record_report(now: dt.datetime, icao: str, dia: dt.date,
                  report: dict) -> None:
    """Congela o contexto completo da cidade no momento de um alerta novo."""
    stage = FILES_DIR / _utc_date(now).isoformat() / "relatorios" / icao / dia.isoformat()
    stage.mkdir(parents=True, exist_ok=True)
    (stage / f"{_stamp(now)}.json.gz").write_bytes(
        gzip.compress(json.dumps(report, default=str).encode("utf-8")))


# --------------------------------------------------- consolidação (1x/dia)

def _merge_parquet(dest: Path, df: pd.DataFrame, key: list[str]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        df = pd.concat([pd.read_parquet(dest), df], ignore_index=True)
    df = df.drop_duplicates(subset=[c for c in key if c in df.columns],
                            keep="last")
    df.to_parquet(dest, index=False)


def _flush_series(utc_date: dt.date) -> list[Path]:
    changed: list[Path] = []
    month = utc_date.strftime("%Y-%m")
    for base, key in _KEYS.items():
        src = BUFFER_DIR / base / f"{utc_date.isoformat()}.jsonl"
        if not src.exists():
            continue
        df = pd.read_json(src, lines=True)
        if df.empty:
            src.unlink()
            continue
        if base in _PER_ICAO:
            for icao, sub in df.groupby("icao"):
                dest = ARCHIVE_DIR / base / str(icao) / f"{month}.parquet"
                _merge_parquet(dest, sub, key)
                changed.append(dest)
        else:
            dest = ARCHIVE_DIR / base / f"{month}.parquet"
            _merge_parquet(dest, df, key)
            changed.append(dest)
        src.unlink()
    return changed


def _flush_files(utc_date: dt.date) -> list[Path]:
    changed: list[Path] = []
    root = FILES_DIR / utc_date.isoformat()
    if not root.exists():
        return changed
    for src in root.rglob("*.json.gz"):
        rel = src.relative_to(root)            # {base}/{icao}/{dia}/{arquivo}
        dest = ARCHIVE_DIR / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        changed.append(dest)
    shutil.rmtree(root, ignore_errors=True)
    return changed


def flush(now: dt.datetime | None = None) -> list[Path]:
    """Consolida no arquivo definitivo (``dados/``) todos os dias de buffer JÁ
    FECHADOS (data UTC < hoje). Devolve os caminhos alterados (o workflow
    commita se a lista não for vazia). Chamável toda rodada — normalmente não
    faz nada até o dia virar."""
    today = _utc_date(now or dt.datetime.now(dt.timezone.utc))
    dates: set[dt.date] = set()
    for p in BUFFER_DIR.glob("*/*.jsonl"):
        try:
            dates.add(dt.date.fromisoformat(p.stem))
        except ValueError:
            pass
    if FILES_DIR.exists():
        for p in FILES_DIR.iterdir():
            if p.is_dir():
                try:
                    dates.add(dt.date.fromisoformat(p.name))
                except ValueError:
                    pass
    changed: list[Path] = []
    for d in sorted(dates):
        if d >= today:
            continue  # dia ainda em andamento
        changed += _flush_series(d)
        changed += _flush_files(d)
    return changed
