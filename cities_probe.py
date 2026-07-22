"""Sondagem: descobre TODAS as cidades de temperatura que a Polymarket lista
(máxima/mínima) e mostra quais não temos. Testa Milan e Wuhan explicitamente.

Roda no GitHub Actions (a rede desta sessão é fechada). Uso: python cities_probe.py
"""
from __future__ import annotations

import datetime as dt
import re
import sys

import requests

from tmax import polymarket as pm

GAMMA = "https://gamma-api.polymarket.com"
S = requests.Session()
S.headers["User-Agent"] = "cities-probe/0.1"
SLUG_RE = re.compile(r"^(highest|lowest)-temperature-in-(.+?)-on-[a-z]+-\d+-\d+")


def get(url: str, **p):
    r = S.get(url, params=p, timeout=30)
    r.raise_for_status()
    return r.json()


def ev_ok(d):
    ev = d[0] if isinstance(d, list) and d else (d if isinstance(d, dict) else None)
    return ev if ev and ev.get("markets") else None


def scan(params, cidades):
    off = 0
    while off < 6000:
        try:
            evs = get(f"{GAMMA}/events", closed="false", limit=100, offset=off,
                      **params)
        except Exception as exc:  # noqa: BLE001
            print("scan err:", exc)
            break
        if not isinstance(evs, list) or not evs:
            break
        for e in evs:
            m = SLUG_RE.match(e.get("slug", "") or "")
            if m:
                cidades.add(m.group(2))
        if len(evs) < 100:
            break
        off += 100


# Cidades candidatas (slug provável na Polymarket). As que já temos ficam de
# fora do "novas". Testa highest (hoje/ontem) e, se achar, quantos dias de
# histórico + se tem lowest.
CANDIDATAS = [
    "milan", "wuhan", "berlin", "rome", "munich", "frankfurt", "hamburg",
    "barcelona", "lisbon", "dublin", "vienna", "prague", "budapest", "athens",
    "stockholm", "oslo", "copenhagen", "helsinki", "zurich", "geneva",
    "brussels", "rotterdam", "dubai", "abu-dhabi", "doha", "riyadh", "tel-aviv",
    "delhi", "new-delhi", "mumbai", "bangalore", "kolkata", "chennai",
    "hyderabad", "bangkok", "jakarta", "manila", "kuala-lumpur", "hanoi",
    "ho-chi-minh-city", "hong-kong", "taipei", "guangzhou", "shenzhen",
    "chengdu", "osaka", "cairo", "lagos", "nairobi", "johannesburg",
    "cape-town", "casablanca", "sydney", "melbourne", "brisbane", "perth",
    "auckland", "rio-de-janeiro", "brasilia", "lima", "bogota", "santiago",
    "montevideo", "caracas", "kyiv", "athens",
]


def dias_hist(city, kind, n=12):
    hoje = dt.date.today()
    hits = 0
    for back in range(n):
        d = hoje - dt.timedelta(days=back)
        slug = (f"{kind}-temperature-in-{city}-on-"
                f"{pm._MONTHS[d.month - 1]}-{d.day}-{d.year}")
        try:
            if ev_ok(get(f"{GAMMA}/events", slug=slug)):
                hits += 1
        except Exception:  # noqa: BLE001
            pass
    return hits


def main() -> int:
    nossas = set(pm._ICAO_TO_CITY_SLUG.values())
    hoje = dt.date.today()

    print("=== varredura de cidades candidatas (highest) ===")
    achou = []
    for city in dict.fromkeys(CANDIDATAS):     # dedup preservando ordem
        # teste rápido: hoje OU ontem existe?
        existe = any(
            ev_ok(get(f"{GAMMA}/events", slug=(
                f"highest-temperature-in-{city}-on-"
                f"{pm._MONTHS[d.month - 1]}-{d.day}-{d.year}")))
            for d in (hoje, hoje - dt.timedelta(days=1))
        )
        if not existe:
            continue
        jah = city in nossas
        low = dias_hist(city, "lowest", 6) > 0
        achou.append((city, jah, low))
        tag = "(já temos)" if jah else "NOVA"
        print(f"  {'✓':<2} {city:<18} highest {tag:<11} lowest: {'sim' if low else 'não'}")

    novas = [c for c, jah, _ in achou if not jah]
    print(f"\nNOVAS que a Polymarket tem e não capturamos: {novas or 'nenhuma'}")
    print(f"(total candidatas com highest: {len(achou)})")
    print("\nObs.: lista de candidatas é curada — pode haver outras. "
          "A varredura automática não rola porque a Gamma limita a paginação.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
