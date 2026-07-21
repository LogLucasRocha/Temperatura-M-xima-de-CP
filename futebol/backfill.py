"""Backfill v2 da hipótese do Lucas, agora com FAVORITO ESTÁVEL (definido pelo
preço já formado, no apito) — evita o viés de seleção do tick de abertura.

Para cada jogo de FUTEBOL resolvido na Polymarket coleta, por time:
  • p_open  = 1º preço (nascimento do mercado)
  • p_24h   = preço ~24h antes do apito (mercado já formado)
  • p_kick  = último preço (≈ apito = "dia do evento")
  • won     = o time venceu? (resolução do mercado)
Favorito = maior p_kick. Mede drift e procura padrões:
  - drift abertura→apito e 24h→apito do favorito/azarão
  - CALIBRAÇÃO: quando o preço diz X%, o time ganha X%? (viés favorito-azarão)
  - o MOVIMENTO do preço prevê o vencedor? (dinheiro esperto / CLV)

Só leitura de dado público. Roda no GitHub Actions: python -m futebol.backfill
"""
from __future__ import annotations

import datetime as dt
import json
import re
import statistics
import sys
from collections import defaultdict

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
SOCCER_TAG = "100350"
MAX_GAMES_PER_LEAGUE = 60
MAX_TOTAL_GAMES = 600
# ligas grandes primeiro (mais líquidas / mais relevantes pro Lucas)
PRIORIDADE = ["bra", "epl", "lal", "bun", "sea", "fl1", "mls", "por", "arg",
              "mex", "ucl", "uel", "uref", "jap", "kor", "ere", "efl"]

S = requests.Session()
S.headers["User-Agent"] = "futebol-research/0.4"


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


def is_base(slug: str) -> bool:
    return bool(re.search(r"-\d{4}-\d{2}-\d{2}$", slug or ""))


def series(token: str, st: int, en: int):
    d = get(f"{CLOB}/prices-history", market=token, startTs=st, endTs=en,
            fidelity=60)
    pts = d.get("history") if isinstance(d, dict) else d
    return [(p["t"], float(p["p"])) for p in (pts or []) if "t" in p and "p" in p]


def at_or_before(ser, target):
    antes = [p for t, p in ser if t <= target]
    return antes[-1] if antes else (ser[0][1] if ser else None)


def ligas_ordenadas(sports):
    fut = [s for s in sports if SOCCER_TAG in str(s.get("tags", "")).split(",")]
    rank = {sp: i for i, sp in enumerate(PRIORIDADE)}
    return sorted(fut, key=lambda s: rank.get(str(s.get("sport")), 999))


