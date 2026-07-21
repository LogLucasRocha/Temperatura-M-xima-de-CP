"""Relatório diário SEPARADO da Ceifa nas cidades americanas (°F), em modo
MONITORAMENTO — só para acompanhar a performance, sem apostar.

Mesma mecânica do run_ceifa.py (entrada em H-1, banda 0,95–0,995, stop fiel à
execução, banca de 10% sem alavancar), mas restrito a config.STATIONS_FAHRENHEIT
e com cabeçalho próprio. Roda pelo mesmo cron do relatório das ativas, mandando
uma segunda mensagem apartada.

Uso local:
    python run_ceifa_f.py [--no-telegram]
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

TITULO = "🇺🇸 <b>Ceifa °F — monitoramento (nossos snapshots)</b>"
NOTA = ("<i>Cidades americanas em °F, em observação — ainda NÃO apostamos "
        "nelas. Acompanhando a assertividade antes de decidir se entram.</i>")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-telegram", action="store_true",
                    help="não envia o relatório (só imprime)")
    args = ap.parse_args()

    log = lambda msg: print(msg, flush=True)  # noqa: E731
    st = ceifa.simulate(log, icaos=set(config.STATIONS_FAHRENHEIT))
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
        print("[telegram] relatório da Ceifa °F enviado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
