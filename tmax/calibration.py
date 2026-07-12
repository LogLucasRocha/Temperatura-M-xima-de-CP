"""Calibração empírica das probabilidades do modelo.

O backtest mostrou que a probabilidade crua da mistura é superconfiante nas
caudas (dizia ~99% e acertava ~76%), e que o tamanho do erro depende de
quanta informação o dia já deu: de madrugada (poucas observações) o exagero
é muito maior do que à tarde. Por isso a curva é ajustada POR PERÍODO do dia
local, via regressão isotônica sobre pares (probabilidade emitida, ocorreu?)
extraídos do arquivo de backtest.

`apply(p, hour)` devolve a probabilidade calibrada: interpolação linear na
curva do período; identidade se ainda não houver curva ajustada. Para D+1
(sem hora "informativa"), usa-se o período menos informado (madrugada) — a
projeção de amanhã tem ainda menos informação que a madrugada de hoje.

A curva vive em tmax/calibration.json, versionada junto com o código, e é
reajustada a cada rodada do workflow de backtest conforme o arquivo cresce.
"""
from __future__ import annotations

import datetime as dt
import json
import math
from pathlib import Path

CURVE_FILE = Path(__file__).parent / "calibration.json"

# Períodos do dia local: a madrugada é o regime menos informado (e o mais
# superconfiante); manhã intermediário; tarde/noite já viu o dia se formar.
BUCKETS = ((0, 5, "0-5h"), (6, 11, "6-11h"), (12, 23, "12-23h"))
LEAST_INFORMED = "0-5h"

_cache: dict | None = None


def bucket_for_hour(hour: int | None) -> str:
    if hour is None:
        return LEAST_INFORMED
    for lo, hi, name in BUCKETS:
        if lo <= hour <= hi:
            return name
    return LEAST_INFORMED


def _curves() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CURVE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}
    return _cache


def apply(p: float | None, hour: int | None = None) -> float | None:
    """Probabilidade calibrada (0..1). Fora da faixa ajustada, usa o valor da
    ponta (a resposta empírica honesta), sem extrapolar de volta a 0/1."""
    if p is None:
        return None
    pts = _curves().get("buckets", {}).get(bucket_for_hour(hour))
    if not pts:
        return p
    if p <= pts[0][0]:
        return pts[0][1]
    if p >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= p <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (p - x0) / (x1 - x0)
    return p


def _logit(p: float, eps: float = 1e-4) -> float:
    p = min(max(p, eps), 1 - eps)
    return math.log(p / (1 - p))


def posterior(p_cal: float | None, price: float | None,
              hour: int | None = None) -> float | None:
    """Melhor estimativa combinando o modelo CALIBRADO com o preço do mercado
    (regressão logística POR PERÍODO, ajustada no arquivo de backtest).

    O peso do preço muda de regime: de madrugada o mercado carrega informação
    que o modelo não vê; à tarde, quando divergem, é o modelo que tende a
    estar certo. Sem blend ajustado ou sem preço, devolve o modelo
    calibrado."""
    if p_cal is None:
        return None
    blends = _curves().get("blend") or {}
    b = blends.get(bucket_for_hour(hour)) if isinstance(blends, dict) else None
    if (not isinstance(b, dict) or "a" not in b or price is None
            or not (0.001 <= price <= 0.999)):
        return p_cal
    z = b["a"] * _logit(p_cal) + b["b"] * _logit(price) + b["c"]
    return 1.0 / (1.0 + math.exp(-max(min(z, 30.0), -30.0)))


