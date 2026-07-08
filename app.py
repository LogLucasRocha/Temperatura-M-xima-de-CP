"""Painel interativo da previsão de Tmax em SBGR (Guarulhos).

Uso:
    streamlit run app.py

Mesmos dados do run_report.py, mas com gráficos Plotly (hover mostra a
temperatura exata em cada ponto) e botão de atualização que refaz a coleta.
"""
from __future__ import annotations

import truststore

truststore.inject_into_ssl()  # usa os certificados do Windows (proxy corporativo)

import datetime as dt

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from sbgr import config, pipeline

BLUE = "#1a5fb4"
ORANGE = "#e56c00"
RED = "#c01c28"
GREEN = "#26a269"
BAND_FILL = "rgba(26, 95, 180, 0.18)"

st.set_page_config(page_title="Previsão de TMax", page_icon="🌡️", layout="wide")


@st.cache_data(ttl=600, show_spinner="Buscando METAR, TAF, modelos e ensembles...")
def load_context() -> dict:
    return pipeline.build_context()


def fig_hourly(ctx: dict) -> go.Figure:
    """Trajetória horária: banda P10–P90, mediana e METARs, com hover unificado."""
    times, p10, p50, p90, p50_raw = pipeline.hourly_percentiles(
        ctx["ens"]["time"], ctx["ens"]["members"], ctx["bias"],
        ctx["shift"], ctx["now"], days={ctx["d0"], ctx["d1"]})

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=times, y=p90, name="P90",
        line=dict(width=0), showlegend=False,
        hovertemplate="P90: %{y:.1f} °C<extra></extra>"))
    fig.add_trace(go.Scatter(
        x=times, y=p10, name="Ensemble P10–P90 (corrigido)",
        fill="tonexty", fillcolor=BAND_FILL, line=dict(width=0),
        hovertemplate="P10: %{y:.1f} °C<extra></extra>"))
    fig.add_trace(go.Scatter(
        x=times, y=p50, name="Ensemble corrigido (mediana)",
        line=dict(color=BLUE, width=2.5),
        hovertemplate="Ensemble corrigido: %{y:.1f} °C<extra></extra>"))
    fig.add_trace(go.Scatter(
        x=times, y=p50_raw, name="Ensemble bruto (mediana)",
        line=dict(color="#888888", width=1.5, dash="dot"),
        hovertemplate="Ensemble bruto: %{y:.1f} °C<extra></extra>"))
    if ctx["obs_today"]:
        fig.add_trace(go.Scatter(
            x=[o["time"] for o in ctx["obs_today"]],
            y=[o["temp"] for o in ctx["obs_today"]],
            name="Observado (METAR)", mode="lines+markers",
            line=dict(color=RED, width=1.5), marker=dict(size=6),
            hovertemplate="Observado: %{y:.1f} °C<extra></extra>"))

    now = ctx["now"]
    fig.add_shape(type="line", x0=now, x1=now, y0=0, y1=1, yref="paper",
                  line=dict(color="#666666", width=1, dash="dash"))
    fig.add_annotation(x=now, y=1, yref="paper", text="agora",
                       showarrow=False, xanchor="left", yanchor="top",
                       font=dict(color="#666666", size=11))

    fig.update_layout(
        hovermode="x unified",
        yaxis_title="Temperatura (°C)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(l=10, r=10, t=30, b=10),
        height=420,
    )
    fig.update_xaxes(tickformat="%d/%m\n%Hh", showspikes=True,
                     spikemode="across", spikethickness=1)
    return fig


