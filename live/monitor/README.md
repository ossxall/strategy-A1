# Monitor de Telegram y endpoints HTTP de `/live`

## Bot de Telegram

`monitor` consulta endpoints HTTP de salud y devuelve el estado HTTP y el JSON
recibido. La respuesta se representa sin asumir nombres ni niveles concretos en
el JSON, por lo que tolera cambios en el esquema.

1. Crea un bot con BotFather y guarda su token como `MONITOR_TELEGRAM_BOT_TOKEN`
   en `live/.env.dev` o `live/.env.prod`, según el perfil. Define allí también
   una contraseña fuerte en `MONITOR_PASSWORD`. `.env.example` solo enumera las
   claves: cópialo al archivo del perfil y no pongas valores reales en el ejemplo.
2. `config.dev.yaml` es la base común; `APP_ENV=prod` aplica encima los cambios
   de `config.prod.yaml`. El token y la contraseña se cargan aparte desde el
   `.env` correspondiente.
3. Revisa `services` en `config.yaml`. En Docker Compose, el contenedor recibe
   automáticamente los endpoints internos (`feed`, `bot` y `executor`); al
   ejecutar directamente se usan las URLs del YAML.
4. Para ejecutarlo directamente, instala las dependencias desde `live/`:

   ```sh
   python -m pip install -r requirements.txt
   ```

5. Para iniciar el monitor en producción, activa el perfil `production`:

   ```sh
   APP_ENV=prod docker-compose --profile production --env-file .env.prod -f docker-compose.yml -f docker-compose.runtime.yml -p blacker-bot1 up -d --build
   ```

   En desarrollo no se inicia por defecto. Para activarlo en dev, configura el
   token y la contraseña en `.env.dev` y ejecuta con `--profile monitor`.
   Para ver los logs de producción:
   `APP_ENV=prod docker-compose --profile production --env-file .env.prod -f docker-compose.yml -f docker-compose.runtime.yml -p blacker-bot1 logs -f monitor`.
   También puedes ejecutarlo directamente desde `live/` con `python monitor.py`;
   cargará el YAML y los secretos correspondientes a `APP_ENV`.

Comandos:

- `/auth <contraseña>` habilita la sesión del usuario hasta `/logout` o el
  reinicio del monitor. Envíalo en un chat privado.
- `/status feed` consulta el endpoint del feed.
- `/status bot` consulta el endpoint del bot.
- `/status executor` consulta el endpoint del executor.
- `/status all` consulta todos los endpoints.
- `/status ip` consulta por HTTPS la IP pública de salida del VPS.
- `/logout` cierra la sesión autenticada.
- `/help` muestra los comandos disponibles.

El comando de IP necesita conectividad HTTPS saliente hacia `api.ipify.org`. El
bot usa long polling de Telegram, por lo que no publica un puerto entrante.
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
