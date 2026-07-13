# Documentação técnica (`docs/`)

- **`tmax.tex`** — documentação técnica e guia de estudo do pipeline (a
  matemática/estatística de cada etapa, com exercícios por seção e gabarito).
  É o único arquivo escrito à mão aqui.
- **`generated_numbers.tex`** — macros LaTeX com os números do último backtest
  (acerto, composto, drawdown de cada estratégia etc). **Gerado
  automaticamente** por [`tmax/results.py`](../tmax/results.py) a cada rodada do
  workflow de backtest; nunca editar à mão. É isso que mantém o documento
  sempre atualizado.
- **`tmax.pdf`** — compilado pelo workflow [`docs.yml`](../.github/workflows/docs.yml)
  sempre que o `.tex` ou os números mudam.

## Como compilar localmente

```
cd docs
latexmk -pdf tmax.tex        # requer uma distribuição TeX (TeX Live / MiKTeX)
```

Ou abra `tmax.tex` no [Overleaf](https://overleaf.com) (envie junto o
`generated_numbers.tex`).

## Resultados brutos

Os números vêm de [`backtest_results/`](../backtest_results/) — um JSON por
estratégia (`edge`, `harvest_12h/14h/16h`, `combined`, `confidence`,
`calibration`, `meta`) com o resultado da última execução do backtest.
