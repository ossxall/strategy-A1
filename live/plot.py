#!/usr/bin/env python3
"""
Grafica el JSON del endpoint `/series` de bot.py (ver server.py).

Genera una página HTML interactiva (Plotly). Cada timeframe recibe el mismo
gráfico completo, con todo lo que traiga el payload:

- precio: velas + EMA rápida/lenta, marcadores de señal (`cross_up`,
  `cross_down`, `adx_exit`, `weak_exit`, `rsi_div_exit`) y los trades;
- trades: entradas (▲ LONG / ▼ SHORT), salidas (■ verde si ganó, rojo si
  perdió; tooltip con razón, PnL, R y % cerrado) y una línea de entrada a cada
  salida;
- paneles: `adx_entry`, volumen y `target` (solo si la serie existe).

Uso
---
    python plot.py                               # lee http://127.0.0.1:8080/series
    python plot.py series.json                   # lee un archivo guardado
    python plot.py http://bot:8080/series --last 300 --tz America/Bogota
    python plot.py --timeframe 30min --timeframe 1m
    python plot.py --watch 30 --open             # refresca cada 30 s

Requiere `pandas` y `plotly` (`pip install plotly`).
"""
from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

DEFAULT_SOURCE = "http://127.0.0.1:8080/series"

UP, DOWN = "#26a69a", "#ef5350"
FAST, SLOW, NEUTRAL = "#ff9800", "#2962ff", "#7e57c2"
EXIT_SIGNAL, WEAK_SIGNAL, RSI_DIV_SIGNAL = "#ffeb3b", "#e040fb", "#00bcd4"
PANEL_HEIGHTS = {"price": 0.55, "adx": 0.15, "volume": 0.15, "target": 0.15}


# ---------------------------------------------------------------------------
# Carga de datos
# ---------------------------------------------------------------------------

def load_payload(source: str, limit: int | None = None) -> dict:
    """Lee el JSON desde una URL o un archivo."""
    if source.startswith(("http://", "https://")):
        if limit is not None and "limit=" not in source:
            source += ("&" if "?" in source else "?") + f"limit={limit}"
        with urllib.request.urlopen(source, timeout=15) as response:
            return json.load(response)
    return json.loads(Path(source).read_text(encoding="utf-8"))


def to_index(t: list[int], tz: str) -> pd.DatetimeIndex:
    """`t` (epoch ms) -> DatetimeIndex en la zona horaria pedida."""
    values = pd.Series(t, dtype="int64")
    # Payloads antiguos (antes del fix en server.py) traían t = ms // 10**6.
    if len(values) and values.max() < 10**8:
        values = values * 10**6
    return pd.DatetimeIndex(pd.to_datetime(values, unit="ms", utc=True)).tz_convert(tz)


def to_frame(entry: dict, tz: str, last: int | None) -> pd.DataFrame:
    frame = pd.DataFrame(entry["series"], index=to_index(entry["t"], tz))
    return frame.tail(last) if last else frame


def to_trades(raw_trades: list[dict], tz: str) -> pd.DataFrame:
    """Trades del payload -> DataFrame con columna `ts` (hora de ejecución)."""
    if not raw_trades:
        return pd.DataFrame()
    trades = pd.DataFrame(raw_trades)
    for column in ("price", "pnl_net", "r_multiple", "close_pct", "position_id"):
        if column not in trades:
            trades[column] = float("nan")
    trades["ts"] = to_index(trades["time"].tolist(), tz)
    return trades


# ---------------------------------------------------------------------------
# Trazas
# ---------------------------------------------------------------------------

def _flag(frame: pd.DataFrame, column: str) -> pd.Series:
    """Serie booleana (False si la columna no existe o tiene nulos)."""
    if column not in frame:
        return pd.Series(False, index=frame.index)
    return frame[column].fillna(False).astype(bool)


def _num(value, default: float = 0.0) -> float:
    return default if pd.isna(value) else float(value)


def _div_flags(frame: pd.DataFrame, value: int) -> pd.Series:
    """Máscara de divergencia RSI (``rsi_div_exit`` == ``value``; +1 alcista, -1 bajista)."""
    if "rsi_div_exit" not in frame:
        return pd.Series(False, index=frame.index)
    return pd.to_numeric(frame["rsi_div_exit"], errors="coerce").fillna(0) == value


