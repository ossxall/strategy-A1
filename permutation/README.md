# Barrido de parámetros de la estrategia

Este directorio ejecuta backtests directamente contra `strategy.Config`, sin
editar `strategy/config.py` y sin sobrescribir los informes normales de la
estrategia.

## Alcance del barrido

`grid.json` define las listas finitas de candidatos que se combinan mediante
producto cartesiano. Los parámetros omitidos conservan el valor de `Config()`.
El ejemplo actual tiene 72 combinaciones.

No existe un conjunto computable de «todos los valores posibles» de la
configuración: varios campos son floats o timeframes y su dominio es infinito
o depende de la aplicación. Para ampliar el experimento, añade parámetros y
listas de candidatos explícitas a `grid.json`. `Config.__post_init__` descarta
combinaciones incompatibles (por ejemplo, `ema_fast >= ema_slow`).

## Ejecución

Desde la raíz del repositorio:

```bash
python permutation/run.py --dry-run
python permutation/run.py
```

Opciones:

```bash
python permutation/run.py --grid permutation/grid.json
python permutation/run.py --dataset dataset/binance/BTCUSDT/merged/BTCUSDT-1m.parquet
python permutation/run.py --max-combinations 0
```

Por defecto hay un límite de seguridad de 10.000 combinaciones. Usa
`--max-combinations 0` para quitarlo conscientemente. El tiempo de ejecución
crece con el número de combinaciones y el tamaño del dataset.

## Resultados

Cada ejecución crea un directorio independiente bajo `permutation/results/`:

- `results_by_equity_drawdown.csv`: equity final descendente; en empate,
  drawdown menor (menos negativo); después, win rate mayor.
- `results_by_win_rate.csv`: win rate descendente; después, equity final y
  drawdown.
- `results.jsonl`: resultados correctos, guardados progresivamente.
- `failed.jsonl`: combinaciones inválidas o backtests con error.

El ranking por equity/drawdown sigue un orden lexicográfico, no una puntuación
ponderada: equity final es el criterio principal y el drawdown desempata. La
comparación solo es válida si todas las combinaciones usan el mismo dataset,
periodo y costes. El mejor resultado del barrido no garantiza rendimiento
futuro; reserva datos posteriores para validación fuera de muestra.
