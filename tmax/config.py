"""Configuração central do pipeline de previsão de temperatura máxima."""
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

# Diretórios
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"


@dataclass(frozen=True)
class Station:
    """Aeroporto cujo METAR é a verdade terrestre do mercado de temperatura."""

    icao: str
    city: str      # nome curto usado em títulos e abas
    airport: str   # nome completo do aeroporto
    flag: str      # emoji da bandeira, para as abas do painel
    lat: float
    lon: float
    timezone: str
    unit: str = "C"   # unidade em que o mercado da cidade resolve (C ou F)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def label(self) -> str:
        return f"{self.city} ({self.icao})"

    @property
    def bias_cache_file(self) -> Path:
        return DATA_DIR / f"bias_cache_{self.icao}.json"


# A estação de cada cidade vem da DESCRIÇÃO oficial do mercado no Polymarket
# (todas citam o ICAO na URL de resolução — Wunderground ou NOAA timeseries).
# check_resolution_sources() no backtest confere periodicamente se o ICAO
# ainda aparece na descrição. Hong Kong ficou de fora de propósito: resolve
# pelo Observatório de HK, que não é estação METAR.
STATIONS = {
    "SBGR": Station("SBGR", "Guarulhos", "São Paulo/Guarulhos Intl",
                    "🇧🇷", -23.4356, -46.4731, "America/Sao_Paulo"),
    "SAEZ": Station("SAEZ", "Buenos Aires", "Ministro Pistarini (Ezeiza)",
                    "🇦🇷", -34.8222, -58.5358,
                    "America/Argentina/Buenos_Aires"),
    "UUWW": Station("UUWW", "Moscou", "Moscou/Vnukovo Intl",
                    "🇷🇺", 55.5915, 37.2615, "Europe/Moscow"),
    "CYYZ": Station("CYYZ", "Toronto", "Toronto Pearson Intl",
                    "🇨🇦", 43.6772, -79.6306, "America/Toronto"),
    "MMMX": Station("MMMX", "Cidade do México", "Benito Juárez Intl",
                    "🇲🇽", 19.4363, -99.0721, "America/Mexico_City"),
    "EGLC": Station("EGLC", "Londres", "London City",
                    "🇬🇧", 51.5053, 0.0553, "Europe/London"),
    "LFPB": Station("LFPB", "Paris", "Paris-Le Bourget",
                    "🇫🇷", 48.9694, 2.4414, "Europe/Paris"),
    "LEMD": Station("LEMD", "Madri", "Adolfo Suárez Madrid-Barajas",
                    "🇪🇸", 40.4719, -3.5626, "Europe/Madrid"),
    "EHAM": Station("EHAM", "Amsterdã", "Schiphol",
                    "🇳🇱", 52.3086, 4.7639, "Europe/Amsterdam"),
    "EPWA": Station("EPWA", "Varsóvia", "Warsaw Chopin",
                    "🇵🇱", 52.1657, 20.9671, "Europe/Warsaw"),
    "LTFM": Station("LTFM", "Istambul", "Istanbul Airport",
                    "🇹🇷", 41.2753, 28.7519, "Europe/Istanbul"),
    "LTAC": Station("LTAC", "Ancara", "Esenboğa Intl",
                    "🇹🇷", 40.1281, 32.9951, "Europe/Istanbul"),
    "RKSI": Station("RKSI", "Seul", "Incheon Intl",
                    "🇰🇷", 37.4692, 126.4505, "Asia/Seoul"),
    "RJTT": Station("RJTT", "Tóquio", "Haneda",
                    "🇯🇵", 35.5533, 139.7811, "Asia/Tokyo"),
    "ZBAA": Station("ZBAA", "Pequim", "Beijing Capital Intl",
                    "🇨🇳", 40.0801, 116.5846, "Asia/Shanghai"),
    "ZSPD": Station("ZSPD", "Xangai", "Shanghai Pudong Intl",
                    "🇨🇳", 31.1434, 121.8052, "Asia/Shanghai"),
    "WSSS": Station("WSSS", "Singapura", "Changi",
                    "🇸🇬", 1.3502, 103.9944, "Asia/Singapore"),
    "NZWN": Station("NZWN", "Wellington", "Wellington Intl",
                    "🇳🇿", -41.3272, 174.8053, "Pacific/Auckland"),
}

