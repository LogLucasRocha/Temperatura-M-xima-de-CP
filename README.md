# Previsão de TMax — Guarulhos, Buenos Aires e Moscou, D0 e D+1

Pipeline que combina múltiplos modelos numéricos, ensembles, correção de viés
por estação e observações em tempo real para gerar uma **distribuição de
probabilidade** da temperatura máxima do dia — a lógica dos traders de
clima: não esperar a próxima rodada de modelo, e sim atualizar a estimativa
com o que a estação já observou.

Estações suportadas (em `tmax/config.py`):

- **SBGR** — Guarulhos (mercado de São Paulo)
- **SAEZ** — Ministro Pistarini/Ezeiza (o mercado de Buenos Aires do
  Polymarket resolve pela estação de Ezeiza via Wunderground, que é o
  METAR de SAEZ, em graus inteiros)
- **UUWW** — Moscou/Vnukovo (o mercado de Moscou resolve pela coluna
  "Temp" do weather.gov/wrh/timeseries, que é o METAR de UUWW, em °C)

## Uso

```
pip install -r requirements.txt
python run_report.py [--station SBGR|SAEZ]
```

Isso gera `reports/relatorio_<ICAO>_AAAA-MM-DD_HHMM.html` (e
`reports/latest_<ICAO>.html`), abre no navegador e imprime um resumo no
console. Opções:

- `--station SAEZ` — gera para Buenos Aires/Ezeiza (padrão: SBGR)
- `--no-open` — não abre o navegador ao final
- `--force-bias` — recalcula a correção de viés ignorando o cache diário

Ou dê dois cliques em `gerar_relatorio.bat`.

### Painel interativo (Streamlit)

```
python main.py
```

(ou `streamlit run app.py`, ou dois cliques em `abrir_painel.bat`)

Mesmos dados, mas com **uma aba por aeroporto** (🇧🇷 SBGR e 🇦🇷 SAEZ) e
gráficos Plotly — passe o mouse para ver a temperatura exata (observado,
mediana, P10 e P90) em cada hora — e botão **🔄 Atualizar** que refaz a
coleta na hora. Os dados ficam em cache por 10 minutos, por estação.

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
   de cada modelo no ponto da estação. Recalculado 1x/dia (cache em
   `data/bias_cache_<ICAO>.json`).
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
send_telegram.py       digest para o Telegram (roda no GitHub Actions)
tmax/config.py         estações (Station), modelos, parâmetros ajustáveis
tmax/fetch.py          coleta (METAR, TAF, IEM, Open-Meteo)
tmax/bias.py           correção de viés com cache diário
tmax/distribution.py   nowcast + mistura probabilística
tmax/pipeline.py       coleta + cálculo compartilhados (contexto da previsão)
tmax/report.py         gráficos matplotlib e HTML do relatório estático
tmax/notify.py         mensagens e gráficos do Telegram
tmax/polymarket.py     posições da carteira e odds dos mercados
data/                  caches gerados em runtime (viés, estado do digest)
reports/               relatórios gerados
```

## Bot do Telegram (comandos)

O `send_telegram.py` roda a cada 10 min no GitHub Actions (`main.yml`) e, além
de mandar os alertas, agora **lê os comandos e cliques de botão** (getUpdates)
na mesma rodada — então a resposta chega com latência de **até ~10 min** (não
há servidor sempre ligado; foi a opção escolhida para não exigir infra nova).

Um **alerta de compra novo** (edge ou colheita) chega com o **bloco completo**
da cidade — tabela mercado × modelo, gráfico e hora a hora, o contexto da
decisão de entrada. As **repetições** (a cada rodada, enquanto a oportunidade
dura) vêm sozinhas em texto curto, para não repetir gráficos. Além disso, você
pode pedir o relatório de qualquer cidade a qualquer momento pelo comando
abaixo.

Comandos (também no menu “/” do Telegram):

- `/relatorio <cidade>` — relatório completo de qualquer cidade, por ICAO
  (`/relatorio SBGR`) ou nome (`/relatorio Guarulhos`, sem depender de acento
  ou maiúscula). Aliases: `/rel`, `/report`, `/cidade`.
- `/cidades` — lista as cidades monitoradas com seus ICAOs.
- `/ajuda` — como usar o bot (`/start`, `/help` também servem).

O bot só responde ao chat configurado em `TELEGRAM_CHAT_ID` (ignora qualquer
outro). O offset dos updates é guardado em `data/digest_state.json`.

## Parâmetros para calibrar (em `tmax/config.py`)

- `BIAS_LOOKBACK_DAYS` (60) — janela de aprendizado do viés
- `NOWCAST_DAMPING` (0.7) — quanto do desvio observado propagar para a tarde
- `D1_STD_INFLATION` (1.15) — inflação de incerteza para amanhã

## Como ler o relatório do backtest (Telegram, a cada 3 dias)

O workflow `backtest.yml` reconstrói a projeção hora a hora de cada dia
arquivado (`backtest_data/`), aplica a regra de sinais de produção e simula
apostar 10% do capital em cada sinal. Linha a linha do relatório:

- **"Estratégia Edge — simulação histórica · N dias-cidade · M entradas"**
  — NÃO é contagem de alertas recebidos: é quantas entradas a regra TERIA
  feito reencenando o arquivo inteiro (1 aposta por faixa/dia, no primeiro
  cruzamento de edge ≥ 5 p.p. com confiança > 90%, só NÃO com preço ≥
  NAO_MIN_PRICE, dentro da janela local `SIGNAL_HOURS`).
- **"Acerto: X%"** — fração das apostas que resolveram a favor.
- **"modelo médio Y%"** — confiança média que o modelo DECLAROU no lado
  comprado. Compare com o acerto: se declara 99% e acerta 77%, o modelo é
  superconfiante; se os dois batem, está calibrado.
- **"preço médio 0.NN"** — quanto custou, em média, cada $1 de retorno
  potencial (0.74 = pagou 74 centavos por algo que paga $1 se acertar).
- **"P&L flat: +X.XXx"** — lucro somado apostando SEMPRE 10% do capital
  INICIAL (sem reinvestir). Métrica estável para comparar regras entre si.
- **"composto: X.XXx"** — o "quanto eu teria": reinvestindo (10% do capital
  corrente em cada aposta), o capital final em múltiplos do inicial.
- **"drawdown máx X%"** — a pior queda do pico ao vale da curva composta.
  É o quanto você precisaria aguentar ver sumir sem abandonar a regra.
- **"N faixas com resolução divergente"** — mercados que resolveram
  diferente do METAR arredondado (risco da fonte de resolução oficial).
- **"Por cidade / Por lado"** — nº de apostas (acerto, P&L flat de cada
  grupo). Lado NÃO = comprar o Não; SIM = comprar o Yes.
- **"📏 Confiança ≥ 90% no D0"** — de todas as faixas em que o modelo
  declarou ≥ 90% (mesmo sem sinal), quantas ele acertou, por cidade —
  o termômetro de calibração mais direto ("declarado" vs real).
- **"🎯 Calibração (Brier antes→depois)"** — erro quadrático médio das
  probabilidades (menor = melhor) antes e depois da curva de calibração,
  por período do dia; "blend" é o diagnóstico modelo+preço (quanto menor o
  peso `a` do modelo, menos ele adiciona ao que o preço já diz).

Ressalvas permanentes: a reconstrução usa o último preço negociado (mercados
finos de madrugada não executariam em tamanho); a calibração é reajustada
in-sample a cada rodada; e o arquivo cresce ~3 dias por execução, então os
números mudam conforme a história acumula.

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
