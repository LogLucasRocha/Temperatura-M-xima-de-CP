"""Leitura (somente-leitura) das posições na Polymarket e formatação para o
Telegram.

Fase A do plano de integração: nada de ordens, nada de chave privada — só o
endereço público da carteira (proxy wallet) consultado na Data-API pública.
Ordenar/cancelar posições (Fase B) exigiria a CLOB API, chave privada e um host
sempre-ligado; ver a conversa/README.

A Data-API devolve uma lista de posições; os campos que usamos:
    title, outcome, size, avgPrice, curPrice, currentValue,
    cashPnl, percentPnl, redeemable, endDate, eventSlug
"""
from __future__ import annotations

import html
import re

import requests

from . import config

DATA_API = "https://data-api.polymarket.com"

# Cidade no título do mercado (em inglês) → ICAO da estação que resolve o
# mercado. Cidades fora deste mapa (ex.: Seoul) ficam sem probabilidade.
_MARKET_CITY_TO_ICAO = {
    "moscow": "UUWW",
    "buenos aires": "SAEZ",
    "sao paulo": "SBGR",
    "são paulo": "SBGR",
}

# "Will the highest temperature in <cidade> be <N>°C [or higher/lower] on ...?"
_TEMP_RE = re.compile(
    r"highest temperature in (?P<city>.+?) be (?P<temp>-?\d+)\s*°?\s*c"
    r"(?P<mod>\s+or\s+(?:higher|above|more|lower|below|less))?",
    re.IGNORECASE)


def parse_temp_market(title: str | None) -> dict | None:
    """Extrai {icao, threshold, mode} de um título de mercado de máxima.

    `mode` ∈ {'exact','atleast','atmost'}. Retorna None se não for um mercado de
    temperatura de uma cidade que acompanhamos."""
    m = _TEMP_RE.search(title or "")
    if not m:
        return None
    icao = _MARKET_CITY_TO_ICAO.get(m.group("city").strip().lower())
    if not icao:
        return None
    mod = (m.group("mod") or "").lower()
    if any(w in mod for w in ("higher", "above", "more")):
        mode = "atleast"
    elif any(w in mod for w in ("lower", "below", "less")):
        mode = "atmost"
    else:
        mode = "exact"
    return {"icao": icao, "threshold": int(m.group("temp")), "mode": mode}

# Posições com valor atual abaixo disso são tratadas como poeira e omitidas do
# resumo (evita listar restos de dust de mercados já resolvidos).
DUST_USD = 1.0

# Orçamento de caracteres do corpo (limite do sendMessage é 4096; deixamos
# folga para cabeçalho, rodapé de truncagem e resgatáveis).
BODY_BUDGET = 3500


def fetch_positions(wallet: str, timeout: int = 30) -> list[dict]:
    """Todas as posições da carteira `wallet` (endereço público da proxy wallet).

    `sizeThreshold=1` já filtra dust no lado do servidor. Levanta em erro HTTP —
    o chamador decide se isso derruba o envio ou vira um aviso."""
    r = requests.get(
        f"{DATA_API}/positions",
        params={"user": wallet, "sizeThreshold": 1, "limit": 500},
        headers={"User-Agent": config.USER_AGENT},
        timeout=timeout)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def _fmt_money(v: float) -> str:
    """USDC com sinal só quando faz sentido (P&L). Valores absolutos sem sinal."""
    return f"${v:,.2f}"


def _fmt_signed(v: float) -> str:
    return f"{'+' if v >= 0 else '-'}${abs(v):,.2f}"


def _prob_line(prob: float | None) -> str:
    """Linha da chance de a posição *dar certo* segundo a previsão atual.
    String vazia quando não sabemos casar o mercado com uma estação/dia."""
    if prob is None:
        return "\n   🎯 <i>sem previsão para casar (cidade/dia fora do alcance)</i>"
    if prob >= 0.65:
        tag = "provável ✅"
    elif prob <= 0.35:
        tag = "arriscado 🔴"
    else:
        tag = "incerto ⚠️"
    return f"\n   🎯 chance de dar certo agora: <b>{prob * 100:.0f}%</b> · {tag}"


