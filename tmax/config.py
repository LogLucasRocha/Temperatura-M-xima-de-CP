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

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def label(self) -> str:
        return f"{self.city} ({self.icao})"

    @property
    def bias_cache_file(self) -> Path:
        return DATA_DIR / f"bias_cache_{self.icao}.json"


STATIONS = {
    "SBGR": Station(
        icao="SBGR", city="Guarulhos",
        airport="Aeroporto Internacional de São Paulo/Guarulhos",
        flag="🇧🇷", lat=-23.4356, lon=-46.4731,
        timezone="America/Sao_Paulo"),
    # Mercado do Polymarket de Buenos Aires resolve pela estação de Ezeiza
    # (Wunderground "Minister Pistarini Intl Airport" = METAR de SAEZ)
    "SAEZ": Station(
        icao="SAEZ", city="Buenos Aires",
        airport="Aeroporto Ministro Pistarini (Ezeiza)",
        flag="🇦🇷", lat=-34.8222, lon=-58.5358,
        timezone="America/Argentina/Buenos_Aires"),
    # Mercado do Polymarket de Moscou resolve pela coluna "Temp" de
    # weather.gov/wrh/timeseries?site=UUWW (NOAA) = METAR de Vnukovo, em °C
    "UUWW": Station(
        icao="UUWW", city="Moscou",
        airport="Aeroporto Internacional de Moscou/Vnukovo",
        flag="🇷🇺", lat=55.5915, lon=37.2615,
        timezone="Europe/Moscow"),
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

# Sinal de edge: divergência mínima |projetado − mercado| numa faixa do dia
# operável (D0; D+1 quando a máxima de hoje já travou) que dispara a mensagem
# de alerta. Cada faixa avisa uma vez ao cruzar o corte e re-arma quando cai
# abaixo dele (ou na virada do dia).
EDGE_ALERT_MIN = 0.05

# Confiança mínima do lado indicado pelo sinal: só alerta quando a projeção
# dá mais de 90% de chance de a aposta sugerida acertar (P(Yes) se o Yes está
# barato; 1 − P(Yes) se está caro).
EDGE_MIN_CONFIDENCE = 0.90

# Janela LOCAL em que sinais podem ser enviados. Fora dela (madrugada) o
# mercado é fino demais para executar e o backtest mostrou que o edge é
# ilusório: cruzamentos fora da janela são consumidos em silêncio — não
# ficam represados esperando a janela abrir.
SIGNAL_HOURS = (6, 23)

# Inflação de incerteza para D+1 (erro cresce com o horizonte)
D1_STD_INFLATION = 1.15

USER_AGENT = "tmax-pipeline/1.0 (uso pessoal, previsao de temperatura)"
