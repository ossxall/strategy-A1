# Aplicación de escritorio

La ventana usa `WebContentsView` de Electron para mostrar cada sistema; no usa
`iframe`. Las pestañas se definen en `config.json` y no se pueden cerrar desde
la interfaz.

En desarrollo, la aplicación lee directamente `desktop/config.json`. En la
versión empaquetada, copia ese archivo una vez a
`app.getPath('userData')/config.json` y lee la copia local; su ruta aparece en el
tooltip de «Aplicación de escritorio» en el header. Después de editarlo, reinicia
la aplicación.

Cada entrada necesita un `id` único, un nombre visible y una URL `http` o
`https`:

```json
{
  "systems": [
    {
      "id": "grafico",
      "name": "Gráfico en vivo",
      "url": "http://127.0.0.1:8787/live/feed/chart"
    }
  ]
}
```
