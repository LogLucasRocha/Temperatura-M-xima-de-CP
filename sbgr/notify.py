"""Camada de notificação por Telegram: formata o contexto da previsão em texto
(HTML do Telegram) e compõe um gráfico por estação (trajetória horária +
distribuições D0/D+1) num único PNG pronto para `sendPhoto`.

Reaproveita o núcleo do pipeline; nada de coleta acontece aqui."""
from __future__ import annotations

import html
import io
import time

import matplotlib

matplotlib.use("Agg")  # sem display (roda no GitHub Actions)
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import requests

from . import config
from .pipeline import hourly_percentiles

TELEGRAM_API = "https://api.telegram.org"

BLUE = "#1a5fb4"
ORANGE = "#e56c00"
RED = "#c01c28"
GREEN = "#26a269"

plt.rcParams.update({
    "figure.facecolor": "white",
    "axes.facecolor": "#fafafa",
    "axes.grid": True,
    "grid.color": "#e0e0e0",
    "grid.linewidth": 0.6,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "font.size": 10,
})


# ------------------------------------------------------------------ gráfico

def _draw_hourly(ax, ctx) -> None:
    times, p10, p50, p90, _raw = hourly_percentiles(
        ctx["ens"]["time"], ctx["ens"]["members"], ctx["bias"],
        ctx["shift"], ctx["now"], days={ctx["d0"], ctx["d1"]})
    ax.fill_between(times, p10, p90, color=BLUE, alpha=0.18, label="P10–P90")
    ax.plot(times, p50, color=BLUE, lw=1.8, label="Mediana (corrigida)")
    if ctx["obs_today"]:
        ax.plot([o["time"] for o in ctx["obs_today"]],
                [o["temp"] for o in ctx["obs_today"]], "o-",
                color=RED, ms=4, lw=1.2, label="Observado (METAR)")
    ax.axvline(ctx["now"], color="#666", lw=1, ls="--")
    ax.annotate("agora", (ctx["now"], ax.get_ylim()[1]), fontsize=8,
                color="#666", ha="left", va="top", xytext=(4, -2),
                textcoords="offset points")
    ax.xaxis.set_major_formatter(
        mdates.DateFormatter("%d/%m\n%Hh", tz=times[0].tzinfo))
    ax.set_ylabel("°C")
    ax.set_title("Trajetória horária (hora local)", fontsize=11)
    ax.legend(loc="upper left", fontsize=8, framealpha=0.9)


def _draw_dist(ax, dist, title, det_points, taf_tx) -> None:
    buckets = dist["buckets"]
    xs = [b["low"] + 0.5 for b in buckets]
    ps = [b["prob"] * 100 for b in buckets]
    bars = ax.bar(xs, ps, width=0.92, color=BLUE, alpha=0.75)
    for bar, p in zip(bars, ps):
        if p >= 5:
            ax.annotate(f"{p:.0f}%", (bar.get_x() + bar.get_width() / 2, p),
                        ha="center", va="bottom", fontsize=7, color="#333")
    med = dist["quantiles"][50]
    ax.axvline(med, color=RED, lw=1.5, ls="--", label=f"Mediana {med:.1f}")
    if det_points:
        for v in det_points.values():
            ax.plot(v, 0, marker="^", ms=8, color=ORANGE, clip_on=False, zorder=5)
    if taf_tx is not None:
        ax.axvline(taf_tx, color=GREEN, lw=1.5, ls=":", label=f"TAF {taf_tx:.0f}")
    ax.set_xticks([b["low"] for b in buckets] + [buckets[-1]["high"]])
    ax.set_xlabel("Faixa da máxima (°C)")
    ax.set_ylabel("Prob. (%)")
    ax.set_title(title, fontsize=10)
    ax.legend(loc="upper right", fontsize=7.5, framealpha=0.9)
    ax.set_ylim(0, max(ps) * 1.25 + 2)


def station_chart_png(ctx: dict) -> bytes:
    """Um único PNG por estação: trajetória horária no topo e as distribuições
    de D0 e D+1 embaixo."""
    fig = plt.figure(figsize=(9, 8))
    gs = fig.add_gridspec(2, 2, height_ratios=[1.05, 1], hspace=0.35, wspace=0.2)
    _draw_hourly(fig.add_subplot(gs[0, :]), ctx)

    det0 = {m: v["corrected"] for m, v in ctx["det_corrected"]["d0"].items()}
    det1 = {m: v["corrected"] for m, v in ctx["det_corrected"]["d1"].items()}
    _draw_dist(fig.add_subplot(gs[1, 0]), ctx["dist_d0"],
               f"Hoje ({ctx['d0'].strftime('%d/%m')})", det0, ctx["taf_tx_d0"])
    _draw_dist(fig.add_subplot(gs[1, 1]), ctx["dist_d1"],
               f"Amanhã ({ctx['d1'].strftime('%d/%m')})", det1, ctx["taf_tx_d1"])

    station = ctx["station"]
    # Sem a bandeira: emojis não existem na fonte do matplotlib (viram tofu).
    fig.suptitle(f"{station.city} — {station.icao}",
                 fontsize=13, fontweight="bold")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


