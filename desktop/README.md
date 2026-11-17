# Aplicación de escritorio

La ventana usa `WebContentsView` de Electron para mostrar cada sistema; no usa
`iframe`. Las pestañas se definen en `config.json` y no se pueden cerrar desde
la interfaz.

Al iniciar, se muestra la URL de `authUrl` en `config.json` a pantalla completa.
La interfaz y los sistemas configurados no se cargan hasta que el sitio redirige
a otra ruta de su dominio. La autenticación y los sistemas usan la partición
persistente `strategy-a1`, por lo que las cookies y los datos persistentes del
navegador (como `localStorage` e IndexedDB) se conservan entre ejecuciones.

La pestaña **Terminal** usa `https://lovelacers.com/webssh/`; su tráfico pasa
por Nginx y Authelia, no por un puerto local del equipo.

Las DevTools de Electron están habilitadas solo al ejecutar desde el código
fuente (modo desarrollo) y deshabilitadas en la aplicación empaquetada.

En desarrollo, la aplicación lee directamente `desktop/config.json`. En Linux
empaquetado, primero busca `config.json` junto al AppImage/ejecutable; si no está,
usa `app.getPath('userData')/config.json` y copia ahí la configuración incluida
solo la primera vez. La ruta activa aparece en el tooltip de «Aplicación de
escritorio» del header. Después de editarla, reinicia la aplicación. Si una
instalación anterior no tiene `authUrl`, se añade desde la configuración incluida.
También se migra la URL antigua local de Terminal a `/webssh/`, conservando el
resto de la configuración.

Cada entrada necesita un `id` único, un nombre visible y una URL `http` o
`https`:

```json
{
  "authUrl": "https://lovelacers.com/auth",
  "systems": [
    {
      "id": "grafico",
      "name": "Gráfico en vivo",
      "url": "http://127.0.0.1:8787/live/feed/chart"
    }
  ]
}
```
