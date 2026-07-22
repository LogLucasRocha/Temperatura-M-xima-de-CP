"""Captura ao vivo das cidades americanas (°F) em modo OBSERVAÇÃO.

NÃO envia nada ao Telegram. Só busca mercado + previsão + ensemble das cidades
de config.STATIONS_FAHRENHEIT e grava nos NOSSOS snapshots (dados/), para que a
Ceifa possa ser avaliada nelas sem apostar. Roda no mesmo job do digest
(.github/workflows/main.yml), compartilhando o mesmo buffer de captura
(data/capture no cache) — como os ICAOs são distintos, não colide com as
cidades ativas em °C.

Reaproveita os helpers do send_telegram (mesma coleta de mercado/previsão/
ensemble do digest), então a gravação sai idêntica à das cidades ativas.

Uso local:
    python capture_fahrenheit.py
"""
from __future__ import annotations

try:
    import truststore

    truststore.inject_into_ssl()
except Exception:
    pass

import concurrent.futures as cf
import sys

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tmax import capture, config, pipeline
import send_telegram as digest


def main() -> int:
    # Todas as cidades em OBSERVAÇÃO: °F (EUA) + o grupo novo em °C (Milão,
    # Wuhan, etc.). Mesmo mecanismo de captura; os relatórios filtram por grupo.
    stations = list({**config.STATIONS_FAHRENHEIT,
                     **config.STATIONS_OBSERVE}.values())
    contexts: dict = {}

    def _build(station):
        def log(msg: str, _s=station) -> None:
            print(f"[{_s.icao}] {msg}")
        return pipeline.build_context(station, log=log)

    with cf.ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_build, s): s for s in stations}
        for fut in cf.as_completed(futs):
            s = futs[fut]
            try:
                contexts[s.icao] = fut.result()
            except Exception as exc:  # noqa: BLE001 — uma cidade não derruba o resto
                print(f"[{s.icao}] ERRO: {exc}", file=sys.stderr)

    # Previsão derivada + ensemble bruto (dedup por ciclo) de cada cidade.
    for s in stations:
        c = contexts.get(s.icao)
        if c is not None:
            digest._cap(digest._capture_context, s, c)

    # Mercado: reaproveita a coleta do digest (busca o evento e grava as faixas
    # com preço). yes_prob é irrelevante para o arquivo/Ceifa, então passa None.
    digest._cap(digest._collect_signal_rows, stations, contexts, lambda *a: None)

    # Consolida os dias UTC já fechados em dados/*.parquet (idempotente).
    changed = digest._cap(capture.flush)
    if changed:
        print(f"[captura °F] consolidado: {changed}")
    print(f"[captura °F] {len(contexts)}/{len(stations)} cidades capturadas "
          "(modo observação, sem Telegram).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
