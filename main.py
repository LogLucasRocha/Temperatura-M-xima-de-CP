"""Abre o painel interativo da previsão de Tmax em SBGR.

Uso:
    python main.py

Equivalente a `streamlit run app.py`: sobe o servidor local e abre o painel
no navegador automaticamente. Ctrl+C no terminal encerra.
"""
import sys
from pathlib import Path

from streamlit.web import cli as stcli

APP = Path(__file__).resolve().parent / "app.py"

if __name__ == "__main__":
    sys.argv = ["streamlit", "run", str(APP)]
    sys.exit(stcli.main())
