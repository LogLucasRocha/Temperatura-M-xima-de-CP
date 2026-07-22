"""Captura ao vivo do mercado de MÍNIMA (lowest temperature) — modo OBSERVAÇÃO.

Réplica do que fazemos no Highest, mas para o mercado de temperatura MÍNIMA das
cidades que a Polymarket cobre (7 das 27). A cada rodada, para cada cidade com
mercado de mínima: grava as faixas com preço (mercado) e a HORA MAIS FRIA
prevista (guardada como pico_hora, para a Ceifa entrar em H-1 sem mudar código).
Grava no lago dados_low/ (mercado/ + previsao/), 1 commit/dia. NÃO aposta.

Roda no .github/workflows/lowtemp_live.yml. Uso: python -m lowtemp.capture
"""
from __future__ import annotations

try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

import datetime as dt
import json
import sys
from pathlib import Path

from tmax import config, pipeline
from tmax import polymarket as pm

BUF_DIR = config.ROOT / "data_low"      # buffer (cache do Actions)
ARCH_DIR = config.ROOT / "dados_low"    # parquet por dia (commitado)


def lowest_slug(icao: str, d: dt.date):
    city = pm._ICAO_TO_CITY_SLUG.get(icao)
    if not city:
        return None
    return (f"lowest-temperature-in-{city}-on-"
            f"{pm._MONTHS[d.month - 1]}-{d.day}-{d.year}")


def _cold_hour(ctx):
    """Hora local do mínimo do dia (análoga ao pico_hora do Highest)."""
    times, _p10, p50, _p90, _raw = pipeline.hourly_percentiles(
        ctx["ens"]["time"], ctx["ens"]["members"], ctx["bias"],
        ctx["shift"], ctx["now"], days={ctx["d0"]})
    valid = [(t, v) for t, v in zip(times, p50) if v is not None]
    return min(valid, key=lambda tv: tv[1])[0].hour if valid else None


def coletar():
    now = dt.datetime.now(dt.timezone.utc)
    stations = {**config.STATIONS, **config.STATIONS_FAHRENHEIT}
    mkt_recs, fc_recs, achou = [], [], 0
    for icao, st in stations.items():
        d0 = dt.datetime.now(st.tz).date()
        slug = lowest_slug(icao, d0)
        if not slug:
            continue
        try:
            ev = pm.fetch_event(slug)
        except Exception:  # noqa: BLE001
            continue
        rows = pm.odds_rows(ev) if ev.get("rows") else []
        if not rows:
            continue                       # cidade sem mercado de mínima hoje
        achou += 1
        try:
            ctx = pipeline.build_context(st, log=lambda _m: None)
            ch = _cold_hour(ctx)
        except Exception as exc:  # noqa: BLE001
            print(f"[{icao}] contexto falhou: {exc}", file=sys.stderr)
            ch = None
        tsiso, dia = now.isoformat(), d0.isoformat()
        for r in rows:
            if r["no"] is None:
                continue
            mkt_recs.append({"ts_utc": tsiso, "icao": icao, "dia": dia,
                             "faixa": r["label"], "preco_sim": r["yes"],
                             "preco_nao": r["no"]})
        if ch is not None:
            fc_recs.append({"ts_utc": tsiso, "icao": icao, "dia": dia,
                            "pico_hora": ch})
    print(f"cidades com mínima: {achou} · linhas mercado: {len(mkt_recs)} · "
          f"previsão: {len(fc_recs)}")
    return mkt_recs, fc_recs


def salvar(mkt: list, fc: list) -> None:
    import pandas as pd

    BUF_DIR.mkdir(exist_ok=True)
    for name, recs in (("mercado", mkt), ("previsao", fc)):
        if recs:
            with open(BUF_DIR / f"{name}.jsonl", "a", encoding="utf-8") as f:
                for r in recs:
                    f.write(json.dumps(r) + "\n")
    hoje = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    for name in ("mercado", "previsao"):
        buf = BUF_DIR / f"{name}.jsonl"
        if not buf.exists():
            continue
        linhas = [json.loads(x) for x in open(buf, encoding="utf-8") if x.strip()]
        if not linhas:
            continue
        df = pd.DataFrame(linhas)
        df["utc"] = df["ts_utc"].str[:10]
        dedup = ["ts_utc", "icao", "dia"] + (["faixa"] if name == "mercado" else [])
        (ARCH_DIR / name).mkdir(parents=True, exist_ok=True)
        for utc, g in df.groupby("utc"):
            if utc >= hoje:
                continue                   # dia UTC corrente fica no buffer
            fp = ARCH_DIR / name / f"{utc}.parquet"
            g = g.drop(columns=["utc"])
            if fp.exists():
                g = pd.concat([pd.read_parquet(fp), g], ignore_index=True)
            g = g.drop_duplicates(dedup)
            g.to_parquet(fp, index=False)
            print(f"arquivado {name}/{utc}: {len(g)} linhas")
        resto = df[df["utc"] >= hoje].drop(columns=["utc"])
        with open(buf, "w", encoding="utf-8") as f:
            for _, r in resto.iterrows():
                f.write(json.dumps(r.to_dict()) + "\n")


def main() -> int:
    mkt, fc = coletar()
    try:
        salvar(mkt, fc)
    except Exception as exc:  # noqa: BLE001 — captura é acessória
        print(f"erro ao salvar: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
