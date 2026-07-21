"""Backfill da hipótese do Lucas: 'quando o mercado abre, o favorito está
subvalorizado e o azarão supervalorizado'.

Para cada jogo de FUTEBOL já resolvido na Polymarket, puxa a trajetória horária
(prices-history, fidelity=60) do mercado-resultado e compara:
  • abertura   = primeiro preço (nascimento do mercado)
  • pré-jogo   = último preço ATÉ ~2h antes do fim (perto do apito inicial)
  • fim        = último preço (pós-jogo / resolução)
Favorito = maior probabilidade na abertura. Drift = pré-jogo − abertura.
A hipótese prevê drift do favorito > 0 e do azarão < 0.

Só leitura de dado público — não aposta. Roda no GitHub Actions:
    python -m futebol.backfill
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
import sys
import time
from collections import defaultdict

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
SOCCER_TAG = "100350"          # tag "Soccer" (descoberta na sondagem)
MAX_GAMES_PER_LEAGUE = 40
MAX_TOTAL_GAMES = 400
PRE_MATCH_MARGIN = 2 * 3600    # "pré-jogo" = até 2h antes do fim (≈ apito)

S = requests.Session()
S.headers["User-Agent"] = "futebol-research/0.3"


def get(url: str, **params):
    r = S.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def epoch(iso: str):
    try:
        return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:  # noqa: BLE001
        return None


def loads(x):
    if isinstance(x, str):
        try:
            return json.loads(x)
        except Exception:  # noqa: BLE001
            return []
    return x or []


def is_base_moneyline(slug: str) -> bool:
    """Evento-base do jogo (termina em -AAAA-MM-DD, sem sufixo de mercado
    exótico como -exact-score, -more-markets, -halftime-result, ...)."""
    import re
    return bool(re.search(r"-\d{4}-\d{2}-\d{2}$", slug or ""))


def price_series(token: str, st: int, en: int):
    d = get(f"{CLOB}/prices-history", market=token, startTs=st, endTs=en,
            fidelity=60)
    pts = d.get("history") if isinstance(d, dict) else d
    return [(p["t"], float(p["p"])) for p in (pts or []) if "t" in p and "p" in p]


def three_points(series, end_ts):
    """(abertura, pré-jogo, fim) de uma série de (t, p)."""
    if not series:
        return None
    abertura = series[0][1]
    fim = series[-1][1]
    corte = end_ts - PRE_MATCH_MARGIN if end_ts else None
    pre = None
    if corte:
        antes = [p for t, p in series if t <= corte]
        pre = antes[-1] if antes else None
    if pre is None:
        pre = fim
    return abertura, pre, fim


def main() -> int:
    sports = get(f"{GAMMA}/sports")
    ligas = [s for s in sports
             if SOCCER_TAG in str(s.get("tags", "")).split(",")]
    print(f"Ligas de futebol (tag {SOCCER_TAG}): {len(ligas)}")
    print("  " + ", ".join(sorted(str(s.get("sport")) for s in ligas)))

    registros = []
    di010 = 0          # diagnóstico dos primeiros jogos
    total = 0
    dbg = 0
    skip = defaultdict(int)
    for liga in ligas:
        if total >= MAX_TOTAL_GAMES:
            break
        series_id = str(liga.get("series") or "").split(",")[0]
        if not series_id:
            continue
        try:
            evs = get(f"{GAMMA}/events", series_id=series_id, closed="true",
                      limit=100, order="startDate", ascending="false")
        except Exception as ex:  # noqa: BLE001
            print(f"[{liga.get('sport')}] erro ao listar: {ex}")
            continue
        jogos = [e for e in (evs or []) if is_base_moneyline(e.get("slug", ""))]
        if dbg < 5:
            amostra = [e.get("slug") for e in (evs or [])[:4]]
            um = (evs or [{}])[0]
            print(f"[dbg {liga.get('sport')}] n_evs={len(evs or [])} "
                  f"n_base={len(jogos)} keys0={sorted(um.keys())[:6]} "
                  f"tem_markets={'markets' in um} slugs={amostra}")
            dbg += 1
        n_liga = 0
        for e in jogos:
            if n_liga >= MAX_GAMES_PER_LEAGUE or total >= MAX_TOTAL_GAMES:
                break
            mkts = e.get("markets") or []
            if not mkts:
                skip["sem_markets"] += 1
                continue
            # Resultado = 3 mercados BINÁRIOS Yes/No (vitória casa / empate /
            # vitória fora). O token "Yes" de cada um = P(aquele resultado).
            # Queremos favorito×azarão = os dois de TIME (empate à parte).
            resultado = []            # (label, yes_token, is_draw)
            for m in mkts:
                q = str(m.get("question", "")).lower()
                outs = [str(o).lower() for o in loads(m.get("outcomes"))]
                toks = loads(m.get("clobTokenIds"))
                if outs != ["yes", "no"] or not toks:
                    continue
                if not ("win on" in q or "draw" in q):
                    continue
                resultado.append((m.get("groupItemTitle") or m.get("question"),
                                  toks[0], "draw" in q))
            times = [r for r in resultado if not r[2]]
            if len(times) < 2:
                skip["sem_moneyline"] += 1
                if skip["sem_moneyline"] <= 3:
                    print(f"  [sem_moneyline] {e.get('slug')}: "
                          f"questions={[mm.get('question') for mm in mkts][:4]}")
                continue
            st = epoch(mkts[0].get("startDate") or e.get("startDate") or "")
            en = epoch(mkts[0].get("endDate") or e.get("endDate") or "")
            if not st or not en:
                skip["sem_ts"] += 1
                continue
            pontos = {}
            try:
                for label, tok, _ in times:
                    tp = three_points(price_series(tok, st, en), en)
                    if tp:
                        pontos[label] = tp
            except Exception as ex:  # noqa: BLE001
                print(f"[{e.get('slug')}] prices-history erro: {ex}")
                continue
            if len(pontos) < 2:
                skip["poucos_pontos"] += 1
                continue
            # favorito = maior prob na abertura
            fav = max(pontos, key=lambda k: pontos[k][0])
            azar = min(pontos, key=lambda k: pontos[k][0])
            fo, fp, ff = pontos[fav]
            ao, ap, af = pontos[azar]
            registros.append({
                "liga": liga.get("sport"), "slug": e.get("slug"),
                "fav": fav, "fav_open": fo, "fav_pre": fp,
                "azar": azar, "azar_open": ao, "azar_pre": ap,
                "n_out": len(pontos)})
            n_liga += 1
            total += 1
            if di010 < 8:
                print(f"  ex [{liga.get('sport')}] {e.get('slug')}: "
                      f"fav={fav[:16]} {fo:.3f}→{fp:.3f} | "
                      f"azar={azar[:16]} {ao:.3f}→{ap:.3f}")
                di010 += 1

    # ------- agregados -------
    print("\n" + "=" * 60)
    print(f"AMOSTRA: {len(registros)} jogos resolvidos")
    print("descartes:", dict(skip))
    print("=" * 60)
    if not registros:
        print("Sem jogos processados.")
        return 0
    fav_drift = [r["fav_pre"] - r["fav_open"] for r in registros]
    azar_drift = [r["azar_pre"] - r["azar_open"] for r in registros]
    fav_sobe = sum(1 for d in fav_drift if d > 0)
    azar_cai = sum(1 for d in azar_drift if d < 0)

    def stats(xs):
        return (statistics.mean(xs),
                statistics.median(xs),
                statistics.pstdev(xs))

    fm, fmed, fsd = stats(fav_drift)
    am, amed, asd = stats(azar_drift)
    print(f"FAVORITO  drift médio abertura→pré-jogo: {fm:+.4f} "
          f"(mediana {fmed:+.4f}, dp {fsd:.4f})")
    print(f"          subiu em {fav_sobe}/{len(registros)} "
          f"({fav_sobe/len(registros):.1%}) dos jogos")
    print(f"AZARÃO    drift médio abertura→pré-jogo: {am:+.4f} "
          f"(mediana {amed:+.4f}, dp {asd:.4f})")
    print(f"          caiu em {azar_cai}/{len(registros)} "
          f"({azar_cai/len(registros):.1%}) dos jogos")

    # por faixa de favoritismo na abertura
    print("\nPor força do favorito na abertura:")
    faixas = [("0.50–0.65", 0.50, 0.65), ("0.65–0.80", 0.65, 0.80),
              ("0.80–0.95", 0.80, 0.95), ("0.95+", 0.95, 1.01)]
    for nome, lo, hi in faixas:
        sub = [r for r in registros if lo <= r["fav_open"] < hi]
        if sub:
            d = statistics.mean([r["fav_pre"] - r["fav_open"] for r in sub])
            print(f"  {nome}: n={len(sub):>3}  drift favorito {d:+.4f}")

    # por liga
    print("\nPor liga (n≥5):")
    porl = defaultdict(list)
    for r in registros:
        porl[r["liga"]].append(r["fav_pre"] - r["fav_open"])
    for liga, ds in sorted(porl.items(), key=lambda kv: -len(kv[1])):
        if len(ds) >= 5:
            print(f"  {liga:<8} n={len(ds):>3}  drift favorito "
                  f"{statistics.mean(ds):+.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
