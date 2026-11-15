# Operación de `/live` en producción

## Antes de desplegar

- Ejecuta primero el flujo completo en testnet: entrada, stop protector, salida
  parcial/total, reinicio durante una orden y reconciliación. Las pruebas offline
  no sustituyen las pruebas de red ni los fills reales.
- Trading sigue apagado por defecto. Mainnet exige
  `BOT_BINANCE_ENVIRONMENT=production`,
  `BOT_BINANCE_TRADING_ENABLED=true` y
  `BOT_BINANCE_ALLOW_LIVE_TRADING=true`; no actives esos tres valores como parte
  de un despliegue rutinario. Usa claves sin permisos de retiro, restringidas por
  IP, y guarda `.env` fuera del control de versiones con permisos de solo lectura
  para el usuario del servicio.
- Revisa símbolo, timeframe, riesgo, leverage, saldo, modo One-way, stops y
  límites del exchange con el operador responsable antes de habilitar órdenes.
  No se garantiza equivalencia entre backtest y ejecución real.
- Mantén sincronización NTP, conectividad estable a Binance y espacio libre en
  disco. Alerta antes de agotar disco: SQLite puede dejar de escribir cuando el
  volumen se llena.

## Despliegue y diagnóstico

Desde `live/`:

```bash
docker-compose -p blacker-bot1 config
docker-compose -p blacker-bot1 up -d --build
docker-compose -p blacker-bot1 ps
docker-compose -p blacker-bot1 logs --tail=200 feed bot executor
```

Los healthchecks consultan ahora las rutas `/live/feed/health`,
`/live/bot/health` y `/live/bot/execution/status`, además de validar frescura de
velas/heartbeats. Un servicio solo se considera sano si la aplicación responde
HTTP 200 con `status=healthy`; un estado degradado/no sano o una ruta inaccesible
produce fallo de healthcheck. El feed y el executor también verifican sus
snapshots de SQLite cuando aplica.

**Docker Compose no reinicia automáticamente un contenedor solo por estar
`unhealthy`.** `restart: unless-stopped` cubre salidas del proceso, no fallos de
readiness. Supervisa `docker-compose -p blacker-bot1 ps` y configura alertas/acción operativa
para `unhealthy`; no reinicies automáticamente el executor ante una discrepancia
de cuenta o `RECONCILIATION_REQUIRED`, porque podría repetir una acción insegura.
Los fallos al enlazar los endpoints del bot o executor ahora abortan el proceso
para que el problema no quede oculto.

Los puertos publicados por Compose están ligados a `127.0.0.1`. Si se exponen
mediante proxy, VPN o una interfaz distinta, añade TLS, autenticación,
restricción de red y rate limiting: las rutas de estado no incorporan
autenticación y contienen información operativa de la cuenta.

## Persistencia, copias y recuperación

- Conserva `live/data/`: contiene `feed.db`, `signal.db`, leases, deduplicación,
  marca `live_since_ms` y estado del executor. `feed.db` puede reconstruirse
  desde REST; `signal.db` es crítico para no duplicar señales ni perder el
  historial de ejecución. No borres ni reemplaces las bases con los servicios
  activos.
- SQLite usa WAL. Mantén cada base y sus archivos WAL/SHM en el mismo volumen
  local persistente; no uses un filesystem de red ni ejecutes réplicas escritoras
  del mismo stack. Este diseño es de instancia única, no de alta disponibilidad.
- Haz copias consistentes con la API de backup de SQLite (por ejemplo,
  `sqlite3 live/data/signal.db ".backup 'backups/signal.db'"` y lo mismo para
  `feed.db`), guarda las copias fuera del host y prueba su restauración. No
  copies solo el archivo `.db` mientras el proceso está activo.
- Para recuperación, inspecciona primero logs, estado del endpoint y
  `RECONCILIATION_REQUIRED`; valida posición y órdenes abiertas en Binance antes
  de reanudar. Nunca resuelvas una discrepancia borrando el ledger.

## Límites conocidos

`/live` sigue dependiendo de un único host, SQLite, Binance REST/WebSocket y del
operador para reaccionar a alertas. Compose no ofrece failover, backups
automáticos, alertado, rotación de secretos ni recuperación automática de
reconciliaciones. Antes de considerar el servicio listo para producción real,
añade monitorización externa, política de backup/retención probada y un
procedimiento de incidentes; valida también carga, disco, reinicio y pérdida de
red en el entorno objetivo.