def _candle_flags(frame: pd.DataFrame, value: int) -> pd.Series:
    """Máscara direccional TA-Lib (alcista +1, bajista -1)."""
    if "candle_exit" not in frame:
        return pd.Series(False, index=frame.index)
    return pd.to_numeric(frame["candle_exit"], errors="coerce").fillna(0) == value


def add_signal_markers(fig, frame: pd.DataFrame, row: int) -> None:
    """Marcadores pequeños de las señales de la estrategia, junto a la vela."""
    pad = (frame["high"].max() - frame["low"].min()) * 0.02
    for column, y, symbol, color in (
        ("cross_up", frame["low"] - pad, "triangle-up-open", UP),
        ("cross_down", frame["high"] + pad, "triangle-down-open", DOWN),
        ("adx_exit", frame["high"] + 2 * pad, "x", EXIT_SIGNAL),
        ("weak_exit", frame["high"] + 3 * pad, "diamond-open", WEAK_SIGNAL),
    ):
        mask = _flag(frame, column)
        if mask.any():
            fig.add_trace(
                go.Scatter(
                    x=frame.index[mask], y=y[mask], mode="markers", name=column,
                    marker=dict(symbol=symbol, size=8, color=color, line=dict(width=1.5, color=color)),
                ),
                row=row, col=1,
            )
    # Divergencia RSI: la alcista (cierre de SHORT) bajo el mínimo, la bajista
    # (cierre de LONG) sobre el máximo.
    for mask, y, name, symbol, color in (
        (_div_flags(frame, 1), frame["low"] - 2 * pad,
         "rsi_divergence_bull", "star", RSI_DIV_SIGNAL),
        (_div_flags(frame, -1), frame["high"] + 4 * pad,
         "rsi_divergence_bear", "star", RSI_DIV_SIGNAL),
        (_candle_flags(frame, 1), frame["low"] - 3 * pad,
         "candle_pattern_bull", "triangle-up-open", EXIT_SIGNAL),
        (_candle_flags(frame, -1), frame["high"] + 4 * pad,
         "candle_pattern_bear", "triangle-down-open", EXIT_SIGNAL),
        (_flag(frame, "doji_exit"), frame["high"] + 5 * pad,
         "candle_doji", "circle-open", WEAK_SIGNAL),
    ):
        if mask.any():
            fig.add_trace(
                go.Scatter(
                    x=frame.index[mask], y=y[mask], mode="markers", name=name,
                    marker=dict(symbol=symbol, size=9, color=color,
                                line=dict(width=1.5, color=color)),
                ),
                row=row, col=1,
            )


def add_trades(fig, trades: pd.DataFrame, frame: pd.DataFrame, row: int) -> tuple[int, int]:
    """Superpone entradas, salidas y la línea entrada->salida. Devuelve (n_entradas, n_salidas)."""
    if trades.empty:
        return 0, 0
    step = frame.index.to_series().diff().median()
    end = frame.index[-1] + (step if pd.notna(step) else pd.Timedelta(0))
    window = trades[(trades["ts"] >= frame.index[0]) & (trades["ts"] <= end)]
    entries = window[window["event"] == "ENTRY"]
    exits = window[window["event"] == "EXIT"]
    entry_by_position = {e.position_id: e for e in entries.itertuples()}

    # Línea de cada entrada a cada una de sus salidas (verde ganó, roja perdió).
    lines = {True: ([], []), False: ([], [])}
    for ex in exits.itertuples():
        entry = entry_by_position.get(ex.position_id)
        if entry is not None:
            xs, ys = lines[_num(ex.pnl_net) >= 0]
            xs += [entry.ts, ex.ts, None]
            ys += [entry.price, ex.price, None]
    for won, (xs, ys) in lines.items():
        if xs:
            fig.add_trace(
                go.Scatter(x=xs, y=ys, mode="lines", name="trade +" if won else "trade −",
                           line=dict(color=UP if won else DOWN, width=1.2, dash="dot"),
                           hoverinfo="skip"),
                row=row, col=1,
            )

    # Entradas.
    for side, symbol, color in (("LONG", "triangle-up", UP), ("SHORT", "triangle-down", DOWN)):
        sub = entries[entries["side"] == side]
        if not sub.empty:
            fig.add_trace(
                go.Scatter(
                    x=sub["ts"], y=sub["price"], mode="markers", name=f"entrada {side}",
                    marker=dict(symbol=symbol, size=14, color=color, line=dict(width=1.5, color="white")),
                    text=[f"ENTRY {side}<br>precio {p:,.2f}" for p in sub["price"]],
                    hovertemplate="%{text}<extra></extra>",
                ),
                row=row, col=1,
            )

    # Salidas (parciales incluidas), coloreadas por resultado.
    for won, label in ((True, "salida +"), (False, "salida −")):
        sub = exits[exits["pnl_net"].map(lambda v: (_num(v) >= 0) == won)]
        if not sub.empty:
            fig.add_trace(
                go.Scatter(
                    x=sub["ts"], y=sub["price"], mode="markers", name=label,
                    marker=dict(symbol="square", size=11, color=UP if won else DOWN,
                                line=dict(width=1.5, color="white")),
                    text=[
                        f"EXIT {r.side} · {r.reason}<br>precio {r.price:,.2f}"
                        f"<br>PnL {_num(r.pnl_net):+,.2f} · R {_num(r.r_multiple):+.2f}"
                        f"<br>cierra {_num(r.close_pct, 100):.0f}% ({r.action})"
                        for r in sub.itertuples()
                    ],
                    hovertemplate="%{text}<extra></extra>",
                ),
                row=row, col=1,
            )
    return len(entries), len(exits)


