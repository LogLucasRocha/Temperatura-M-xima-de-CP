"""Relatório diário da estratégia Ceifa no Telegram.

Roda pelo .github/workflows/ceifa_report.yml (cron 09:00 UTC = 06:00 em
Brasília). Simula a Ceifa sobre o arquivo histórico (backtest_data/) e manda os
quatro números pedidos: quantidade de testes, assertividade, rendimento e
drawdown. Não coleta dados novos nem recalibra — só lê o arquivo atual.

Uso local:
    python run_ceifa.py [--no-telegram]
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

from tmax import backtest, ceifa, notify


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-telegram", action="store_true",
                    help="não envia o relatório (só imprime)")
    args = ap.parse_args()

    log = lambda msg: print(msg, flush=True)  # noqa: E731
    # Backtest SÓ nos nossos snapshots capturados (dados/), como pedido —
    # nada do arquivo reconstruído de APIs.
    st = ceifa.simulate(log)
    text = backtest.ceifa_report_text(st)
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
        if st.get("per_day"):
            try:
                notify.send_photo(
                    token, chat_id, notify.equity_chart_png(st["per_day"]),
                    "📈 Evolução patrimonial da Ceifa (base R$100, sem alavancar)")
            except Exception as exc:  # noqa: BLE001 — gráfico é acessório
                print(f"[telegram] falha no gráfico: {exc}", file=sys.stderr)
        print("[telegram] relatório da Ceifa enviado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