def fit(pairs_by_bucket: dict[str, list[tuple[float, int]]],
        blend_rows: list[tuple[float, int | None, float | None, int]] | None
        = None,
        meta: dict | None = None) -> dict:
    """Ajusta a isotônica (PAVA) por período + o blend logístico com o preço,
    e grava tudo em disco.

    `pairs_by_bucket`: {bucket: [(prob_crua, 0/1), ...]}
    `blend_rows`: [(prob_crua, hora, preço|None, 0/1), ...]
    Retorna um resumo com Brier antes/depois (in-sample)."""
    global _cache
    buckets: dict[str, list] = {}
    summary: dict[str, dict] = {}
    for name, pairs in pairs_by_bucket.items():
        if len(pairs) < 200:
            continue
        pairs = sorted(pairs)
        # PAVA: blocos [soma_y, n, soma_x] fundidos até ficar não-decrescente
        blocks: list[list[float]] = []
        for x, y in pairs:
            blocks.append([float(y), 1, x])
            while (len(blocks) > 1
                   and blocks[-2][0] / blocks[-2][1]
                   >= blocks[-1][0] / blocks[-1][1]):
                s2, n2, x2 = blocks.pop()
                s1, n1, x1 = blocks.pop()
                blocks.append([s1 + s2, n1 + n2, x1 + x2])
        pts = []
        for s, n, xs in blocks:
            x = xs / n
            y = s / n
            if pts and x <= pts[-1][0]:
                continue
            pts.append([round(x, 6), round(y, 6)])
        buckets[name] = pts

    _cache = {"buckets": buckets}  # curvas ativas para avaliar/ajustar o blend

    for name, pairs in pairs_by_bucket.items():
        if name not in buckets:
            continue
        brier_raw = sum((x - y) ** 2 for x, y in pairs) / len(pairs)
        brier_cal = sum((apply(x, _hour_of(name)) - y) ** 2
                        for x, y in pairs) / len(pairs)
        summary[name] = {"n": len(pairs), "brier_raw": round(brier_raw, 4),
                         "brier_cal": round(brier_cal, 4),
                         "points": len(buckets[name])}

    blend: dict[str, dict] = {}
    if blend_rows:
        by_bucket: dict[str, list] = {}
        for mp, hour, price, y in blend_rows:
            if price is None or not (0.001 <= price <= 0.999):
                continue
            by_bucket.setdefault(bucket_for_hour(hour), []).append(
                (apply(mp, hour), price, y))
        for name, triples in by_bucket.items():
            if len(triples) < 500:
                continue
            a, b, c = _fit_logistic(triples)
            blend[name] = {"a": round(a, 4), "b": round(b, 4),
                           "c": round(c, 4), "n": len(triples)}
        _cache = {"buckets": buckets, "blend": blend}
        for name, triples in by_bucket.items():
            if name not in blend:
                continue
            brier_cal_only = sum((p - y) ** 2 for p, _q, y in triples
                                 ) / len(triples)
            brier_post = sum(
                (posterior(p, q, _hour_of(name)) - y) ** 2
                for p, q, y in triples) / len(triples)
            summary[f"blend {name}"] = {
                "brier_cal": round(brier_cal_only, 4),
                "brier_post": round(brier_post, 4), **blend[name]}

    data = {"fitted_at": dt.datetime.now().isoformat(timespec="seconds"),
            "buckets": buckets, "blend": blend, "summary": summary,
            **(meta or {})}
    CURVE_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
    _cache = data
    return summary


def _fit_logistic(triples: list) -> tuple[float, float, float]:
    """Regressão logística y ~ a·logit(p_modelo) + b·logit(preço) + c via
    Newton com penalização L2 (evita explosão por separação perfeita).
    Aumenta a penalização até os coeficientes ficarem em faixa sadia."""
    X = [(_logit(p), _logit(q), 1.0) for p, q, _ in triples]
    Y = [y for _, _, y in triples]
    lam = max(1.0, 1e-3 * len(triples))
    for _tent in range(5):
        w = [0.5, 0.5, 0.0]
        for _ in range(50):
            g = [lam * wi for wi in w]
            H = [[lam if i == j else 0.0 for j in range(3)]
                 for i in range(3)]
            for x, y in zip(X, Y):
                z = max(min(sum(wi * xi for wi, xi in zip(w, x)), 30.0),
                        -30.0)
                mu = 1.0 / (1.0 + math.exp(-z))
                r = mu - y
                s = mu * (1.0 - mu)
                for i in range(3):
                    g[i] += r * x[i]
                    for j in range(3):
                        H[i][j] += s * x[i] * x[j]
            d = _solve3(H, g)
            # passo limitado: Newton puro dispara com separação quase perfeita
            scale = max(abs(di) for di in d)
            if scale > 2.0:
                d = [di * 2.0 / scale for di in d]
            w = [wi - di for wi, di in zip(w, d)]
            if max(abs(di) for di in d) < 1e-9:
                break
        if max(abs(wi) for wi in w) <= 20.0:
            return w[0], w[1], w[2]
        lam *= 10.0
    return w[0], w[1], w[2]


def _solve3(H, g):
    """Resolve H·d = g (3x3) por eliminação de Gauss com pivoteamento."""
    m = [row[:] + [gi] for row, gi in zip(H, g)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(m[r][col]))
        m[col], m[piv] = m[piv], m[col]
        for r in range(3):
            if r != col and m[col][col] != 0:
                f = m[r][col] / m[col][col]
                m[r] = [a - f * b for a, b in zip(m[r], m[col])]
    return [m[i][3] / (m[i][i] or 1e-12) for i in range(3)]


def _hour_of(bucket_name: str) -> int:
    for lo, _hi, name in BUCKETS:
        if name == bucket_name:
            return lo
    return 0