def _position_line(p: dict, prob: float | None = None) -> str:
    """Uma posição aberta (HTML do Telegram). `prob` = P(dar certo) na previsão
    atual, se conhecida."""
    title = html.escape(str(p.get("title", "?")))
    outcome = html.escape(str(p.get("outcome", "?")))
    slug = p.get("eventSlug") or p.get("slug")
    if slug:
        title = f'<a href="https://polymarket.com/event/{html.escape(str(slug))}">{title}</a>'

    size = float(p.get("size") or 0)
    avg = float(p.get("avgPrice") or 0)
    cur = float(p.get("curPrice") or 0)
    pnl = float(p.get("cashPnl") or 0)
    pct = float(p.get("percentPnl") or 0)
    dot = "🟢" if pnl >= 0 else "🔴"

    end = p.get("endDate")
    end_txt = f" · fecha {str(end)[:10]}" if end else ""

    return (
        f"{dot} {title} — <b>{outcome}</b>\n"
        f"   {size:,.0f} @ ${avg:.3f} → ${cur:.3f} · "
        f"{_fmt_signed(pnl)} ({pct:+.0f}%){end_txt}"
        f"{_prob_line(prob)}")


def positions_message(positions: list[dict], prob_fn=None) -> str:
    """Resumo em HTML do Telegram. Abertas ordenadas por valor atual;
    resolvidas-a-resgatar num rodapé compacto. String vazia se não há nada.

    `prob_fn(position) -> float | None` devolve a probabilidade (0..1) de a
    posição *dar certo* segundo a previsão atual; None quando não dá para casar
    o mercado com uma estação/dia."""
    prob_fn = prob_fn or (lambda _p: None)
    open_pos, redeemable = [], []
    for p in positions:
        if p.get("redeemable"):
            redeemable.append(p)
        elif float(p.get("currentValue") or 0) >= DUST_USD:
            open_pos.append(p)

    if not open_pos and not redeemable:
        return "💼 <b>Polymarket</b>\nSem posições abertas no momento."

    open_pos.sort(key=lambda p: float(p.get("currentValue") or 0), reverse=True)

    total_val = sum(float(p.get("currentValue") or 0) for p in open_pos)
    total_pnl = sum(float(p.get("cashPnl") or 0) for p in open_pos)
    cost = total_val - total_pnl
    total_pct = (total_pnl / cost * 100) if cost > 0 else 0.0

    head = (f"💼 <b>Posições Polymarket</b> · {len(open_pos)} aberta(s) · "
            f"{_fmt_money(total_val)}\n"
            f"P&amp;L não realizado: <b>{_fmt_signed(total_pnl)}</b> "
            f"({total_pct:+.1f}%)")
    blocks = [head]

    # Preenche até o orçamento de caracteres (as maiores primeiro) para nunca
    # estourar o limite do sendMessage.
    lines, used, shown = [], 0, 0
    for p in open_pos:
        line = _position_line(p, prob_fn(p))
        if used + len(line) > BODY_BUDGET:
            break
        lines.append(line)
        used += len(line) + 2
        shown += 1
    blocks.append("\n\n".join(lines))
    if shown < len(open_pos):
        blocks.append(f"<i>… e mais {len(open_pos) - shown} posição(ões) "
                      "(menores, omitidas por espaço).</i>")

    if redeemable:
        val = sum(float(p.get("currentValue") or 0) for p in redeemable)
        blocks.append(
            f"✅ <b>{len(redeemable)}</b> posição(ões) resolvida(s) a resgatar "
            f"(~{_fmt_money(val)}).")

    return "\n\n".join(blocks)