# Cidades que resolvem em FAHRENHEIT — catalogadas mas fora de operação por
# decisão do Lucas (12/07/2026). Para reativar, mova para STATIONS.
STATIONS_FAHRENHEIT = {
    "KLGA": Station("KLGA", "Nova York", "LaGuardia",
                    "🇺🇸", 40.7772, -73.8726, "America/New_York", unit="F"),
    "KORD": Station("KORD", "Chicago", "O'Hare Intl",
                    "🇺🇸", 41.9786, -87.9048, "America/Chicago", unit="F"),
    "KMIA": Station("KMIA", "Miami", "Miami Intl",
                    "🇺🇸", 25.7932, -80.2906, "America/New_York", unit="F"),
    "KLAX": Station("KLAX", "Los Angeles", "Los Angeles Intl",
                    "🇺🇸", 33.9425, -118.4081, "America/Los_Angeles",
                    unit="F"),
    "KDAL": Station("KDAL", "Dallas", "Dallas Love Field",
                    "🇺🇸", 32.8471, -96.8518, "America/Chicago", unit="F"),
    "KATL": Station("KATL", "Atlanta", "Hartsfield-Jackson Intl",
                    "🇺🇸", 33.6367, -84.4281, "America/New_York", unit="F"),
    "KBKF": Station("KBKF", "Denver", "Buckley SFB (Aurora)",
                    "🇺🇸", 39.7017, -104.7517, "America/Denver", unit="F"),
    "KHOU": Station("KHOU", "Houston", "William P. Hobby",
                    "🇺🇸", 29.6454, -95.2789, "America/Chicago", unit="F"),
    "KSEA": Station("KSEA", "Seattle", "Seattle-Tacoma Intl",
                    "🇺🇸", 47.4489, -122.3094, "America/Los_Angeles",
                    unit="F"),
}

DEFAULT_STATION = STATIONS["SBGR"]

# Modelos determinísticos (Open-Meteo) -> família usada na correção de viés
DET_MODELS = {
    "ecmwf_ifs025": "ecmwf",
    "gfs_seamless": "gfs",
    "icon_seamless": "icon",
    "gem_seamless": "gem",
    "ecmwf_aifs025_single": "aifs",
}

# Nomes amigáveis para o relatório
MODEL_LABELS = {
    "ecmwf_ifs025": "ECMWF IFS",
    "gfs_seamless": "NOAA GFS",
    "icon_seamless": "DWD ICON",
    "gem_seamless": "CMC GEM",
    "ecmwf_aifs025_single": "ECMWF AIFS (IA)",
}

# Modelos de ensemble (Open-Meteo ensemble API) -> família de viés
ENS_MODELS = {
    "ecmwf_ifs025": "ecmwf",   # ENS: 51 membros
    "gfs05": "gfs",            # GEFS: 31 membros
}

# A API canonicaliza os nomes dos modelos nas chaves da resposta
ENS_RESPONSE_ALIASES = {
    "ecmwf_ifs025_ensemble": "ecmwf_ifs025",
    "ncep_gefs05": "gfs05",
}

# Correção de viés
BIAS_LOOKBACK_DAYS = 60          # janela de histórico para aprender o viés
BIAS_CACHE_MAX_AGE_HOURS = 24    # recalcula 1x/dia
MIN_OBS_PER_DAY = 18             # mínimo de METARs no dia para validar a máxima observada

# Nowcast intradiário
NOWCAST_DAMPING = 0.7            # fração do desvio observado aplicada às horas restantes
NOWCAST_HOURS = 3                # quantas horas recentes usar no cálculo do desvio

# Máxima do dia considerada "travada" quando as últimas N horas observadas
# ficaram todas abaixo dela (o pico passou); o digest corta o hora a hora
# restante de hoje.
TMAX_LOCK_HOURS = 3

# Estado do último digest enviado (para omitir estações sem novidade)
DIGEST_STATE_FILE = DATA_DIR / "digest_state.json"

# Estratégia Edge PAUSADA (decisão do Lucas, 14/07): foco só na colheita.
# Com False, o digest não computa nem envia sinais de edge; a colheita e o
# resto (posições, stop, avisos de condição, captura) seguem normais. A captura
# de mercado/previsão continua, então dá para reconstruir o edge depois. Para
# reativar, volte para True.
EDGE_ENABLED = False

