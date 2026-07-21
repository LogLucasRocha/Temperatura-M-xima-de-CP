"""Captura ao vivo IN-PLAY do futebol na Polymarket (modo OBSERVAÇÃO).

A cada rodada pega os jogos de futebol AO VIVO (bola rolando) e, para cada lado
cujo preço está perto da certeza (>= 0,90), grava preço + bestBid/bestAsk +
flag da banda [0,95; 0,995). NÃO aposta. Espelha a captura da temperatura:
grava em dados_futebol/{dia-UTC}.parquet, consolidando 1×/dia; o buffer do dia
corrente vive em data_futebol/ (cache do Actions).

O bestAsk é o dado que faltava no backtest: diz o preço REAL de compra no
momento da banda — ou seja, se dá pra executar a 0,95 ou se o ask já subiu.

Roda no .github/workflows/futebol_live.yml (cron 10 min).
Uso local: python -m futebol.live
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path

import requests

GAMMA = "https://gamma-api.polymarket.com"
SOCCER_TAG = "100350"
WATCH = 0.90                 # só grava lados a partir daqui (perto da certeza)
BAND_LO, BAND_HI = 0.95, 0.995
ROOT = Path(__file__).resolve().parent.parent
BUF_DIR = ROOT / "data_futebol"      # buffer do dia (cache)
ARCH_DIR = ROOT / "dados_futebol"    # parquet por dia (commitado)

S = requests.Session()
S.headers["User-Agent"] = "futebol-live/0.1"


def get(url: str, **params):
    r = S.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def epoch(iso):
    try:
        return int(dt.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except Exception:  # noqa: BLE001
        return None


def loads(x):
    if isinstance(x, str):
        try:
            return json.loads(x)
        except Exception:  # noqa: BLE001
            return []
    return x or []


def is_base(slug: str) -> bool:
    return bool(re.search(r"-\d{4}-\d{2}-\d{2}$", slug or ""))


def coletar() -> list[dict]:
    """Snapshots dos lados perto da certeza nos jogos de futebol AO VIVO."""
    agora = dt.datetime.now(dt.timezone.utc)
    now = int(agora.timestamp())
    recs, offset, vivos = [], 0, 0
    while offset < 1000:
        try:
            evs = get(f"{GAMMA}/events", tag_id=SOCCER_TAG, closed="false",
                      limit=100, offset=offset)
        except Exception as exc:  # noqa: BLE001
            print(f"erro ao listar eventos (offset {offset}): {exc}",
                  file=sys.stderr)
            break
        if not isinstance(evs, list) or not evs:
            break
        for e in evs:
            slug = e.get("slug", "")
            if not is_base(slug):
                continue
            ko = epoch(e.get("endDate"))          # endDate ≈ apito (in-play)
            if not ko or not (ko <= now <= ko + 4 * 3600):
                continue                           # só jogos AO VIVO
            vivos += 1
            liga = slug.split("-")[0]
            for m in e.get("markets") or []:
                q = str(m.get("question", "")).lower()
                outs = [str(o).lower() for o in loads(m.get("outcomes"))]
                if outs != ["yes", "no"] or "win on" not in q:
                    continue
                try:
                    price = float(m.get("lastTradePrice"))
                except (TypeError, ValueError):
                    continue
                if price < WATCH:
                    continue
                recs.append({
                    "ts_utc": agora.isoformat(),
                    "dia": agora.strftime("%Y-%m-%d"),
                    "liga": liga, "slug": slug,
                    "team": m.get("groupItemTitle") or q[:40],
                    "price": price,
                    "bid": _f(m.get("bestBid")), "ask": _f(m.get("bestAsk")),
                    "band": bool(BAND_LO <= price < BAND_HI),
                    "kickoff": ko})
        if len(evs) < 100:
            break
        offset += 100
    print(f"jogos ao vivo: {vivos} · lados >=0,90 gravados: {len(recs)} "
          f"(na banda: {sum(1 for r in recs if r['band'])})")
    return recs


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def salvar(recs: list[dict]) -> None:
    """Anexa ao buffer e consolida os dias UTC já fechados em parquet."""
    import pandas as pd

    BUF_DIR.mkdir(exist_ok=True)
    buf = BUF_DIR / "buffer.jsonl"
    if recs:
        with open(buf, "a", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
    if not buf.exists():
        return
    linhas = [json.loads(x) for x in open(buf, encoding="utf-8") if x.strip()]
    if not linhas:
        return
    hoje = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    df = pd.DataFrame(linhas)
    ARCH_DIR.mkdir(exist_ok=True)
    for dia, g in df.groupby("dia"):
        if dia >= hoje:
            continue                               # dia corrente fica no buffer
        fp = ARCH_DIR / f"{dia}.parquet"
        if fp.exists():
            g = pd.concat([pd.read_parquet(fp), g], ignore_index=True)
        g = g.drop_duplicates(["ts_utc", "slug", "team"])
        g.to_parquet(fp, index=False)
        print(f"arquivado {dia}: {len(g)} linhas")
    resto = df[df["dia"] >= hoje]
    with open(buf, "w", encoding="utf-8") as f:
        for _, r in resto.iterrows():
            f.write(json.dumps(r.to_dict()) + "\n")


def main() -> int:
    recs = coletar()
    try:
        salvar(recs)
    except Exception as exc:  # noqa: BLE001 — captura é acessória
        print(f"erro ao salvar: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
