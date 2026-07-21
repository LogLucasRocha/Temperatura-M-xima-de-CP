"""Sondagem v2 da estrutura de ESPORTES da Polymarket.

Descobre: (1) TODAS as ligas via /sports; (2) como listar os JOGOS de uma liga;
(3) a estrutura do mercado (tokens favorito/azarão); (4) o prices-history
horário de um jogo real (abertura → hora a hora → evento). Não grava nem aposta.

Roda no GitHub Actions. Uso: python -m futebol.probe
"""
from __future__ import annotations

import datetime as dt
import json
import sys

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
S = requests.Session()
S.headers["User-Agent"] = "futebol-research/0.2"


def get(url: str, **params):
    r = S.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def sec(t: str) -> None:
    print("\n" + "=" * 72 + f"\n{t}\n" + "=" * 72, flush=True)


def epoch(iso: str) -> int | None:
    try:
        return int(dt.datetime.fromisoformat(
            iso.replace("Z", "+00:00")).timestamp())
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    # 1) TODAS as ligas.
    sec("1) /sports — todas as ligas (sport | series | tags)")
    sports = get(f"{GAMMA}/sports")
    print(f"total: {len(sports)} ligas")
    for s in sports:
        print(f"  id={s.get('id'):>4}  sport={str(s.get('sport')):<14} "
              f"series={s.get('series')}  tags={s.get('tags')}")

    # 2) Achar o Brasileirão e testar como listar seus jogos.
    sec("2) Brasileirão — achar a liga e listar jogos")
    cand = [s for s in sports if any(
        k in str(s.get("sport", "")).lower()
        for k in ("bra", "brazil", "brasil", "sbr", "serie"))]
    print("candidatos:", [(s.get("id"), s.get("sport"), s.get("series"))
                          for s in cand])
    liga = cand[0] if cand else next(
        (s for s in sports if s.get("sport") == "epl"), sports[0])
    print("usando liga:", liga.get("sport"), "| series:", liga.get("series"),
          "| tags:", liga.get("tags"))

    series_id = str(liga.get("series") or "").split(",")[0]
    tag_ids = [t for t in str(liga.get("tags") or "").split(",") if t]
    jogos = []
    tentativas = ([{"series_id": series_id}] if series_id else []) + \
                 [{"tag_id": t} for t in tag_ids]
    for extra in tentativas:
        for closed in ("true", "false"):
            try:
                evs = get(f"{GAMMA}/events", closed=closed, limit=6,
                          order="startDate", ascending="false", **extra)
                n = len(evs) if isinstance(evs, list) else 0
                print(f"  [events {extra} closed={closed}] -> {n}")
                for e in (evs or [])[:6]:
                    print("      •", e.get("slug"), "|", e.get("title"),
                          "| start:", e.get("startDate"))
                if n and not jogos:
                    jogos = evs
            except Exception as ex:  # noqa: BLE001
                print(f"  [events {extra} closed={closed}] ERRO: {ex}")

    # 3) Estrutura de um JOGO resolvido (times, tokens, preços).
    sec("3) Estrutura de um jogo (moneyline: favorito x azarão)")
    jogo = None
    for e in jogos:
        if e.get("markets"):
            jogo = e
            break
    if not jogo:
        print("sem jogo com mercado nas tentativas; encerro aqui.")
        return 0
    print("jogo:", jogo.get("title"), "|", jogo.get("slug"))
    print("start:", jogo.get("startDate"), "end:", jogo.get("endDate"),
          "closed:", jogo.get("closed"))
    mkt = jogo["markets"][0]
    for k in ("question", "outcomes", "outcomePrices", "clobTokenIds",
              "lastTradePrice", "bestBid", "bestAsk", "spread", "umaEndDate",
              "startDate", "endDate", "closed", "volume"):
        if k in mkt:
            print(f"  {k}: {json.dumps(mkt[k], ensure_ascii=False, default=str)[:200]}")

    # 4) prices-history do jogo (horário) — agora num token REAL.
    sec("4) prices-history horário (abertura → evento)")
    try:
        ids = json.loads(mkt["clobTokenIds"]) if isinstance(
            mkt["clobTokenIds"], str) else mkt["clobTokenIds"]
    except Exception:  # noqa: BLE001
        ids = []
    st = epoch(jogo.get("startDate") or mkt.get("startDate") or "")
    en = epoch(jogo.get("endDate") or mkt.get("endDate") or "")
    print("tokens:", [str(i)[:24] + "…" for i in ids], "| janela:", st, "->", en)
    for i, tok in enumerate(ids):
        got = False
        for params in ({"startTs": st, "endTs": en, "fidelity": 60},
                       {"interval": "max", "fidelity": 60},
                       {"interval": "all", "fidelity": 60}):
            if params.get("startTs") in (None,) or params.get("endTs") in (None,):
                if "startTs" in params:
                    continue
            try:
                d = get(f"{CLOB}/prices-history", market=tok, **params)
                pts = d.get("history") if isinstance(d, dict) else d
                pts = pts or []
                print(f"  token[{i}] {params} -> {len(pts)} pontos")
                if pts:
                    print("     abertura:", pts[0], "| evento:", pts[-1])
                    got = True
                    break
            except Exception as ex:  # noqa: BLE001
                print(f"  token[{i}] {params} ERRO: {ex}")
        if got and i == 0:
            continue
    return 0


if __name__ == "__main__":
    sys.exit(main())