# ---------------------------------------------------------------------------
# Gráfico de un timeframe
# ---------------------------------------------------------------------------

def build_figure(
    entry: dict, payload: dict, tz: str, last: int | None, trades: pd.DataFrame
) -> go.Figure | None:
    frame = to_frame(entry, tz, last)
    if frame.empty:
        return None

    panels = ["price"]
    if "adx_entry" in frame:
        panels.append("adx")
    if "volume" in frame:
        panels.append("volume")
    if "target" in frame:
        panels.append("target")
    row = {name: i + 1 for i, name in enumerate(panels)}

    fig = make_subplots(
        rows=len(panels), cols=1, shared_xaxes=True, vertical_spacing=0.03,
        row_heights=[PANEL_HEIGHTS[p] for p in panels],
    )
    fig.add_trace(
        go.Candlestick(
            x=frame.index, open=frame["open"], high=frame["high"], low=frame["low"],
            close=frame["close"], name="precio",
            increasing_line_color=UP, decreasing_line_color=DOWN,
        ),
        row=1, col=1,
    )
    for column, color in (("ema_fast", FAST), ("ema_slow", SLOW)):
        if column in frame:
            fig.add_trace(
                go.Scatter(x=frame.index, y=frame[column], name=column,
                           line=dict(color=color, width=1.5)),
                row=1, col=1,
            )
    add_signal_markers(fig, frame, 1)
    n_entries, n_exits = add_trades(fig, trades, frame, 1)

    if "adx" in row:
        fig.add_trace(
            go.Scatter(x=frame.index, y=frame["adx_entry"], name="adx_entry",
                       line=dict(color=NEUTRAL, width=1.5)),
            row=row["adx"], col=1,
        )
        fig.update_yaxes(title_text="ADX", row=row["adx"], col=1)
    if "volume" in row:
        colors = [UP if c >= o else DOWN for o, c in zip(frame["open"], frame["close"])]
        fig.add_trace(
            go.Bar(x=frame.index, y=frame["volume"], marker_color=colors,
                   name="volumen", showlegend=False),
            row=row["volume"], col=1,
        )
        fig.update_yaxes(title_text="vol", row=row["volume"], col=1)
    if "target" in row:
        fig.add_trace(
            go.Scatter(x=frame.index, y=frame["target"], name="target",
                       line=dict(color=FAST, width=1.5, shape="hv")),
            row=row["target"], col=1,
        )
        fig.update_yaxes(title_text="target", tickvals=[-1, 0, 1], row=row["target"], col=1)

    title = (
        f"{payload.get('symbol', '')} · {entry['timeframe']} ({', '.join(entry['roles'])}) · "
        f"{len(frame)} velas · {n_entries} entradas / {n_exits} salidas"
    )
    fig.update_layout(
        title=title, template="plotly_dark", height=250 + 170 * len(panels),
        margin=dict(l=60, r=30, t=60, b=30), hovermode="x unified",
        legend=dict(orientation="h", y=1.04),
    )
    fig.update_xaxes(rangeslider_visible=False)
    return fig