# Sinal de edge: divergência mínima |projetado − mercado| numa faixa do dia
# operável (D0; D+1 quando a máxima de hoje já travou) que dispara a mensagem
# de alerta. Cada faixa avisa uma vez ao cruzar o corte e re-arma quando cai
# abaixo dele (ou na virada do dia).
EDGE_ALERT_MIN = 0.05

# NOTA (13/07): testamos um TETO de 20 p.p. ("edge grande demais = erro do
# modelo") e o backtest o rejeitou — no lado NÃO já filtrado, gap grande é o
# modelo vendo a máxima da tarde antes do mercado, não erro. Ver o histórico
# do git / relatório da sessão. Mantido sem teto de propósito.

# Confiança mínima do lado indicado pelo sinal: só alerta quando a projeção
# dá mais de 90% de chance de a aposta sugerida acertar (P(Yes) se o Yes está
# barato; 1 − P(Yes) se está caro).
EDGE_MIN_CONFIDENCE = 0.90

# Janela LOCAL em que sinais podem ser enviados. Fora dela (madrugada) o
# mercado é fino demais para executar e o backtest mostrou que o edge é
# ilusório: cruzamentos fora da janela são consumidos em silêncio — não
# ficam represados esperando a janela abrir.
SIGNAL_HOURS = (6, 23)

# Lados operados nos sinais de entrada: o backtest de 18 cidades mostrou 90%
# de acerto no NÃO contra 41% no SIM (perfil de loteria) — só o NÃO notifica.
SIGNAL_SIDES = ("NAO",)

# Preço mínimo do NÃO para sinalizar: NÃO abaixo disso é brigar com um
# mercado quase-certo do Yes — a autópsia mostrou que esses casos são
# cara-ou-coroa com book fino (Moscou 08/07 perdeu a $0.04; SAEZ 27/06
# ganhou a $0.08 por sorte). Com o filtro: 92% de acerto, drawdown 1.5%.
NAO_MIN_PRICE = 0.30

# Stop loss: alerta quando o mercado precifica a posição este percentual (ou
# mais) abaixo do preço médio de entrada — repetido a cada rodada enquanto
# durar. No backtest, a saída simulada acontece a STOP_EXIT_FRAC de perda.
STOP_ALERT_FRAC = 0.10
STOP_EXIT_FRAC = 0.15

# Alertas de condição observada (platô "andou de lado" e fuga do envelope do
# ensemble "acima do teto / abaixo do piso"). DESLIGADOS (decisão do Lucas,
# 16/07). Para reativar, True.
COND_ALERTS_ENABLED = False

# ---------------------------------------------------------------- Ceifa
# Estratégia ATIVA (decisão do Lucas, 15/07) e a ÚNICA no momento: comprar o
# NÃO quando o mercado já está quase-certo, com o preço do NÃO nesta faixa —
# a análise da base de mercado mostrou o mercado ~100% assertivo aí. Critério
# é SÓ o preço (sem filtro de hora nem de modelo). O alerta REPETE a cada
# rodada até você ter posição naquele contrato; assim que a carteira mostra a
# entrada, para de alertar aquele contrato. Assertividade = preço do NÃO
# convergindo para 1,0 (o NÃO resolveu).
CEIFA_ENABLED = True
CEIFA_PRICE_MIN = 0.95      # exclusivo: preço do NÃO > 0,95
CEIFA_PRICE_MAX = 0.995     # exclusivo: preço do NÃO < 0,995

# Colheita de favoritos: APOSENTADA (decisão do Lucas 15/07 — substituída pela
# Ceifa). Mantida no código, desligada por HARVEST_ENABLED. Parâmetros antigos
# preservados só para o backtest histórico de comparação.
HARVEST_ENABLED = False
HARVEST_PRICE_MIN = 0.97
HARVEST_PRICE_MAX = 0.995
HARVEST_MIN_HOUR = 16
HARVEST_MIN_CONF = 0.85

# Com 25 cidades, o bloco completo (posições+tabela+gráfico+hora a hora) só
# é enviado para cidades com ATIVIDADE (posição aberta ou sinal na rodada);
# as demais são monitoradas em silêncio. False = comportamento antigo.
FULL_BLOCK_ONLY_WITH_ACTIVITY = True

# Inflação de incerteza para D+1 (erro cresce com o horizonte)
D1_STD_INFLATION = 1.15

USER_AGENT = "tmax-pipeline/1.0 (uso pessoal, previsao de temperatura)"