def main() -> int:
    sports = get(f"{GAMMA}/sports")
    ligas = ligas_ordenadas(sports)
    print(f"Ligas de futebol: {len(ligas)} (grandes primeiro)")

    obs = []            # 1 registro por TIME por jogo
    total = 0
    for liga in ligas:
        if total >= MAX_TOTAL_GAMES:
            break
        sid = str(liga.get("series") or "").split(",")[0]
        if not sid:
            continue
        try:
            evs = get(f"{GAMMA}/events", series_id=sid, closed="true",
                      limit=100, order="startDate", ascending="false")
        except Exception:  # noqa: BLE001
            continue
        n_liga = 0
        for e in (evs or []):
            if n_liga >= MAX_GAMES_PER_LEAGUE or total >= MAX_TOTAL_GAMES:
                break
            if not is_base(e.get("slug", "")):
                continue
            times = []
            for i, m in enumerate(e.get("markets") or []):
                q = str(m.get("question", "")).lower()
                outs = [str(o).lower() for o in loads(m.get("outcomes"))]
                toks = loads(m.get("clobTokenIds"))
                prices = loads(m.get("outcomePrices"))
                if outs != ["yes", "no"] or not toks or "draw" in q:
                    continue
                if "win on" not in q:
                    continue
                won = str(prices[0]) in ("1", "1.0") if prices else None
                times.append({"label": m.get("groupItemTitle") or q,
                              "yes": toks[0], "won": won,
                              "ordem": i, "start": m.get("startDate"),
                              "end": m.get("endDate")})
            if len(times) < 2:
                continue
            st = epoch(times[0]["start"] or e.get("startDate") or "")
            en = epoch(times[0]["end"] or e.get("endDate") or "")
            if not st or not en:
                continue
            reg = []
            try:
                for j, t in enumerate(times):
                    ser = series(t["yes"], st, en)
                    if len(ser) < 3:
                        reg = []
                        break
                    reg.append({"liga": liga.get("sport"), "slug": e.get("slug"),
                                "p_open": ser[0][1],
                                "p_24h": at_or_before(ser, en - 24 * 3600),
                                "p_kick": ser[-1][1], "won": t["won"],
                                "home": j == 0})   # ordem "home" (sports/ordering)
            except Exception:  # noqa: BLE001
                continue
            if len(reg) < 2 or any(r["won"] is None for r in reg):
                continue
            pk = [r["p_kick"] for r in reg]
            fav_i = max(range(len(reg)), key=lambda k: pk[k])
            for k, r in enumerate(reg):
                r["fav"] = (k == fav_i)
            obs.extend(reg)
            n_liga += 1
            total += 1

    print("=" * 64)
    print(f"AMOSTRA: {total} jogos · {len(obs)} lados de time")
    print("=" * 64)
    if not obs:
        print("Sem dados.")
        return 0

    favs = [r for r in obs if r["fav"]]
    unds = [r for r in obs if not r["fav"]]

    def resumo(nome, xs, campo_ini):
        drift = [r["p_kick"] - r[campo_ini] for r in xs]
        sobe = sum(1 for d in drift if d > 0)
        print(f"{nome} ({campo_ini}→apito): média {statistics.mean(drift):+.4f} "
              f"| mediana {statistics.median(drift):+.4f} "
              f"| subiu {sobe}/{len(xs)} ({sobe/len(xs):.0%})")

    print("\n-- DRIFT (favorito definido pelo preço no APITO, sem viés) --")
    resumo("Favorito abertura", favs, "p_open")
    resumo("Favorito  24h    ", favs, "p_24h")
    resumo("Azarão   abertura", unds, "p_open")
    resumo("Azarão    24h    ", unds, "p_24h")

    # CALIBRAÇÃO: preço no apito vs vitória real (viés favorito-azarão)
    print("\n-- CALIBRAÇÃO no apito (preço previsto × vitória real) --")
    print(f"  {'faixa':<12}{'n':>5}{'preço méd':>11}{'venceu':>9}{'gap':>8}")
    bins = [(i / 10, (i + 1) / 10) for i in range(10)]
    for lo, hi in bins:
        sub = [r for r in obs if lo <= r["p_kick"] < hi]
        if len(sub) >= 10:
            pm = statistics.mean(r["p_kick"] for r in sub)
            wr = statistics.mean(1.0 if r["won"] else 0.0 for r in sub)
            print(f"  {lo:.1f}–{hi:.1f}   {len(sub):>5}{pm:>11.3f}"
                  f"{wr:>9.1%}{wr - pm:>+8.1%}")

    # ABERTURA calibra? (preço de abertura × vitória real)
    print("\n-- CALIBRAÇÃO na ABERTURA (preço de abertura × vitória real) --")
    print(f"  {'faixa':<12}{'n':>5}{'preço méd':>11}{'venceu':>9}{'gap':>8}")
    for lo, hi in bins:
        sub = [r for r in obs if lo <= r["p_open"] < hi]
        if len(sub) >= 10:
            pm = statistics.mean(r["p_open"] for r in sub)
            wr = statistics.mean(1.0 if r["won"] else 0.0 for r in sub)
            print(f"  {lo:.1f}–{hi:.1f}   {len(sub):>5}{pm:>11.3f}"
                  f"{wr:>9.1%}{wr - pm:>+8.1%}")

    # MOVIMENTO prevê o vencedor? (dinheiro esperto / CLV)
    print("\n-- O MOVIMENTO abertura→apito prevê o vencedor? --")
    subiu = [r for r in obs if r["p_kick"] - r["p_open"] > 0.02]
    caiu = [r for r in obs if r["p_kick"] - r["p_open"] < -0.02]
    if subiu:
        wr = statistics.mean(1.0 if r["won"] else 0.0 for r in subiu)
        pm = statistics.mean(r["p_kick"] for r in subiu)
        print(f"  subiu >2pp:  n={len(subiu):>4}  venceu {wr:.1%} "
              f"(preço médio no apito {pm:.1%})")
    if caiu:
        wr = statistics.mean(1.0 if r["won"] else 0.0 for r in caiu)
        pm = statistics.mean(r["p_kick"] for r in caiu)
        print(f"  caiu  >2pp:  n={len(caiu):>4}  venceu {wr:.1%} "
              f"(preço médio no apito {pm:.1%})")

    # MANDANTE (ordem do mercado = home): vence mais que o preço diz?
    print("\n-- MANDANTE (1º mercado = casa) × VISITANTE --")
    for nome, grp in (("mandante", [r for r in obs if r["home"]]),
                      ("visitante", [r for r in obs if not r["home"]])):
        wr = statistics.mean(1.0 if r["won"] else 0.0 for r in grp)
        pm = statistics.mean(r["p_kick"] for r in grp)
        print(f"  {nome:<10} n={len(grp):>4}  venceu {wr:.1%}  "
              f"preço médio apito {pm:.1%}  gap {wr - pm:+.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
