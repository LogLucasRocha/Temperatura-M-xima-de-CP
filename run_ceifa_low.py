"""Relatório diário da Ceifa na temperatura MÍNIMA (lowest) — monitoramento.

Réplica do run_ceifa_f.py, mas lendo o lago dados_low/ (mínima). Roda no mesmo
cron das 06:00, mandando uma mensagem apartada. Modo observação: não aposta.

Uso local: python run_ceifa_low.py [--no-telegram]
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

from tmax import backtest, ceifa, config, notify

ARCHIVE = config.ROOT / "dados_low"
TITULO = "❄️ <b>Ceifa Mínima — monitoramento (nossos snapshots)</b>"
NOTA = ("<i>Temperatura MÍNIMA (lowest) das cidades que a Polymarket cobre — "
        "observação, ainda sem apostar. H-1 = hora antes do mínimo previsto.</i>")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-telegram", action="store_true")
    args = ap.parse_args()

    log = lambda msg: print(msg, flush=True)  # noqa: E731
    st = ceifa.simulate(log, archive=ARCHIVE)
    text = backtest.ceifa_report_text(st, titulo=TITULO, nota=NOTA)
    if st["n"]:
        parts = [f"{k} {v[1] / v[0]:.0%} (n={v[0]})"
                 for k, v in sorted(st["by_city"].items(),
                                    key=lambda kv: -kv[1][0])[:6]]
        text += "\n<i>Top cidades:</i> " + " · ".join(parts)

    print("\n" + text.replace("<b>", "").replace("</b>", "")
          .replace("<i>", "").replace("</i>", ""))

    token = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not args.no_telegram and token and chat_id:
        notify.send_message(token, chat_id, text)
        print("[telegram] relatório da Ceifa Mínima enviado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
