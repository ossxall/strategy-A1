# Despliegue remoto en LightNode

Desde tu PC, `live/deploy/build_remote.py` copia `live/.env.prod` al VPS por SSH
y ejecuta allí `live/deploy/build.sh`, que actualiza el checkout y construye y
levanta los contenedores. Requiere Docker Compose 1.29.2 y la dependencia de
Paramiko indicada en `live/deploy/requirements.txt`.

El script verifica la clave SSH del servidor contra `known_hosts` y rechaza
hosts desconocidos. Obtén esa clave por un canal de confianza antes de ejecutar
el build. `--remote-dir` es el directorio padre del checkout
`strategy-A1` (por defecto, el home del usuario SSH).

## Preparación del VPS

Instala Docker Engine y Docker Compose 1.29.2 en el VPS. `live/config.dev.yaml`
contiene la configuración no secreta común; `live/config.prod.yaml` solo define
los overrides de producción, que se aplican con `APP_ENV=prod`. En tu PC, crea
`live/.env.prod` a partir de `.env.example` y completa solo secretos y
datos privados, incluidos `LETSENCRYPT_EMAIL` y `MONITOR_*`. Para desarrollo,
usa una copia independiente llamada `live/.env.dev`. Ambos archivos se ignoran
en Git. `build_remote.py` copia solo `.env.prod` al VPS con permisos privados.
Compose pasa cada secreto solo al servicio que lo necesita.
Mantén `live/data/` en el host: contiene las bases SQLite persistentes y no debe
eliminarse al actualizar el código.

### Acceso externo a los endpoints

Nginx publica los puertos 80 y 443. El puerto 80 solo redirige a HTTPS; Nginx
valida mediante Authelia cada petición a `/live/feed/`, `/live/bot/` y
`/live/bot/execution/`, además del visor Dozzle en `/logs/`, Glances en
`/glances/`, WebSSH en `/webssh/` y SQLite Web en `/sqlweb/`. Los puertos
internos de estos servicios no se publican directamente. El portal de acceso
está en `/auth/`. Las rutas protegidas comparten
la misma cookie de sesión, así que el usuario inicia sesión una vez y no vuelve
a introducir la contraseña hasta que caduque. Authelia requiere
usuario y contraseña (sin segundo factor). La cookie dura 24 horas, también
caduca tras 24 horas de inactividad, y no tiene una extensión «recordarme».
Redis conserva la sesión entre reinicios de Authelia; Redis no publica ningún
puerto al host.

SQLite Web tiene acceso de lectura/escritura a las bases de `live/data/`.
`SQLITE_WEB_PASSWORD` queda vacía por ahora; Nginx protege
`https://lovelacers.com/sqlweb/` con Authelia. El servicio no publica un puerto
directamente en el host, así que mantenlo detrás de ese proxy autenticado.

WebSSH está disponible en `https://lovelacers.com/webssh/`. En Compose usa la
política `warning` para permitir conexiones a hosts nuevos; Authelia protege el
acceso web, pero esa política no verifica la clave SSH del servidor de destino.
Para exigir verificación de claves, configura `WEBSSH_POLICY=reject` y añade
claves de destino verificadas por un canal de confianza a
`live/ssh/known_hosts` antes de iniciar el servicio; Compose lo monta como
`/root/.ssh/known_hosts` dentro del contenedor.

Antes de iniciar Compose:

1. El hostname configurado es `lovelacers.com` (A: `130.94.105.224`). Abre TCP
   80 y 443 en los firewalls del VPS/proveedor. Después del `git clone`, desde
   el VPS ejecuta el asistente para obtener el primer certificado:

   ```bash
   cd strategy-A1/live
   bash deploy/setup_tls.sh
   ```

   El script solicita el correo de Let's Encrypt si no se exportó
   `LETSENCRYPT_EMAIL`, sirve temporalmente solo el desafío ACME y guarda el
   certificado en `live/certbot/conf/` (excluido de Git). Al terminar, apaga
   ese Nginx temporal.
2. En el VPS, ejecuta el asistente de Authelia:

   ```bash
   cd strategy-A1/live
   bash deploy/setup_authelia.sh
   ```

   El asistente copia la configuración para `lovelacers.com`, genera los
   secretos y crea un usuario. La contraseña se solicita sin mostrarla; solo se
   guarda su hash Argon2. Ejecútalo como el mismo usuario SSH que hará el
   despliegue (en este caso, `root`). No reemplaza configuraciones, usuarios ni
   secretos existentes; además corrige `remember_me: '-1'` de configuraciones
   anteriores.

3. Cuando el certificado y la configuración de Authelia estén
   preparados, pon el mismo `LETSENCRYPT_EMAIL` en el `.env.prod` de tu PC y
   ejecuta allí `build_remote.py`. Ese script copia `.env.prod`, pero no copia
   los archivos de Authelia ni los certificados; estos ya deben estar creados y
   persistir en el VPS. Luego el build remoto inicia el despliegue normal:

   ```bash
   python3 live/deploy/build_remote.py \
     --host 130.94.105.224 \
     --user <usuario-ssh> \
     --key ~/.ssh/<clave-privada> \
     --remote-dir ~
   ```

No publiques directamente los puertos 8787, 8080 ni 8081: permanecen ligados a
`127.0.0.1`. Tras desplegar, visita
`https://lovelacers.com/live/feed/health`; Authelia te enviará al portal y te
devolverá al endpoint después de iniciar sesión.

Programa la renovación automática; desde el host crea una entrada de cron para
ejecutar `live/deploy/renew_tls.sh`, por ejemplo:

```cron
0 3,15 * * * /ruta/al/checkout/live/deploy/renew_tls.sh
```

El trading está desactivado por defecto. No actives las opciones de trading real
como parte del despliegue automático; consulta [PRODUCTION.md](PRODUCTION.md)
antes de operar.
