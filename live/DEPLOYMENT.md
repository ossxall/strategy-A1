# Despliegue automático en LightNode

El workflow `.github/workflows/deploy-lightnode.yml` despliega `main` cuando se
fusiona un pull request **desde `dev` hacia `main`** en este repositorio.
Accede al VPS por SSH, clona el repositorio público la primera vez y, en
despliegues posteriores, actualiza el checkout solo mediante avance rápido.
Después ejecuta
`APP_ENV=prod docker-compose --env-file .env.prod -p blacker-bot1 up -d --build --remove-orphans`
desde `live/`. Este Compose es compatible con Docker Compose 1.29.2.

## Secretos de GitHub

Configura estos secretos en **Settings → Secrets and variables → Actions**:

| Secreto | Valor |
| --- | --- |
| `LIGHTNODE_HOST` | IP o hostname del VPS |
| `LIGHTNODE_USER` | Usuario SSH con acceso a Docker |
| `LIGHTNODE_SSH_KEY` | Clave privada SSH autorizada en el VPS |
| `LIGHTNODE_KNOWN_HOSTS` | Clave pública de host SSH fijada para el VPS |
| `LIGHTNODE_DEPLOY_PATH` | Ruta absoluta del checkout, por ejemplo `/opt/strategy-A1` |
| `LIGHTNODE_PORT` | Puerto SSH; opcional, por defecto `22` |

Obtén la clave de host por un canal de confianza y guárdala en
`LIGHTNODE_KNOWN_HOSTS`; no se acepta automáticamente la clave presentada por
la red. Para un puerto distinto de 22, el registro de `known_hosts` debe usar
el formato `[host]:puerto`.

El usuario SSH debe poder clonar el repositorio público, escribir en
`LIGHTNODE_DEPLOY_PATH` y ejecutar `docker-compose` sin interacción. El directorio
de despliegue debe estar vacío o ser ya un checkout de este repositorio. Si el
checkout local diverge de `origin/main`, el despliegue falla en lugar de
sobrescribir esos cambios.

## Preparación del VPS

Instala Docker Engine y Docker Compose 1.29.2; crea en
`LIGHTNODE_DEPLOY_PATH/live/` el archivo `.env.prod` (a partir de
`.env.example`) con la configuración y secretos de producción, incluidas las
variables `MONITOR_*`. Compose solo inyecta esas variables al contenedor
`monitor`; no le pasa las credenciales de Binance. El workflow no copia ni crea
secretos de aplicación en el VPS.
Mantén `live/data/` en el host: contiene las bases SQLite persistentes y no debe
eliminarse al actualizar el código.

### Acceso externo a `/live`

El servicio Nginx publica solo el puerto 80 y protege las rutas `/live/` con
autenticación básica. **HTTP no cifra las credenciales ni las respuestas**;
úsalo solo temporalmente y limita el puerto 80 en el firewall a tus IP de
confianza, o accede mediante una VPN. No lo dejes abierto a Internet.

Antes de iniciar Compose, instala `apache2-utils` y crea el usuario/contraseña
(el comando solicita la contraseña sin incluirla en el historial):

```bash
sudo apt-get install apache2-utils
cd /ruta/al/checkout/live
sudo htpasswd -cB nginx/.htpasswd operador
sudo chmod 644 nginx/.htpasswd
```

Abre el puerto TCP 80 solo para las IP necesarias en los firewalls del VPS y del
proveedor; no publiques directamente los puertos 8787, 8080 ni 8081. Despliega
como de costumbre. Las rutas del feed van a `feed`, las de
`/live/bot/execution/` a `executor` y las demás `/live/bot/` a `bot`.

El archivo de contraseña está excluido de Git. Para volver a HTTPS, instala un
certificado TLS y configura Nginx antes de abrir el acceso públicamente.

El trading está desactivado por defecto. No actives las opciones de trading real
como parte del despliegue automático; consulta [PRODUCTION.md](PRODUCTION.md)
antes de operar.
