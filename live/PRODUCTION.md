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
- Revisa símbolo, timeframe y riesgo de la estrategia; configura el leverage
  exclusivamente en el executor. Verifica saldo, modo One-way, stops y límites
  del exchange con el operador responsable antes de habilitar órdenes.
  No se garantiza equivalencia entre backtest y ejecución real.
- Mantén sincronización NTP, conectividad estable a Binance y espacio libre en
  disco. Alerta antes de agotar disco: SQLite puede dejar de escribir cuando el
  volumen se llena.

## Despliegue y diagnóstico

Desde `live/`:

```bash
export AUTHELIA_UID="$(id -u)"
export AUTHELIA_GID="$(id -g)"
APP_ENV=prod docker-compose --env-file .env.prod -f docker-compose.yml -f docker-compose.runtime.yml -p blacker-bot1 up -d --build
APP_ENV=prod docker-compose --env-file .env.prod -f docker-compose.yml -f docker-compose.runtime.yml -p blacker-bot1 ps
APP_ENV=prod docker-compose --env-file .env.prod -f docker-compose.yml -f docker-compose.runtime.yml -p blacker-bot1 logs --tail=200 feed bot executor monitor
```

Para detener el stack sin `.env.prod` ni secretos:
`docker-compose -f docker-compose.yml -p blacker-bot1 down`.

El contenedor `monitor` se inicia con el stack, sin publicar puertos entrantes.
Antes del despliegue, define `MONITOR_TELEGRAM_BOT_TOKEN` y una contraseña
fuerte en `MONITOR_PASSWORD` dentro de `.env.prod`. Compose solo pasa variables
`MONITOR_*` al monitor y no le entrega las credenciales de Binance. Las URLs de
salud usan por defecto los nombres DNS internos de Compose; se pueden cambiar
con `MONITOR_FEED_URL`, `MONITOR_BOT_URL` y `MONITOR_EXECUTOR_URL`.

Los healthchecks consultan ahora las rutas `/live/feed/health`,
`/live/bot/health` y `/live/bot/execution/status`, además de validar frescura de
velas/heartbeats. Un servicio solo se considera sano si la aplicación responde
HTTP 200 con `status=healthy`; un estado degradado/no sano o una ruta inaccesible
produce fallo de healthcheck. El feed y el executor también verifican sus
snapshots de SQLite cuando aplica.

**Docker Compose no reinicia automáticamente un contenedor solo por estar
`unhealthy`.** `restart: unless-stopped` cubre salidas del proceso, no fallos de
readiness. Supervisa `APP_ENV=prod docker-compose --env-file .env.prod -f docker-compose.yml -f docker-compose.runtime.yml -p blacker-bot1 ps`
y configura alertas/acción operativa
para `unhealthy`; no reinicies automáticamente el executor ante una discrepancia
de cuenta o `RECONCILIATION_REQUIRED`, porque podría repetir una acción insegura.
Los fallos al enlazar los endpoints del bot o executor ahora abortan el proceso
para que el problema no quede oculto.

Los puertos de feed, bot y executor siguen ligados a `127.0.0.1`. Nginx publica
80 (redirección a HTTPS) y 443, enruta los endpoints `/live/...`, Dozzle en
`/logs/` y Glances en `/glances/`; todas las rutas privadas están protegidas con
Authelia. La cookie de sesión SSO es común a las rutas y dura 24 horas.
Redis conserva las sesiones al reiniciar Authelia y solo es accesible desde la
red Docker separada de autenticación (sin puertos publicados). Antes de habilitar
el acceso externo, configura un certificado válido para `lovelacers.com` siguiendo
[DEPLOYMENT.md](DEPLOYMENT.md), y configura el hostname de Authelia y sus
secretos en el VPS; los puertos internos 8787, 8080 y 8081 no deben exponerse.

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
