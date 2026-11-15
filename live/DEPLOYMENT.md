# Despliegue automático en LightNode

El workflow `.github/workflows/deploy-lightnode.yml` despliega `main` cuando se
fusiona un pull request **desde `dev` hacia `main`** en este repositorio.
Accede al VPS por SSH, clona el repositorio público la primera vez y, en
despliegues posteriores, actualiza el checkout solo mediante avance rápido.
Después ejecuta `docker-compose -p blacker-bot1 up -d --build --remove-orphans`
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
`LIGHTNODE_DEPLOY_PATH/live/` el archivo `.env` (se puede partir de
`.env.example`). El workflow no copia ni crea secretos de aplicación en el VPS.
Mantén `live/data/` en el host: contiene las bases SQLite persistentes y no debe
eliminarse al actualizar el código.

El trading está desactivado por defecto. No actives las opciones de trading real
como parte del despliegue automático; consulta [PRODUCTION.md](PRODUCTION.md)
antes de operar.
