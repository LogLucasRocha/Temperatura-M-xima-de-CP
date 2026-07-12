"""Roda o backtest de sinais: arquiva os dias que faltam, simula a regra de
produção sobre o arquivo inteiro e (opcionalmente) manda o relatório pro
Telegram.

Uso local:
    python run_backtest.py [--no-harvest] [--no-telegram]

Na nuvem roda pelo GitHub Actions (.github/workflows/backtest.yml) a cada
3 dias; o workflow commita de volta os dias novos de backtest_data/ — é isso
que estende o arquivo além das janelas de retenção das fontes (ensemble
Open-Meteo: ~92 dias).
"""
from __future__ import annotations

try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

import argparse
import os
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tmax import backtest, notify


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-harvest", action="store_true",
                    help="não coleta dias novos; só simula o arquivo atual")
    ap.add_argument("--no-telegram", action="store_true",
                    help="não envia o relatório (só imprime)")
    args = ap.parse_args()

    log = lambda msg: print(msg, flush=True)  # noqa: E731

    if not args.no_harvest:
        added = backtest.harvest(log)
        log(f"harvest: {added} dia(s) novo(s) arquivado(s).")

    # Reajusta a curva de calibração com o arquivo atualizado; a simulação
    # (e a produção) usam a probabilidade calibrada.
    cal = backtest.fit_calibration(log)
    stats = backtest.simulate(log)
    text = backtest.report_text(stats)
    if cal:
        partes = [f"{nome} {s['brier_raw']:.3f}→{s['brier_cal']:.3f}"
                  for nome, s in sorted(cal.items()) if "brier_raw" in s]
        partes += [f"{nome} {s['brier_cal']:.3f}→{s['brier_post']:.3f}"
                   for nome, s in sorted(cal.items())
                   if nome.startswith("blend")]
        text += ("\n🎯 <b>Calibração reajustada</b> (Brier antes→depois): "
                 + " · ".join(partes))
    print("\n" + text.replace("<b>", "").replace("</b>", "")
          .replace("&amp;", "&").replace("&gt;", ">"))

    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not args.no_telegram and token and chat_id:
        notify.send_message(token, chat_id, text)
        print("[telegram] relatório enviado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