def fig_distribution(dist: dict, det_points: dict, taf_tx) -> go.Figure:
    """Barras de probabilidade por faixa de 1 °C, com mediana, TAF e modelos
    determinísticos marcados."""
    buckets = dist["buckets"]
    xs = [b["low"] + 0.5 for b in buckets]
    ps = [b["prob"] * 100 for b in buckets]
    labels = [f"{b['low']}–{b['high']} °C" for b in buckets]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=xs, y=ps, width=0.92, marker_color=BLUE, opacity=0.75,
        customdata=labels, name="Probabilidade",
        text=[f"{p:.0f}%" if p >= 4 else "" for p in ps], textposition="outside",
        hovertemplate="Faixa %{customdata}: %{y:.1f}%<extra></extra>"))

    med = dist["quantiles"][50]
    fig.add_shape(type="line", x0=med, x1=med, y0=0, y1=1, yref="paper",
                  line=dict(color=RED, width=1.6, dash="dash"))
    fig.add_annotation(x=med, y=1, yref="paper", text=f"mediana {med:.1f} °C",
                       showarrow=False, xanchor="left", yanchor="top",
                       font=dict(color=RED, size=11))
    if taf_tx is not None:
        fig.add_shape(type="line", x0=taf_tx, x1=taf_tx, y0=0, y1=1, yref="paper",
                      line=dict(color=GREEN, width=1.6, dash="dot"))
        fig.add_annotation(x=taf_tx, y=0.92, yref="paper", text=f"TAF {taf_tx:.0f} °C",
                           showarrow=False, xanchor="left",
                           font=dict(color=GREEN, size=11))
    if det_points:
        fig.add_trace(go.Scatter(
            x=list(det_points.values()), y=[0] * len(det_points),
            mode="markers", name="Modelos determinísticos",
            marker=dict(symbol="triangle-up", size=12, color=ORANGE),
            customdata=[config.MODEL_LABELS.get(m, m) for m in det_points],
            hovertemplate="%{customdata}: %{x:.1f} °C<extra></extra>"))

    fig.update_layout(
        xaxis_title="Faixa da máxima (°C)",
        yaxis_title="Probabilidade (%)",
        showlegend=False,
        margin=dict(l=10, r=10, t=30, b=10),
        height=340,
        yaxis_range=[0, max(ps) * 1.25 + 2],
    )
    fig.update_xaxes(tickvals=[b["low"] for b in buckets] + [buckets[-1]["high"]])
    return fig


def prob_table(dist: dict) -> pd.DataFrame:
    return pd.DataFrame(
        {"Faixa": [f"{b['low']}–{b['high']} °C" for b in dist["buckets"]],
         "Prob.": [f"{b['prob'] * 100:.1f}%" for b in dist["buckets"]]})


def exceed_table(dist: dict) -> pd.DataFrame:
    items = sorted(dist["exceed"].items())
    return pd.DataFrame(
        {"Limiar": [f"≥ {t} °C" for t, _ in items],
         "Prob.": [f"{p * 100:.1f}%" for _, p in items]})


def det_table(det: dict) -> pd.DataFrame:
    return pd.DataFrame(
        {"Modelo": [config.MODEL_LABELS.get(m, m) for m in det],
         "Bruto": [f"{v['raw']:.1f}" for v in det.values()],
         "Viés": [f"{v['bias']:+.1f}" for v in det.values()],
         "Corrigido": [f"{v['corrected']:.1f}" for v in det.values()]})


def day_section(label: str, date, dist: dict, det: dict, taf_tx) -> None:
    q = dist["quantiles"]
    st.subheader(f"{label} — {date.strftime('%A, %d/%m/%Y')}")
    taf_note = f" · TAF TX: **{taf_tx:.0f} °C**" if taf_tx is not None else ""
    st.markdown(
        f"Máxima esperada: **{q[50]:.1f} °C** "
        f"(P10–P90: {q[10]:.1f} a {q[90]:.1f} °C){taf_note}")
    det_points = {m: v["corrected"] for m, v in det.items()}
    st.plotly_chart(fig_distribution(dist, det_points, taf_tx),
                    width="stretch")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**Probabilidade por faixa**")
        st.dataframe(prob_table(dist), hide_index=True, width="stretch")
    with c2:
        st.markdown("**Probabilidade de exceder**")
        st.dataframe(exceed_table(dist), hide_index=True, width="stretch")
    with c3:
        st.markdown("**Modelos determinísticos (Tmax °C)**")
        st.dataframe(det_table(det), hide_index=True, width="stretch")
    st.caption(
        f"Base: {dist['n_members']} membros de ensemble (ECMWF ENS + GEFS), "
        "corrigidos de viés e suavizados pelo erro residual histórico.")


