"""Consulta y renderizado del gráfico OHLCV del colector."""

import sqlite3
from datetime import datetime, timezone

from .config import DB_PATH, INTERVAL, STATUS_DB_TIMEOUT, SYMBOL

try:
    from ..db.sqlite_config import connect_sqlite
except ImportError:  # ejecución como paquete top-level desde live/
    from db.sqlite_config import connect_sqlite

CHART_CANDLE_LIMIT = 100
CHART_ROUTE = "/live/feed/chart"


class ChartUnavailable(RuntimeError):
    """La base no está disponible o todavía no contiene velas."""


def load_latest_candles(limit=CHART_CANDLE_LIMIT):
    """Lee velas recientes sin tomar el lock usado por los escritores."""
    connection = None
    try:
        connection = connect_sqlite(
            DB_PATH,
            timeout_ms=max(1, int(STATUS_DB_TIMEOUT * 1000)),
            read_only=True,
            query_only=True,
        )
        rows = connection.execute(
            """
            SELECT open_time, open, high, low, close, volume
            FROM candles
            WHERE symbol=? AND interval=?
            ORDER BY open_time DESC
            LIMIT ?
            """,
            (SYMBOL, INTERVAL, int(limit)),
        ).fetchall()
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise ChartUnavailable("No se pudo consultar feed.db") from exc
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass

    if not rows:
        raise ChartUnavailable("feed.db todavía no contiene velas")

    rows.reverse()
    return rows


def render_ohlcv_chart():
    """Devuelve un documento HTML autónomo salvo Plotly.js, servido por CDN."""
    try:
        import plotly.graph_objects as go
        import plotly.io as pio
        from plotly.subplots import make_subplots
    except ImportError as exc:
        raise ChartUnavailable(
            "Falta Plotly; instala las dependencias de live/requirements.txt"
        ) from exc

    rows = load_latest_candles()
    timestamps = [datetime.fromtimestamp(row[0] / 1000, timezone.utc) for row in rows]
    opens = [row[1] for row in rows]
    highs = [row[2] for row in rows]
    lows = [row[3] for row in rows]
    closes = [row[4] for row in rows]
    volumes = [row[5] for row in rows]
    volume_colors = [
        "#26a69a" if close >= open_price else "#ef5350"
        for open_price, close in zip(opens, closes)
    ]

    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.75, 0.25],
        subplot_titles=("Precio OHLC", "Volumen"),
    )
    figure.add_trace(
        go.Candlestick(
            x=timestamps,
            open=opens,
            high=highs,
            low=lows,
            close=closes,
            name="OHLC",
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
        ),
        row=1,
        col=1,
    )
    figure.add_trace(
        go.Bar(
            x=timestamps,
            y=volumes,
            marker_color=volume_colors,
            name="Volumen",
        ),
        row=2,
        col=1,
    )
    figure.update_layout(
        title=f"{SYMBOL} · {INTERVAL} · últimas {len(rows)} velas",
        template="plotly_white",
        height=760,
        margin={"l": 55, "r": 25, "t": 80, "b": 45},
        xaxis_rangeslider_visible=False,
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.02},
    )
    figure.update_yaxes(title_text="Precio", row=1, col=1)
    figure.update_yaxes(title_text="Volumen", row=2, col=1)
    figure.update_xaxes(title_text="Tiempo (UTC)", row=2, col=1)

    return pio.to_html(
        figure,
        full_html=True,
        include_plotlyjs="cdn",
        config={"responsive": True, "displaylogo": False},
    )
