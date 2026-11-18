# Barrido de parámetros de la estrategia

Este directorio ejecuta backtests directamente contra `strategy.Config`, sin
editar `strategy/config.py` y sin sobrescribir los informes normales de la
estrategia.

## Agente con OpenCode GPT-6 Luna

`run.py` contiene el CLI, el motor, las métricas, SQLite y la integración OpenCode
en un solo archivo. GPT-6 Luna (`opencode-go/gpt-6-luna`) se
usa como auditor/analista en modo de solo lectura; Python ejecuta todos los
backtests y calcula las métricas. La estrategia original no se edita.

Al ejecutar el agente sin argumentos se muestra la solicitud inicial de datos:

```bash
python permutation/run.py
```

Prepara el análisis y el plan (todavía **no** hace la búsqueda):

```bash
python permutation/run.py prepare \
  --dataset dataset/binance/futures/um/BTCUSDT/csv \
  --source "Binance Vision" \
  --instrument BTCUSDT \
  --timeframe 30min \
  --capital 10000 \
  --commission-bps 4 \
  --spread-bps 2 \
  --slippage-bps 1 \
  --leverage 1 \
  --contract-size 1
```

El ejemplo de costes es ilustrativo: reemplázalo con los costes reales. Si no
se conocen, deben declararse como supuestos antes de generar el plan. El motor
es cash-only y no admite apalancamiento ni multiplicadores de contrato distintos
de 1. El spread cotizado se representa como medio spread adverso por lado y se
suma al slippage usado por el backtester.

El agente OpenCode y su base de sesiones se crean temporalmente en un sandbox
aislado, con todas sus herramientas denegadas, y se eliminan al terminar. El
script solo lee estrategia y dataset; los reportes/checkpoints se escriben
dentro de `permutation/runs/<run-id>/`. No modifica los archivos de entrada ni
escribe resultados fuera de `/permutation`.

Revisa `permutation/runs/<run-id>/plan.md`. El hash fija código, datos, grid,
costes, score, umbrales y splits. Para aprobar explícitamente:

```bash
python permutation/run.py approve \
  --run-id <run-id> \
  --plan-hash <hash-mostrado>
python permutation/run.py run --run-id <run-id>
```

`--autonomous` en `prepare` evita la aprobación humana solo si se solicita
expresamente. En ambos modos, una auditoría fallida o inconclusa bloquea la
búsqueda. El agente verifica que estrategia y dataset no hayan cambiado desde
la aprobación.

La búsqueda solo usa train; walk-forward divide cronológicamente train y la
validación solo evalúa la lista corta. Los finalistas se escriben y congelan en
`reports/finalists.json` antes de habilitar test. El test se puede ejecutar una
única vez:

```bash
python permutation/run.py test --run-id <run-id> --confirm-test
python permutation/run.py status --run-id <run-id>
```

Cada run guarda `experiments.db`, logs, checkpoints, el plan, entorno y reportes
bajo `permutation/runs/<run-id>/`. Los experimentos se deduplican por hash de
parámetros; los detalles de las métricas quedan en SQLite para recuperar
configuraciones train ya completadas.

### Alcance estadístico de esta versión

El agente implementa inventario/aprobación, auditoría de truncación, splits
cronológicos con embargo, score fijo, filtros duros, walk-forward por bloques,
persistencia/deduplicación y congelación OOS de finalistas. **Todavía no
implementa ni declara superadas** las pruebas DSR, PBO/CSCV, Reality
Check/SPA, Monte Carlo, plateau/vecinos, estrés de costes ni estabilidad por
régimen. Esta versión hace una búsqueda exhaustiva del grid aprobado en un solo
ciclo y todavía no produce gráficos ni curvas de equity/trades exportadas. Por
eso el informe no puede emitir veredicto `ROBUSTA`; si un candidato pasa OOS,
como máximo se informa `MARGINAL` con esas limitaciones. `N_trials` y todos los
resultados calculados se guardan en el run.

## Alcance del barrido

`grid.json` define las listas finitas de candidatos que se combinan mediante
producto cartesiano. Las combinaciones que `Config` rechaza se descartan antes
de ejecutar el barrido. Los parámetros omitidos conservan el valor de `Config()`.
El grid actual prueba los 10 pares válidos de EMA de tendencia entre 9, 20, 50,
100 y 200; no cambia los valores por defecto de `Config`.

No existe un conjunto computable de «todos los valores posibles» de la
configuración: varios campos son floats o timeframes y su dominio es infinito
o depende de la aplicación. Para ampliar el experimento, añade parámetros y
listas de candidatos explícitas a `grid.json`. `Config.__post_init__` descarta
combinaciones incompatibles (por ejemplo, `ema_fast >= ema_slow`).

## Grid y salidas

`grid.json` define las listas finitas de candidatos del producto cartesiano; los
parámetros omitidos conservan los valores actuales de `Config()`. El grid por
defecto genera 16 combinaciones posibles y conserva 10 tras descartar las que
violan `trend_ema_fast < trend_ema_slow`. No se pueden permutar capital, costes
ni temporalidad: quedan fijados en el plan aprobado. `Config.__post_init__`
valida las restricciones.

Cada ejecución escribe en `permutation/runs/<run-id>/`: `experiments.db`,
`checkpoints/`, `logs/`, `reports/todos_los_experimentos.csv`,
`reports/mejores_combinaciones.csv`, `reports/finalists.json`,
`reports/informe_final.md` y `environment.txt`.
