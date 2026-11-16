# Monitor de Telegram y endpoints HTTP de `/live`

## Bot de Telegram

`monitor` consulta endpoints HTTP de salud y devuelve el estado HTTP y el JSON
recibido. La respuesta se representa sin asumir nombres ni niveles concretos en
el JSON, por lo que tolera cambios en el esquema.

1. Crea un bot con BotFather y copia su token a `MONITOR_TELEGRAM_BOT_TOKEN` en
   `live/.env.dev` o `live/.env.prod`.
2. Define una contraseña fuerte en `MONITOR_PASSWORD` en el mismo archivo de
   entorno.
3. Revisa `services` en `config.yaml`. Los valores iniciales usan los nombres
   DNS de Docker Compose y los puertos internos de cada servicio.
4. Para ejecutarlo directamente, instala las dependencias desde `live/`:

   ```sh
   python -m pip install -r monitor/requirements.txt
   ```

5. Arranca el perfil opcional desde `live/`:

   ```sh
   docker-compose -p blacker-bot1 --profile monitor up -d --build monitor
   ```

   Para ver los logs: `docker-compose -p blacker-bot1 logs -f monitor`.
   También puedes ejecutarlo directamente desde `live/` con `python monitor.py`;
   cargará `.env.dev` por defecto o `.env.prod` si `APP_ENV=prod`.

Comandos:

- `/auth <contraseña>` habilita la sesión del usuario hasta `/logout` o el
  reinicio del monitor. Envíalo en un chat privado.
- `/status feed` consulta el endpoint del feed.
- `/status bot` consulta el endpoint del bot.
- `/status executor` consulta el endpoint del executor.
- `/status all` consulta todos los endpoints.
- `/logout` cierra la sesión autenticada.
- `/help` muestra los comandos disponibles.

El bot usa long polling de Telegram, por lo que no publica un puerto entrante.
Fuera de Docker, cambia en `config.yaml` los hosts `feed`, `bot` y `executor`
por `127.0.0.1` y usa los puertos publicados abajo.

## HTTP endpoints de `/live`

Inventario de los servicios HTTP definidos en `docker-compose.yml`:

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