# --------------------------------------------------------- tabela de odds PNG

# Divergência (nossa prob. − preço do mercado) a partir da qual destacamos a
# faixa: verde quando achamos o Yes mais provável que o mercado, vermelho quando
# menos. Abaixo disso, sem cor (ruído).
_EDGE = 0.08
_GREEN_BG = "#d7f0dd"
_RED_BG = "#f7d6da"


def _pct(v) -> str:
    return "—" if v is None else f"{v * 100:.0f}%"


def _draw_odds_table(ax, day_label: str, date, rows: list[dict]) -> None:
    ax.axis("off")
    ax.set_title(f"{day_label} ({date.strftime('%d/%m')})",
                 fontsize=11, fontweight="bold", pad=10)
    col_labels = ["Faixa", "Yes", "No",
                  "Probabilidade\nReal de Sim", "Probabilidade\nReal de Não"]
    cell_text = [[r["label"], _pct(r["yes"]), _pct(r["no"]),
                  _pct(r["mp"]), _pct(r["mp_no"])] for r in rows]
    tbl = ax.table(cellText=cell_text, colLabels=col_labels,
                   cellLoc="center", loc="upper center",
                   colWidths=[0.30, 0.105, 0.105, 0.245, 0.245])
    tbl.auto_set_font_size(False)
    tbl.set_fontsize(9)
    tbl.scale(1, 1.55)

    for (row, col), cell in tbl.get_celld().items():
        cell.set_edgecolor("#d9d9d9")
        if row == 0:                                  # cabeçalho (2 linhas)
            cell.set_facecolor(BLUE)
            cell.set_text_props(color="white", fontweight="bold")
            cell.set_fontsize(8)
            cell.set_height(cell.get_height() * 1.7)
        elif row % 2 == 0:                            # zebra
            cell.set_facecolor("#f4f6fa")
        if col == 0 and row > 0:
            cell.get_text().set_ha("left")
            cell.PAD = 0.04

    # Realce da divergência mercado × nós, na coluna "Sim".
    for i, r in enumerate(rows, start=1):
        if r["mp"] is None or r["yes"] is None:
            continue
        edge = r["mp"] - r["yes"]
        if edge >= _EDGE:
            tbl[i, 3].set_facecolor(_GREEN_BG)
        elif edge <= -_EDGE:
            tbl[i, 3].set_facecolor(_RED_BG)


def odds_table_png(station, day_tables: list[tuple]) -> bytes:
    """Um PNG por estação comparando mercado × nossa previsão. `day_tables` é uma
    lista de (rótulo_do_dia, date, rows) — normalmente Hoje e Amanhã."""
    n = len(day_tables)
    max_rows = max(len(rows) for _, _, rows in day_tables)
    fig, axes = plt.subplots(
        1, n, figsize=(5.4 * n, max_rows * 0.42 + 1.4))
    if n == 1:
        axes = [axes]
    for ax, (day_label, date, rows) in zip(axes, day_tables):
        _draw_odds_table(ax, day_label, date, rows)
    fig.suptitle(f"{station.city} ({station.icao}) — mercado × nossa previsão",
                 fontsize=13, fontweight="bold")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


def odds_caption(station) -> str:
    return (f"📊 <b>{html.escape(station.city)} ({station.icao})</b> — "
            "mercado × nossa previsão\n"
            "<i>Yes/No = preço do mercado (prob. implícita) · Sim/Não = nossa "
            "previsão de o resultado acontecer · verde/vermelho = onde divergimos "
            "do mercado.</i>")


# -------------------------------------------------------------------- texto

def _exceed_summary(dist: dict) -> str:
    """Prob. de exceder os limiares inteiros ao redor da mediana (info-chave
    para os mercados de temperatura)."""
    med = round(dist["quantiles"][50])
    parts = []
    for t in (med - 1, med, med + 1, med + 2):
        p = dist["exceed"].get(t)
        if p is not None:
            parts.append(f"≥{t} {p * 100:.0f}%")
    return " · ".join(parts)