def main() -> None:
    head, btn = st.columns([5, 1], vertical_alignment="center")
    with head:
        st.title(f"🌡️ Previsão de TMax — {config.STATION} (Guarulhos)")
    with btn:
        if st.button("🔄 Atualizar", type="primary", width="stretch"):
            load_context.clear()
            st.rerun()

    try:
        ctx = load_context()
    except RuntimeError as exc:
        st.error(f"Sem dados suficientes: {exc}. Tente atualizar em alguns minutos.")
        st.stop()

    age_min = (dt.datetime.now(pipeline.TZ) - ctx["generated"]).total_seconds() / 60
    st.caption(
        f"Dados coletados em {ctx['generated'].strftime('%d/%m/%Y %H:%M %Z')} "
        f"(há {age_min:.0f} min) · cache de 10 min — use o botão para forçar nova coleta · "
        f"Verdade terrestre: METAR de {config.STATION} · Fontes: Open-Meteo, "
        "aviationweather.gov, arquivo IEM")

    if ctx["latest_metar"]:
        m = ctx["latest_metar"]
        cols = st.columns(3)
        cols[0].metric("Último METAR", f"{m['temp']:.0f} °C",
                       help=m["raw"], delta=m["time"].strftime("%d/%m %H:%M"),
                       delta_color="off")
        if ctx["obs_max_today"] is not None:
            cols[1].metric("Máx. já observada hoje", f"{ctx['obs_max_today']:.0f} °C")
        cols[2].metric("Mediana D0 / D+1",
                       f"{ctx['dist_d0']['quantiles'][50]:.1f} / "
                       f"{ctx['dist_d1']['quantiles'][50]:.1f} °C")

    if ctx["nowcast"]:
        nc = ctx["nowcast"]
        direction = "mais quente" if nc["offset"] > 0 else "mais frio"
        st.info(
            f"📡 **Nowcast:** nas últimas {nc['n_hours']}h o observado está "
            f"**{abs(nc['offset']):.1f} °C {direction}** que o ensemble corrigido "
            f"→ ajuste de **{nc['shift']:+.1f} °C** aplicado às horas restantes de hoje.")

    st.subheader("Trajetória horária (hoje e amanhã)")
    st.plotly_chart(fig_hourly(ctx), width="stretch")
    with st.expander("ℹ️ O que são P10, P90, ensemble bruto e corrigido?"):
        st.markdown(
            "A previsão não vem de um modelo só: são "
            "**82 cenários (membros)** — ECMWF ENS (51) + GEFS (31) — rodados "
            "com pequenas variações nas condições iniciais. Em cada hora, "
            "ordenamos os 82 valores:\n\n"
            "- **P10** — 10% dos cenários estão *abaixo* desse valor "
            "(90% acima). É o lado frio do plausível.\n"
            "- **P90** — 90% dos cenários estão *abaixo* (10% acima). "
            "O lado quente do plausível.\n"
            "- **Banda P10–P90** — concentra os **80% centrais** dos cenários; "
            "quanto mais larga, mais incerta a previsão naquela hora.\n"
            "- **Ensemble bruto** — mediana dos 82 membros exatamente como "
            "saem dos modelos, sem nenhum ajuste.\n"
            "- **Ensemble corrigido** — mediana após subtrair o **viés** que "
            "cada modelo mostrou em SBGR nos últimos "
            f"{config.BIAS_LOOKBACK_DAYS} dias e, nas horas restantes de "
            "hoje, somar o ajuste do **nowcast** (desvio observado nos "
            "METARs das últimas horas). É a linha em que o painel confia.")

    day_section("D0 · Hoje", ctx["d0"], ctx["dist_d0"],
                ctx["det_corrected"]["d0"], ctx["taf_tx_d0"])
    day_section("D+1 · Amanhã", ctx["d1"], ctx["dist_d1"],
                ctx["det_corrected"]["d1"], ctx["taf_tx_d1"])

    if ctx["taf"]:
        with st.expander("TAF (meteorologista da estação)"):
            st.code(ctx["taf"])
            for t in ctx["taf_tx"]:
                st.markdown(
                    f"- Máxima prevista (TX): **{t['temp']} °C** — dia "
                    f"{t['local_date'].strftime('%d/%m')}, válido "
                    f"~{t['valid_local'].strftime('%Hh')} local")

    with st.expander(f"Viés aprendido por modelo (últimos {config.BIAS_LOOKBACK_DAYS} dias)"):
        bias_df = pd.DataFrame(
            {"Modelo": [config.MODEL_LABELS.get(b.get("model", fam), fam)
                        for fam, b in sorted(ctx["bias"].items())],
             "Viés médio": [f"{b['bias']:+.2f}" for _, b in sorted(ctx["bias"].items())],
             "Desvio residual": [f"{b['resid_std']:.2f}" for _, b in sorted(ctx["bias"].items())],
             "MAE": [f"{b['mae']:.2f}" for _, b in sorted(ctx["bias"].items())],
             "Dias": [b["n_days"] for _, b in sorted(ctx["bias"].items())]})
        st.dataframe(bias_df, hide_index=True, width="stretch")
        st.caption("Viés = previsto − observado (METAR). Valor positivo → o modelo "
                   "superestima a máxima em SBGR e a correção subtrai esse valor.")


main()