# ---------------------------------------------------------------------------
# Página HTML
# ---------------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">{refresh}
<title>{title}</title>
<style>
  body {{ background:#111; color:#ddd; font:14px system-ui,sans-serif; margin:0 auto; max-width:1500px; padding:16px; }}
  h1 {{ font-size:18px; margin:0 0 4px; }} p {{ margin:0 0 12px; color:#999; }}
  nav a {{ color:#8ab4f8; margin-right:14px; text-decoration:none; }}
  .chart {{ margin:16px 0 24px; }}
</style></head><body>
<h1>{title}</h1><p>{subtitle}</p>
<nav>{nav}</nav>
{charts}
</body></html>
"""


def build_page(payload: dict, args: argparse.Namespace) -> tuple[str, int]:
    entries = payload.get("timeframes", [])
    if args.timeframe:
        entries = [e for e in entries if e["timeframe"] in args.timeframe]

    trades = pd.DataFrame() if args.no_trades else to_trades(payload.get("trades", []), args.tz)
    figures = [
        (e["timeframe"], fig)
        for e in entries
        if (fig := build_figure(e, payload, args.tz, args.last, trades)) is not None
    ]

    plotly_js = True if args.offline else "cdn"  # solo se incluye en el primer gráfico
    nav, charts = [], []
    for i, (timeframe, fig) in enumerate(figures):
        anchor = "tf-" + re.sub(r"\W+", "_", timeframe)
        label = html_lib.escape(timeframe)
        nav.append(f'<a href="#{anchor}">{label}</a>')
        body = fig.to_html(full_html=False, include_plotlyjs=plotly_js if i == 0 else False)
        charts.append(f'<div class="chart" id="{anchor}">{body}</div>')

    subtitle = (
        f"intervalo {payload.get('interval', '?')} · estrategia "
        f"{payload.get('strategy_timeframe', '?')} · {len(payload.get('trades', []))} "
        f"eventos de trade · calculado {payload.get('updated_at', '?')} · zona horaria {args.tz}"
    )
    refresh = f'<meta http-equiv="refresh" content="{args.watch:g}">' if args.watch else ""
    page = PAGE.format(
        refresh=refresh, title=f"{html_lib.escape(payload.get('symbol', 'series'))} — series",
        subtitle=html_lib.escape(subtitle), nav="".join(nav), charts="\n".join(charts),
    )
    return page, len(figures)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Grafica el JSON de /series del bot.")
    parser.add_argument("source", nargs="?", default=DEFAULT_SOURCE,
                        help=f"URL o archivo JSON (por defecto {DEFAULT_SOURCE}).")
    parser.add_argument("-o", "--out", default="series.html", help="HTML de salida.")
    parser.add_argument("-t", "--timeframe", action="append",
                        help="Solo este timeframe (repetible), p. ej. -t 30min -t 1m.")
    parser.add_argument("--last", type=int, default=None, help="Graficar solo las últimas N velas.")
    parser.add_argument("--tz", default="UTC", help="Zona horaria (p. ej. America/Bogota).")
    parser.add_argument("--no-trades", action="store_true", help="No superponer los trades.")
    parser.add_argument("--offline", action="store_true",
                        help="Incrusta plotly.js en el HTML (por defecto se carga desde CDN).")
    parser.add_argument("--watch", type=float, default=0, metavar="SEGUNDOS",
                        help="Vuelve a leer y regenerar cada N segundos (la página se autorrecarga).")
    parser.add_argument("--open", action="store_true", help="Abre el HTML en el navegador.")
    return parser.parse_args()


def render(args: argparse.Namespace) -> int:
    payload = load_payload(args.source, args.last)
    if not payload.get("ready", True) or not payload.get("timeframes"):
        print("El bot todavía no tiene series calculadas.", file=sys.stderr)
        return 0
    page, count = build_page(payload, args)
    Path(args.out).write_text(page, encoding="utf-8")
    print(f"{count} gráfico(s) -> {args.out}  (calculado {payload.get('updated_at')})")
    return count


def main() -> int:
    args = parse_args()
    opened = False
    try:
        while True:
            try:
                count = render(args)
            except Exception as exc:  # en --watch seguimos intentando
                if not args.watch:
                    raise
                print(f"Error: {exc}", file=sys.stderr)
                count = 0
            if args.open and count and not opened:
                webbrowser.open(Path(args.out).resolve().as_uri())
                opened = True
            if not args.watch:
                return 0
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
