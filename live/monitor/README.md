# HTTP endpoints de `/live`

Inventario de los tres servicios HTTP definidos en `docker-compose.yml`:

| Servicio | Puerto local | Rutas `/live` | Formato |
| --- | ---: | --- | --- |
| `feed` | 8787 | `/live/feed/health`, `/live/feed/status`, `/live/feed/chart` | JSON, JSON, HTML |
| `bot` | 8080 | `/live/bot/health`, `/live/bot/status`, `/live/bot/backtest/…`, `/live/bot/report/…` | JSON, JSON, HTML/archivos generados |
| `executor` | 8081 | `/live/bot/execution/status`, `/live/bot/execution/health` | JSON |

Las rutas listadas son `GET`. Las respuestas de salud pueden devolver `503`
cuando el servicio está degradado o no sano. Las rutas `backtest` y `report`
sirven el visor y sus archivos generados; los `.rest` incluyen ejemplos de los
manifiestos JSON. Cambia `@backtestFile` o `@reportFile` en `bot.rest` para pedir
otro archivo.

## Uso

Los archivos usan por defecto los puertos publicados en loopback por Compose:
`feed.rest` → `127.0.0.1:8787`, `bot.rest` → `127.0.0.1:8080` y
`executor.rest` → `127.0.0.1:8081`. Se pueden ejecutar con la extensión REST
Client de VS Code o cualquier cliente compatible con el formato `.rest`.

Nginx también enruta las rutas con prefijo `/live/` por el puerto 80. Para
probarlo, cambia `@baseUrl` al host Nginx, por ejemplo
`http://127.0.0.1`. Los alias legacy `/health` y `/status` de `feed` y `bot`
solo existen en el servicio directo; Nginx no los enruta.

Alias legacy directos inventariados en los `.rest`:

- `feed`: `/health` y `/status`.
- `bot`: `/health` y `/status`.
- `executor`: `/live/bot/execution/health` (alias de `…/status`).
