"""Junta as partições diárias de uma base num arquivo ÚNICO, sob demanda.

O arquivo permanente (dados/) é particionado por dia para o git ficar enxuto,
mas para análise isso já é uma base só (pandas/DuckDB leem a pasta inteira).
Este script existe só para quando você quer um arquivo avulso — por exemplo,
abrir no Excel ou mandar para alguém. NÃO commita nada.

Uso:
    python -m tmax.consolidar mercado              -> mercado_consolidado.parquet
    python -m tmax.consolidar previsao --xlsx       -> previsao_consolidado.xlsx
    python -m tmax.consolidar all                   -> todas as bases tabulares
    python -m tmax.consolidar mercado --icao SAEZ   -> só uma cidade
    python -m tmax.consolidar mercado --out /tmp/x.parquet

Dica de análise (sem consolidar): pandas lê a base inteira direto da pasta —
    import pandas as pd
    df = pd.read_parquet("dados/mercado/")     # tudo, todas cidades e dias
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from . import config

ARCHIVE = config.ROOT / "dados"
BASES = ["mercado", "previsao", "alertas", "stops"]


def carregar(base: str, icao: str | None = None) -> pd.DataFrame:
    """Lê todas as partições diárias de uma base como um único DataFrame."""
    root = ARCHIVE / base
    if icao and base in ("mercado", "previsao"):
        root = root / icao
    files = sorted(root.rglob("*.parquet")) if root.exists() else []
    if not files:
        return pd.DataFrame()
    df = pd.concat((pd.read_parquet(f) for f in files), ignore_index=True)
    if "ts_utc" in df.columns:
        df = df.sort_values("ts_utc").reset_index(drop=True)
    return df


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base", choices=BASES + ["all"])
    ap.add_argument("--xlsx", action="store_true",
                    help="salva em Excel (.xlsx) em vez de Parquet")
    ap.add_argument("--icao", help="filtra uma cidade (mercado/previsao)")
    ap.add_argument("--out", help="caminho de saída (padrão: <base>_consolidado.<ext>)")
    args = ap.parse_args()

    bases = BASES if args.base == "all" else [args.base]
    ext = "xlsx" if args.xlsx else "parquet"
    escreveu = 0
    for base in bases:
        df = carregar(base, args.icao)
        if df.empty:
            print(f"[{base}] sem dados ainda.")
            continue
        out = Path(args.out) if (args.out and len(bases) == 1) \
            else Path(f"{base}_consolidado.{ext}")
        if args.xlsx:
            df.to_excel(out, index=False)
        else:
            df.to_parquet(out, index=False)
        print(f"[{base}] {len(df)} linhas, "
              f"{df['ts_utc'].min() if 'ts_utc' in df else '?'} → "
              f"{df['ts_utc'].max() if 'ts_utc' in df else '?'}  ->  {out}")
        escreveu += 1
    return 0 if escreveu else 1


if __name__ == "__main__":
    sys.exit(main())
