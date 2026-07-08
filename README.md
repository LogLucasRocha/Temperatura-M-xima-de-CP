# Previsão de TMax — SBGR (Guarulhos), D0 e D+1

Pipeline que combina múltiplos modelos numéricos, ensembles, correção de viés
por estação e observações em tempo real para gerar uma **distribuição de
probabilidade** da temperatura máxima do dia em SBGR — a lógica dos traders de
clima: não esperar a próxima rodada de modelo, e sim atualizar a estimativa
com o que a estação já observou.

## Uso

```
pip install -r requirements.txt
python run_report.py
```

Isso gera `reports/relatorio_AAAA-MM-DD_HHMM.html` (e `reports/latest.html`),
abre no navegador e imprime um resumo no console. Opções:

- `--no-open` — não abre o navegador ao final
- `--force-bias` — recalcula a correção de viés ignorando o cache diário

Ou dê dois cliques em `gerar_relatorio.bat`.

### Painel interativo (Streamlit)

```
python main.py
```

(ou `streamlit run app.py`, ou dois cliques em `abrir_painel.bat`)

Mesmos dados, mas com gráficos Plotly — passe o mouse para ver a temperatura
exata (observado, mediana, P10 e P90) em cada hora — e botão **🔄 Atualizar**
que refaz a coleta na hora. Os dados ficam em cache por 10 minutos.

## O que o pipeline faz

1. **METAR/SPECI em tempo real** (aviationweather.gov) — a "verdade terrestre"
   que resolve a aposta. A máxima já observada no dia vira piso da distribuição.
2. **TAF** — extrai o grupo TX (máxima prevista pelo meteorologista da estação)
   e mostra como referência independente.
3. **Multi-modelo determinístico** (Open-Meteo): ECMWF IFS, GFS, ICON, GEM e
   ECMWF AIFS (IA), cada um com sua máxima corrigida de viés.
4. **Ensembles**: ECMWF ENS (51 membros) + GEFS (31 membros) → distribuição
   horária completa.
5. **Correção de viés ("MOS caseiro")**: compara as máximas previstas nos
   últimos 60 dias (API de histórico de previsões do Open-Meteo) com as máximas
   observadas nos METARs (arquivo da Iowa State) e aprende o erro sistemático
   de cada modelo no ponto de SBGR. Recalculado 1x/dia (cache em
   `data/bias_cache.json`).
6. **Nowcast intradiário**: mede o desvio entre o observado nas últimas horas e
   o ensemble corrigido, e desloca as horas restantes de hoje por uma fração
   amortecida desse desvio (com peso menor de manhã cedo, quando nevoeiro e
   resfriamento noturno enganam).
7. **Distribuição final**: cada membro de ensemble vira uma gaussiana centrada
   na sua máxima corrigida, com desvio igual ao erro residual histórico do
   modelo (inflado 15% para D+1, encolhido em D0 conforme o dia avança). A
   mistura dá quantis, probabilidade por faixa de 1 °C e probabilidade de
   exceder cada limiar — o formato dos buckets de mercado de previsão.

## Estrutura

```
main.py                abre o painel interativo (python main.py)
app.py                 o painel em si (Streamlit + Plotly)
run_report.py          relatório HTML estático
sbgr/config.py         coordenadas, modelos, parâmetros ajustáveis
sbgr/fetch.py          coleta (METAR, TAF, IEM, Open-Meteo)
sbgr/bias.py           correção de viés com cache diário
sbgr/distribution.py   nowcast + mistura probabilística
sbgr/pipeline.py       coleta + cálculo compartilhados (contexto da previsão)
sbgr/report.py         gráficos matplotlib e HTML do relatório estático
data/                  cache do viés
reports/               relatórios gerados
```

## Parâmetros para calibrar (em `sbgr/config.py`)

- `BIAS_LOOKBACK_DAYS` (60) — janela de aprendizado do viés
- `NOWCAST_DAMPING` (0.7) — quanto do desvio observado propagar para a tarde
- `D1_STD_INFLATION` (1.15) — inflação de incerteza para amanhã

## Limitações conhecidas / próximos passos

- O viés é uma média simples; separar por estação do ano, condição de céu e
  vento (gradient boosting) tende a melhorar.
- O nowcast é um deslocamento amortecido, não uma regressão treinada
  (Tmax ~ T das 9h/10h + nuvens + vento).
- Falta nowcasting de satélite (GOES-16) e radar para capar a máxima quando
  nebulosidade/chuva se aproxima — o METAR de nuvens já ajuda indiretamente
  via nowcast de temperatura.
- METAR brasileiro reporta temperatura em graus inteiros; confirme a regra de
  resolução do mercado (inteiro do METAR vs. décimos) antes de operar.