def station_lines(ctx: dict) -> str:
    """Legenda (HTML do Telegram) do gráfico de uma estação. Cada tópico é um
    parágrafo separado por linha em branco, para não se perder na leitura."""
    s = ctx["station"]
    q0, q1 = ctx["dist_d0"]["quantiles"], ctx["dist_d1"]["quantiles"]

    head = [f"{s.flag} <b>{html.escape(s.city)} ({s.icao})</b>"]
    if ctx["latest_metar"]:
        agora = f"Agora: <b>{ctx['latest_metar']['temp']:.0f} °C</b>"
        if ctx["obs_max_today"] is not None:
            agora += f" · máx. já hoje: {ctx['obs_max_today']:.0f} °C"
        head.append(agora)
    blocks = ["\n".join(head)]

    if ctx["nowcast"]:
        nc = ctx["nowcast"]
        direction = "mais quente" if nc["offset"] > 0 else "mais frio"
        blocks.append(
            f"📡 <b>Nowcast:</b> nas últimas {nc['n_hours']}h o observado está "
            f"<b>{abs(nc['offset']):.1f} °C {direction}</b> que o ensemble "
            f"corrigido → ajuste de <b>{nc['shift']:+.1f} °C</b> nas horas "
            "restantes de hoje.")

    taf0 = f" · TAF {ctx['taf_tx_d0']:.0f}" if ctx["taf_tx_d0"] is not None else ""
    blocks.append(
        f"Hoje {ctx['d0'].strftime('%d/%m')}: <b>{q0[50]:.1f} °C</b> "
        f"(P10–P90 {q0[10]:.1f}–{q0[90]:.1f}){taf0}\n"
        f"{_exceed_summary(ctx['dist_d0'])}")

    taf1 = f" · TAF {ctx['taf_tx_d1']:.0f}" if ctx["taf_tx_d1"] is not None else ""
    blocks.append(
        f"Amanhã {ctx['d1'].strftime('%d/%m')}: <b>{q1[50]:.1f} °C</b> "
        f"(P10–P90 {q1[10]:.1f}–{q1[90]:.1f}){taf1}\n"
        f"{_exceed_summary(ctx['dist_d1'])}")

    return "\n\n".join(blocks)


def station_hourly_lines(ctx: dict) -> str:
    """Mensagem separada com a trajetória hora a hora (a mesma do gráfico):
    mediana corrigida + faixa P10–P90 por hora, agrupada por dia. Vai como
    `sendMessage` (limite de 4096) e não na legenda do gráfico (limite 1024)."""
    s = ctx["station"]
    times, p10, p50, p90, _raw = hourly_percentiles(
        ctx["ens"]["time"], ctx["ens"]["members"], ctx["bias"],
        ctx["shift"], ctx["now"], days={ctx["d0"], ctx["d1"]})

    # Observado por hora, para marcar as horas já decorridas de hoje.
    obs_by_hour = {
        o["time"].replace(minute=0, second=0, microsecond=0): o["temp"]
        for o in ctx["obs_today"]}

    blocks = [f"{s.flag} <b>{html.escape(s.city)} ({s.icao})</b> — hora a hora\n"
              "<i>mediana corrigida · faixa P10–P90 · obs = METAR observado</i>"]

    current_day = None
    day_lines: list[str] = []
    for t, lo, md, hi in zip(times, p10, p50, p90):
        if md is None:
            continue
        if t.date() != current_day:
            if day_lines:
                blocks.append("\n".join(day_lines))
            current_day = t.date()
            label = "Hoje" if current_day == ctx["d0"] else "Amanhã"
            day_lines = [f"<b>{label} {current_day.strftime('%d/%m')}</b>"]
        obs = obs_by_hour.get(t.replace(minute=0, second=0, microsecond=0))
        line = f"{t.strftime('%Hh')}  <b>{md:.1f}°</b> (P10–P90 {lo:.0f}–{hi:.0f})"
        if obs is not None:
            line += f" · obs {obs:.0f}°"
        day_lines.append(line)
    if day_lines:
        blocks.append("\n".join(day_lines))

    return "\n\n".join(blocks)


def digest_header(when_label: str) -> str:
    return (f"🌡️ <b>Previsão de máxima</b> · {when_label}\n"
            "Mediana e P10–P90 do ensemble corrigido; ≥X = prob. de exceder.")


def station_divider(station) -> str:
    """Separador visual que abre o bloco de uma estação no digest."""
    return (f"━━━━━━━━━━━━━━━\n"
            f"{station.flag} <b>{html.escape(station.city).upper()} "
            f"({station.icao})</b>\n"
            f"━━━━━━━━━━━━━━━")


# ----------------------------------------------------------- envio Telegram

_RETRY_WAITS = (3, 8, 20)  # segundos entre tentativas


def _tg_post(token: str, method: str, data: dict, files=None,
             timeout: int = 60) -> None:
    """POST na Bot API com retry para falhas transitórias (timeout, conexão,
    5xx e 429 respeitando o retry_after do flood control).

    Um timeout DEPOIS de o Telegram receber o envio pode duplicar a mensagem
    no chat ao repetir — aceitável num digest; pior seria perder o resto."""
    for wait in (*_RETRY_WAITS, None):
        try:
            r = requests.post(f"{TELEGRAM_API}/bot{token}/{method}",
                              data=data, files=files, timeout=timeout)
            r.raise_for_status()
            return
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout):
            if wait is None:
                raise
        except requests.exceptions.HTTPError as exc:
            resp = exc.response
            status = resp.status_code if resp is not None else 0
            if wait is None or (status < 500 and status != 429):
                raise
            if status == 429:
                try:
                    wait = max(wait, int(resp.json()["parameters"]["retry_after"]))
                except Exception:
                    pass
        time.sleep(wait)


def send_message(token: str, chat_id: str, text: str) -> None:
    _tg_post(token, "sendMessage",
             {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": "true"},
             timeout=30)


def send_photo(token: str, chat_id: str, png: bytes, caption: str) -> None:
    _tg_post(token, "sendPhoto",
             {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
             files={"photo": ("previsao.png", png, "image/png")},
             timeout=90)
