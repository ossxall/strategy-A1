from __future__ import annotations

import json
import math
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import random

import numpy as np
import pandas as pd

from strategy.config import Config
from strategy.models import DEFAULT_OUTPUT_DIR

# ---------------------------------------------------------------------------
# Métricas, reportes y archivos
# ---------------------------------------------------------------------------

def positions_from_trades(trades: pd.DataFrame) -> pd.DataFrame:
    cols = ["position_id", "side", "entry_time", "exit_time", "pnl_net", "fees", "risk", "legs", "reason", "r_multiple"]
    if trades.empty: return pd.DataFrame(columns=cols)
    pos = trades.groupby("position_id", sort=True).agg(
        side=("side", "first"), entry_time=("entry_time", "first"), exit_time=("exit_time", "last"),
        pnl_net=("pnl_net", "sum"), fees=("fees", "sum"), risk=("position_risk", "first"),
        legs=("trade_id", "count"), reason=("reason", "last")).reset_index()
    pos["r_multiple"] = np.where(pos.risk > 0, pos.pnl_net / pos.risk, np.nan)
    return pos[cols]


def position_stats(pos: pd.DataFrame) -> dict:
    if pos.empty:
        return {"positions": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0, "profit_factor": None,
                "net_pnl": 0.0, "avg_pnl": 0.0, "expectancy_r": None, "max_consecutive_losses": 0}
    pnl = pos.pnl_net.astype(float)
    gp, gl = float(pnl[pnl > 0].sum()), float(-pnl[pnl < 0].sum())
    streak = best = 0
    for value in pnl:
        streak = streak + 1 if value < 0 else 0
        best = max(best, streak)
    r = pos.r_multiple.dropna()
    return {"positions": int(len(pnl)), "wins": int((pnl > 0).sum()), "losses": int((pnl < 0).sum()),
            "win_rate_pct": float((pnl > 0).mean() * 100), "profit_factor": gp / gl if gl else None,
            "net_pnl": float(pnl.sum()), "avg_pnl": float(pnl.mean()),
            "expectancy_r": float(r.mean()) if len(r) else None, "max_consecutive_losses": int(best)}


def equity_curve_from_trades(trades: pd.DataFrame, capital: float) -> pd.DataFrame:
    cols = ["timestamp", "trade_id", "pnl_net", "cum_pnl", "equity"]
    if trades.empty: return pd.DataFrame(columns=cols)
    out = trades.sort_values(["exit_time", "trade_id"], kind="stable").copy()
    out["cum_pnl"] = out.pnl_net.astype(float).cumsum()
    out["equity"] = capital + out.cum_pnl
    return out[["exit_time", "trade_id", "pnl_net", "cum_pnl", "equity"]].rename(columns={"exit_time": "timestamp"})[cols].reset_index(drop=True)


def calculate_metrics(trades: pd.DataFrame, equity: pd.DataFrame, capital: float) -> dict:
    stats = position_stats(positions_from_trades(trades))
    if equity.empty:
        final, max_dd, min_eq, min_pct = capital, 0.0, capital, 0.0
        peak_eq = trough_eq = capital
        peak_pct = trough_pct = sharpe = sortino = 0.0
    else:
        eq = equity.equity.astype(float)
        final = float(eq.iloc[-1])
        peak = eq.cummax()
        dd = (eq / peak - 1) * 100
        max_dd = float(dd.min())
        min_eq = float(eq.min())
        min_pct = float((eq / capital - 1).min() * 100)
        idx = int(np.argmin(dd.to_numpy()))
        peak_eq, trough_eq = float(peak.iloc[idx]), float(eq.iloc[idx])
        peak_pct, trough_pct = (peak_eq / capital - 1) * 100, (trough_eq / capital - 1) * 100
        daily = equity.set_index("timestamp").equity.resample("1D").last().dropna()
        rets = daily.pct_change().dropna()
        vol = rets.std(ddof=1) if len(rets) > 1 else 0
        sharpe = float(rets.mean() / vol * math.sqrt(365)) if len(rets) > 1 and np.isfinite(vol) and vol > 0 else 0.0
        downside = math.sqrt(float(np.mean(np.minimum(rets.to_numpy(float), 0) ** 2))) if len(rets) else 0
        sortino = float(rets.mean() / downside * math.sqrt(365)) if downside > 0 else 0.0
    net = final - capital
    return {"initial_capital": capital, "final_equity": final, "net_pnl": net,
            "return_pct": net / capital * 100, "max_drawdown_pct": max_dd,
            "max_dd_peak_equity": peak_eq, "max_dd_trough_equity": trough_eq,
            "max_dd_peak_vs_capital_pct": peak_pct, "max_dd_trough_vs_capital_pct": trough_pct,
            "min_equity": min_eq, "min_equity_vs_capital_pct": min_pct,
            "return_over_maxdd": (net / capital * 100) / abs(max_dd) if max_dd < 0 else None,
            "sharpe_daily_365": sharpe, "sortino_daily_365": sortino, "legs": int(len(trades)), **stats}


def jsonable(obj):
    if isinstance(obj, dict): return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)): return [jsonable(v) for v in obj]
    if isinstance(obj, (np.floating, float)): return float(obj) if math.isfinite(float(obj)) else None
    if isinstance(obj, np.integer): return int(obj)
    if isinstance(obj, (pd.Timestamp, datetime)): return obj.isoformat()
    if isinstance(obj, Path): return str(obj)
    return obj


def dataset_quality(data: pd.DataFrame) -> dict:
    if len(data) < 2: return {"bars": len(data), "gaps_gt_1bar": 0, "max_gap_minutes": 0.0}
    delta = data.index.to_series().diff().dropna().dt.total_seconds() / 60
    median = float(delta.median())
    gaps = delta[delta > median]
    return {"bars": int(len(data)), "gaps_gt_1bar": int(len(gaps)),
            "max_gap_minutes": float(gaps.max()) if len(gaps) else median}


def build_report(cfg: Config, data: pd.DataFrame, trades: pd.DataFrame, metrics: dict, source: str) -> dict:
    sides = {}
    for side in ("LONG", "SHORT"):
        subset = trades[trades.side == side] if not trades.empty else trades
        sides[side] = position_stats(positions_from_trades(subset))
    reasons = {}
    if not trades.empty:
        grouped = trades.groupby("reason", sort=False).agg(legs=("trade_id", "count"), positions=("position_id", "nunique"), pnl_net=("pnl_net", "sum"))
        reasons = {str(k): {"legs": int(v.legs), "positions": int(v.positions), "pnl_net": float(v.pnl_net)} for k, v in grouped.iterrows()}
    return jsonable({"strategy": "strategy2_ema_cross", "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": {"source": source, "bars": len(data), "start": data.index[0].isoformat(), "end": data.index[-1].isoformat(), "quality": dataset_quality(data)},
        "config": asdict(cfg), "metrics": metrics, "by_side": sides, "exit_reasons": reasons})


def write_outputs(out_dir: str | Path, trades: pd.DataFrame, equity: pd.DataFrame, curve: pd.DataFrame,
                  report: dict, write_equity_csv: bool = False):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    trades.to_csv(out / "trades.csv", index=False)
    positions_from_trades(trades).to_csv(out / "positions.csv", index=False)
    curve.to_csv(out / "equity_curve.csv", index=False)
    if write_equity_csv: equity.to_csv(out / "equity.csv", index=False)
    (out / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    metrics_path = out / "metrics.json"
    current = jsonable(report["metrics"])
    previous = None
    if metrics_path.exists():
        try: previous = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError): pass
    delta = {}
    if previous is not None:
        for key in sorted(set(previous) | set(current)):
            a, b = previous.get(key), current.get(key)
            numeric = isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(b, (int, float)) and not isinstance(b, bool)
            diff = b - a if numeric else None
            delta[key] = {"previous": a, "current": b, "delta": diff,
                          "delta_pct": diff / abs(a) * 100 if numeric and a else None,
                          "changed": diff != 0 if numeric else a != b}
    (out / "metrics_delta.json").write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(),
        "previous_available": previous is not None, "delta": delta}, indent=2, allow_nan=False), encoding="utf-8")
    metrics_path.write_text(json.dumps(current, indent=2, allow_nan=False), encoding="utf-8")
    (out / "config.json").write_text(json.dumps(report["config"], indent=2, allow_nan=False), encoding="utf-8")
    print(f"[OUT] Archivos guardados en {out.resolve()}", flush=True)


def print_report(metrics: dict, cfg: Config, data: pd.DataFrame):
    print("\n" + "=" * 68 + "\nSTRATEGY 2 — EMA CROSS\n" + "=" * 68)
    print(f"Periodo       : {data.index[0]} -> {data.index[-1]}")
    print(f"Timeframe     : {cfg.timeframe} | EMA{cfg.ema_fast}/{cfg.ema_slow}")
    print(f"Capital       : {metrics['initial_capital']:,.2f} -> {metrics['final_equity']:,.2f}")
    print(f"PnL neto      : {metrics['net_pnl']:,.2f} ({metrics['return_pct']:.2f}%)")
    print(f"Max drawdown  : {metrics['max_drawdown_pct']:.2f}%")
    print(f"Sharpe/Sortino: {metrics['sharpe_daily_365']:.2f} / {metrics['sortino_daily_365']:.2f}")
    print(f"Posiciones    : {metrics['positions']} | tramos: {metrics['legs']}")
    print(f"Win rate      : {metrics['win_rate_pct']:.2f}% | PF: {metrics['profit_factor']}")
    print(f"Expectancy R  : {metrics['expectancy_r']} | racha pérdidas: {metrics['max_consecutive_losses']}")
    print(f"Costes        : comisión {cfg.commission_bps} bps | slippage {cfg.slippage_bps} bps")
    print("=" * 68)


def plot_equity(curve: pd.DataFrame, path: str | Path, capital: float, dpi: int = 150):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        print("[PLOT] matplotlib no disponible; se omite el gráfico.")
        return
    if curve.empty:
        print("[PLOT] Sin cierres; no se genera gráfico.")
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    x = pd.to_datetime(curve.timestamp, utc=True)
    y = curve.equity.to_numpy(float)
    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(x, y, linewidth=1.4, label="Equity")
    ax.axhline(capital, linestyle="--", linewidth=0.9, label=f"Capital inicial ({capital:,.2f})")
    ax.set(title="Strategy 2 — Equity curve", xlabel="Fecha (UTC)", ylabel="Equity")
    ax.grid(True, alpha=.3); ax.legend()
    locator = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(locator); ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(path, dpi=dpi); plt.close(fig)
    print(f"[PLOT] {path}", flush=True)


# ---------------------------------------------------------------------------
# Gráfico interactivo del backtest completo (Plotly)
# ---------------------------------------------------------------------------

UP_COLOR, DOWN_COLOR = "#26a69a", "#ef5350"
FAST_COLOR, SLOW_COLOR, NEUTRAL_COLOR = "#ff9800", "#2962ff", "#7e57c2"
ADX_EXIT_COLOR, WEAK_EXIT_COLOR = "#ffeb3b", "#e040fb"
RSI_DIV_COLOR = "#00bcd4"
STOP_COLOR, TARGET_COLOR, BREAKEVEN_COLOR = "#ff5252", "#00e676", "#b0bec5"
EMA_SLOW_TOUCH_COLOR = "#00e5ff"

#: Prefijo del motivo de salida cuando el precio toca la EMA slow.
EMA_SLOW_TOUCH_REASON = "EMA_SLOW_TOUCH"


def _position_spans(trades: pd.DataFrame) -> list[dict]:
    """Agrupa los tramos (legs) de cada posición para poder dibujar niveles."""
    if trades is None or trades.empty:
        return []
    spans = []
    for position_id, group in trades.sort_values("trade_id").groupby("position_id", sort=True):
        first, last = group.iloc[0], group.iloc[-1]
        spans.append({
            "position_id": position_id,
            "side": str(first["side"]),
            "entry_time": first["entry_time"],
            "exit_time": last["exit_time"],
            "entry_price": float(first["entry_price"]),
            "exit_price": float(last["exit_price"]),
            "pnl_net": float(group["pnl_net"].sum()),
        })
    return spans


def _higher_timeframes(raw: pd.DataFrame | None, cfg: Config) -> tuple[list[dict], list[dict]]:
    """Series e indicadores de los timeframes de salida (ADX y EMA weakening)."""
    if raw is None or raw.empty:
        return [], []
    from strategy.indicators import adx as adx_indicator
    from strategy.indicators import adx_reversal_flags, ema, ema_weakening_flags
    from strategy.utils import resample_ohlcv, source_minutes

    source_td = pd.Timedelta(minutes=source_minutes(raw))
    adx_tfs: list[dict] = []
    if cfg.adx_exit_enabled:
        for timeframe in cfg.adx_timeframes:
            if source_td >= pd.Timedelta(timeframe):
                continue
            low = resample_ohlcv(raw, timeframe)
            if len(low) < cfg.adx_dilen + cfg.adx_adxlen + 2:
                continue
            adx_values = adx_indicator(low, cfg.adx_dilen, cfg.adx_adxlen)["adx"].to_numpy(float)
            adx_tfs.append({"timeframe": timeframe, "bars": low, "adx": adx_values,
                            "key_level": cfg.adx_key_level,
                            "reversal": adx_reversal_flags(adx_values, cfg.adx_key_level)})

    weak_tfs: list[dict] = []
    if cfg.ema_weakening_exit_enabled:
        weak_fast, weak_slow = cfg.ema_weakening_lengths
        for tf in cfg.ema_weakening_timeframes or (cfg.timeframe,):
            if pd.Timedelta(tf) == pd.Timedelta(cfg.timeframe) or source_td >= pd.Timedelta(tf):
                continue
            low = resample_ohlcv(raw, tf)
            if len(low) < weak_slow + 2:
                continue
            fast = ema(low.close, weak_fast).to_numpy(float)
            slow = ema(low.close, weak_slow).to_numpy(float)
            weak_tfs.append({"timeframe": tf, "bars": low, "ema_fast": fast, "ema_slow": slow,
                             "gap_pct": cfg.ema_weakening_gap_pct,
                             "weakening": ema_weakening_flags(fast, slow, cfg.ema_weakening_gap_pct)})
    return adx_tfs, weak_tfs


def _slice_timeframe(info: dict | None, start: pd.Timestamp, end: pd.Timestamp) -> dict | None:
    """Recorta las series de un timeframe (``adx``/``weak``) a ``[start, end)``."""
    if info is None or start is None:
        return info
    low = info["bars"]
    keep = (low.index >= start) & (low.index < end)
    if not keep.any():
        return None
    out = dict(info)
    out["bars"] = low.loc[keep]
    for key, value in info.items():
        if isinstance(value, np.ndarray) and len(value) == len(keep):
            out[key] = value[keep]
    return out


# ---------------------------------------------------------------------------
# Serialización compacta para Plotly (carga rápida en el navegador)
# ---------------------------------------------------------------------------

_GL_THRESHOLD = 2000   # series más largas se renderizan con WebGL (Scattergl)
_MAX_CANDLES = 3000    # sobre este tamaño se dibuja la línea de cierre en vez de velas
_PANEL_HEIGHT = 460    # alto (px) de cada panel del HTML interactivo
_MIN_HEIGHT = 1400     # alto (px) mínimo del HTML interactivo


def _x_ms(values) -> np.ndarray:
    """Timestamps -> milisegundos epoch (int64).

    Plotly serializa los ``DatetimeIndex`` como strings ISO (~22-27 bytes por
    punto). Usar enteros en ms reduce el payload a la mitad; el eje se fuerza a
    ``date`` para que se sigan mostrando fechas.
    """
    return pd.DatetimeIndex(values).to_numpy(dtype="datetime64[ms]").astype("int64")


def _sig(values, digits: int = 9) -> np.ndarray:
    """Redondea a ``digits`` cifras significativas.

    Plotly escribe los floats con su repr más corta; redondear a ~9 cifras
    significativas es visualmente indistinguible y recorta mucho el JSON.
    """
    arr = np.asarray(values, float)
    out = arr.astype(float, copy=True)
    mask = np.isfinite(arr) & (arr != 0)
    if mask.any():
        magnitude = np.floor(np.log10(np.abs(arr[mask])))
        factor = np.power(10.0, digits - 1 - magnitude)
        out[mask] = np.round(arr[mask] * factor) / factor
    return out


_PLOTLY_CONFIG = {
    "responsive": True, "displaylogo": False, "scrollZoom": True,
    "modeBarButtonsToRemove": ["select2d", "lasso2d"],
}

#: Subcarpeta, dentro de ``output_dir``, donde se guardan los HTML de Plotly.
_PLOTLY_SUBDIR = "html"

_PLOTLY_HTML_TEMPLATE = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    html, body {{ margin: 0; padding: 0; background: #101418; }}
    .plotly-graph-div {{ width: 100% !important; }}
  </style>
</head>
<body>
{body}
</body>
</html>
"""


def _write_plotly_html(fig, path: Path, title: str) -> None:
    """Escribe la figura como página HTML completa.

    A diferencia de ``fig.write_html``, el ``<body>`` no lleva márgenes y el
    gráfico ocupa todo el ancho disponible.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    body = fig.to_html(
        full_html=False, include_plotlyjs="cdn", config=_PLOTLY_CONFIG,
        default_width="100%", default_height="100%",
    )
    path.write_text(
        _PLOTLY_HTML_TEMPLATE.format(title=safe_title, body=body),
        encoding="utf-8",
    )


def build_backtest_figure(
    bars: pd.DataFrame,
    cfg: Config,
    signals: pd.DataFrame | None = None,
    trades: pd.DataFrame | None = None,
    equity: pd.DataFrame | None = None,
    adx_exit: np.ndarray | None = None,
    weak_exit: np.ndarray | None = None,
    rsi_div_exit: np.ndarray | None = None,
    raw: pd.DataFrame | None = None,
    source: str = "",
    title: str | None = None,
    window: tuple | None = None,
    html_path: str | Path | None = None,
    opens: list | None = None,
    adds: list | None = None,
    add_skipped: list | None = None,
    candle_exit: np.ndarray | None = None,
    doji_exit: np.ndarray | None = None,
):
    """Construye una figura Plotly con todo el backtest, optimizada para web.

    Cubre la totalidad de la serie e incluye:

    * velas del timeframe de la estrategia y las EMAs rápida/lenta;
    * señales de cruce (`cross_up`/`cross_down`) y salidas (`adx_exit`,
      `weak_exit`, `rsi_div_exit`, patrones TA-Lib y dojis);
    * trades: entradas, salidas, unión entrada→salida, stop-loss, breakeven,
      take-profit parcial y salidas por toque a la EMA slow (motivo
      ``EMA_SLOW_TOUCH_*``), estas últimas con un marcador propio;
    * **entradas parciales**: si se recibe ``opens``/``adds`` (listas del motor
      `Backtester`), las entradas parciales se etiquetan en el hover y el
      añadido de confirmación (``ENTRY_ADD``) se dibuja con un triángulo verde
      apuntando a la derecha, de forma que se distingue la primera entrada del
      cierre de la posición;
    * paneles de ADX de entrada, `target` (señal), volumen y curva de equity;
    * series e indicadores de los timeframes de salida (ADX y EMA weakening)
      cuando se dispone del dataset original `raw`.

    Para que el HTML cargue rápido en el navegador:

    * el eje temporal se envía como enteros en milisegundos (no strings ISO);
    * los valores se redondean a ~9 cifras significativas;
    * las series largas usan WebGL (`Scattergl`) y los niveles se agrupan en una
      sola traza por tipo (en lugar de una por posición);
    * los timeframes secundarios con muchas velas se dibujan como línea de
      cierre en lugar de candlestick;
    * el hover es ``closest`` en vez de ``x unified`` (mucho más liviano).

    Devuelve un ``plotly.graph_objects.Figure``. Si se indica ``html_path``, la
    figura se guarda además como HTML interactivo.
    """
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError as exc:  # pragma: no cover
        raise ImportError("Se requiere plotly (`pip install plotly`) para el gráfico del backtest.") from exc

    if (signals is None or adx_exit is None or weak_exit is None or rsi_div_exit is None
            or candle_exit is None or doji_exit is None):
        from strategy.strategy import EMACrossStrategy
        engine = EMACrossStrategy(cfg)
        if signals is None:
            signals = engine.generate_signals(bars, raw)
        if raw is not None and (adx_exit is None or weak_exit is None or rsi_div_exit is None
                                or candle_exit is None or doji_exit is None):
            auto_adx, auto_weak, auto_div = engine.prepare_exits(raw, bars)
            (auto_candle_bull, auto_candle_bear, auto_signed_bull,
             auto_signed_bear, auto_doji) = engine.prepare_candle_exits(bars)
            auto_candle = (
                (auto_candle_bull | auto_signed_bull).astype(np.int8)
                - (auto_candle_bear | auto_signed_bear).astype(np.int8)
                if auto_candle_bull is not None else None
            )
            adx_exit = auto_adx if adx_exit is None else adx_exit
            weak_exit = auto_weak if weak_exit is None else weak_exit
            rsi_div_exit = auto_div if rsi_div_exit is None else rsi_div_exit
            candle_exit = auto_candle if candle_exit is None else candle_exit
            doji_exit = auto_doji if doji_exit is None else doji_exit

    # Recorte opcional a una ventana temporal (p. ej. un mes). Los indicadores se
    # calculan sobre la serie completa y recién después se recorta para mostrar.
    win_start = win_end = None
    if window is not None:
        win_start, win_end = pd.Timestamp(window[0]), pd.Timestamp(window[1])
        keep = (bars.index >= win_start) & (bars.index < win_end)
        bars = bars.loc[keep]
        if signals is not None:
            signals = signals.reindex(bars.index)
        if adx_exit is not None:
            adx_exit = np.asarray(adx_exit, bool)[keep]
        if weak_exit is not None:
            weak_exit = np.asarray(weak_exit, bool)[keep]
        if rsi_div_exit is not None:
            rsi_div_exit = np.asarray(rsi_div_exit, int)[keep]
        if candle_exit is not None:
            candle_exit = np.asarray(candle_exit, int)[keep]
        if doji_exit is not None:
            doji_exit = np.asarray(doji_exit, bool)[keep]
        if trades is not None and not trades.empty:
            trades = trades[(trades["entry_time"] < win_end) & (trades["exit_time"] >= win_start)]
        if equity is not None and not equity.empty:
            equity = equity[(equity["timestamp"] >= win_start) & (equity["timestamp"] < win_end)]

    n = len(bars)
    adx_flags = np.asarray(adx_exit, bool) if adx_exit is not None else np.zeros(n, bool)
    weak_flags = np.asarray(weak_exit, bool) if weak_exit is not None else np.zeros(n, bool)
    div_flags = np.asarray(rsi_div_exit, int) if rsi_div_exit is not None else np.zeros(n, int)
    candle_flags = np.asarray(candle_exit, int) if candle_exit is not None else np.zeros(n, int)
    doji_flags = np.asarray(doji_exit, bool) if doji_exit is not None else np.zeros(n, bool)
    if len(adx_flags) != n:
        adx_flags = np.zeros(n, bool)
    if len(weak_flags) != n:
        weak_flags = np.zeros(n, bool)
    if len(div_flags) != n:
        div_flags = np.zeros(n, int)
    if len(candle_flags) != n:
        candle_flags = np.zeros(n, int)
    if len(doji_flags) != n:
        doji_flags = np.zeros(n, bool)
    div_bull, div_bear = div_flags > 0, div_flags < 0
    candle_bull, candle_bear = candle_flags > 0, candle_flags < 0

    adx_tfs, weak_tfs = _higher_timeframes(raw, cfg)
    if window is not None:
        adx_tfs = [frame for frame in
                   (_slice_timeframe(frame, win_start, win_end) for frame in adx_tfs)
                   if frame is not None]
        weak_tfs = [frame for frame in
                    (_slice_timeframe(frame, win_start, win_end) for frame in weak_tfs)
                    if frame is not None]

    trade_count = 0 if trades is None or trades.empty else int(trades["position_id"].nunique())

    adx_panels = [f"adx_tf_{i}" for i in range(len(adx_tfs))]
    weak_panels = [f"weak_tf_{i}" for i in range(len(weak_tfs))]

    panels = ["price"]
    if signals is not None and "adx_entry" in signals:
        panels.append("adx")
    if signals is not None and "target" in signals:
        panels.append("target")
    panels.extend(adx_panels)
    panels.extend(weak_panels)
    if equity is not None and not equity.empty:
        panels.append("equity")

    weights = {"price": 1.0, "adx": 0.25, "target": 0.2, "equity": 0.4}
    for name in adx_panels:
        weights[name] = 0.35
    for name in weak_panels:
        weights[name] = 0.35
    total_weight = sum(weights[name] for name in panels)
    specs = [[{"secondary_y": name == "price" or name in adx_panels}] for name in panels]
    fig = make_subplots(
        rows=len(panels), cols=1, shared_xaxes=False, vertical_spacing=0.035,
        row_heights=[weights[name] / total_weight for name in panels], specs=specs,
    )
    row = {name: i + 1 for i, name in enumerate(panels)}
    r = row["price"]

    def _line_trace(x, y, name, color, width=1.3, dash=None, shape=None, fill=None,
                    fillcolor=None, legendgroup=None, showlegend=True, opacity=None):
        """Serie de líneas; usa WebGL cuando hay muchos puntos."""
        line = {"color": color, "width": width}
        if dash:
            line["dash"] = dash
        if shape:
            line["shape"] = shape
        kwargs = dict(x=x, y=_sig(y), mode="lines", name=name, line=line,
                      legendgroup=legendgroup, showlegend=showlegend)
        if fill:
            kwargs["fill"] = fill
            kwargs["fillcolor"] = fillcolor or "rgba(120,144,156,0.35)"
        if opacity is not None:
            kwargs["opacity"] = opacity
        builder = go.Scattergl if len(x) >= _GL_THRESHOLD else go.Scatter
        return builder(**kwargs)

    def _marker_trace(x, y, name, symbol, color, size, text=None, showlegend=True):
        kwargs = dict(x=x, y=_sig(y), mode="markers", name=name, showlegend=showlegend,
                      marker=dict(symbol=symbol, size=size, color=color,
                                  line=dict(width=1.2, color="white")))
        if text is not None:
            kwargs["text"] = text
            kwargs["hovertemplate"] = "%{text}<extra></extra>"
        return go.Scatter(**kwargs)

    bars_x = _x_ms(bars.index)
    bars_high = bars["high"].to_numpy(float)
    bars_low = bars["low"].to_numpy(float)
    sig_x = _x_ms(signals.index) if signals is not None else bars_x

    # -- Precio + EMAs + volumen -------------------------------------------
    fig.add_trace(go.Candlestick(
        x=bars_x, open=_sig(bars["open"]), high=_sig(bars["high"]),
        low=_sig(bars["low"]), close=_sig(bars["close"]), name="precio",
        increasing_line_color=UP_COLOR, decreasing_line_color=DOWN_COLOR),
        row=r, col=1, secondary_y=False)
    if signals is not None:
        for column, color, label in (("ema_fast", FAST_COLOR, f"EMA {cfg.ema_fast}"),
                                     ("ema_slow", SLOW_COLOR, f"EMA {cfg.ema_slow}")):
            if column in signals:
                fig.add_trace(_line_trace(bars_x, signals[column].to_numpy(float), label, color),
                              row=r, col=1, secondary_y=False)
    if "volume" in bars:
        fig.add_trace(_line_trace(bars_x, bars["volume"].to_numpy(float), "volumen", "#607d8b",
                                  width=0.6, fill="tozeroy", fillcolor="rgba(96,125,139,0.28)",
                                  showlegend=False), row=r, col=1, secondary_y=True)
    fig.update_yaxes(title_text="precio", row=r, col=1, secondary_y=False)
    fig.update_yaxes(title_text="vol", showgrid=False, row=r, col=1, secondary_y=True)

    # -- Señales de la estrategia (marcadores, pocos puntos -> SVG) ---------
    span_y = float(np.nanmax(bars_high) - np.nanmin(bars_low)) or 1.0
    pad = span_y * 0.02

    def _flag(series: pd.Series) -> np.ndarray:
        return series.fillna(False).astype(bool).to_numpy()

    if signals is not None:
        for column, y, symbol, color, label in (
            ("cross_up", bars_low - pad, "triangle-up", UP_COLOR, "cruce ↑"),
            ("cross_down", bars_high + pad, "triangle-down", DOWN_COLOR, "cruce ↓"),
        ):
            if column in signals:
                mask = _flag(signals[column])
                if mask.any():
                    fig.add_trace(_marker_trace(sig_x[mask], y[mask], label, symbol, color, 9),
                                  row=r, col=1)
    for mask, y, symbol, color, label in (
        (adx_flags, bars_high + 2 * pad, "x", ADX_EXIT_COLOR, "salida ADX"),
        (weak_flags, bars_high + 3 * pad, "diamond", WEAK_EXIT_COLOR, "salida weakening"),
        (div_bull, bars_low - 2 * pad, "star", RSI_DIV_COLOR, "divergencia RSI alcista"),
        (div_bear, bars_high + 4 * pad, "star", RSI_DIV_COLOR, "divergencia RSI bajista"),
        (candle_bull, bars_low - 3 * pad, "triangle-up-open", RSI_DIV_COLOR,
         "patrón TA-Lib alcista (salida SHORT)"),
        (candle_bear, bars_high + 5 * pad, "triangle-down-open", RSI_DIV_COLOR,
         "patrón TA-Lib bajista (salida LONG)"),
        (doji_flags, bars_high + 6 * pad, "circle-open", WEAK_EXIT_COLOR,
         "doji TA-Lib (confirmación)"),
    ):
        if mask.any():
            fig.add_trace(_marker_trace(bars_x[mask], y[mask], label, symbol, color, 8),
                          row=r, col=1)

    # -- Trades (conectores y niveles fusionados) --------------------------
    if trades is not None and not trades.empty:
        spans = _position_spans(trades)

        for won, color in ((True, UP_COLOR), (False, DOWN_COLOR)):
            xs, ys = [], []
            for span_item in spans:
                if (span_item["pnl_net"] >= 0) != won:
                    continue
                start_ts, end_ts = span_item["entry_time"], span_item["exit_time"]
                if win_start is not None:
                    start_ts, end_ts = max(start_ts, win_start), min(end_ts, win_end)
                xs += [*_x_ms([start_ts]), *_x_ms([end_ts]), None]
                ys += [span_item["entry_price"], span_item["exit_price"], None]
            if xs:
                fig.add_trace(go.Scatter(x=xs, y=_sig(ys), mode="lines", showlegend=False,
                                         line=dict(color=color, width=1, dash="dot"),
                                         hoverinfo="skip"), row=r, col=1)

        # --- Ayuda visual de entradas parciales (ENTRY 50% → ADD confirmación) --
        # `opens`/`adds` provienen del motor (`Backtester.opened` / `.adds`).
        entry_pct_by_pid: dict[int, float] = {}
        if opens:
            for o in opens:
                try:
                    entry_pct_by_pid[int(o["position_id"])] = float(o.get("entry_pct") or 100.0)
                except (KeyError, TypeError, ValueError):
                    pass
        add_pct_by_pid: dict[int, float] = {}
        if adds:
            for a in adds:
                try:
                    pid = int(a["position_id"])
                    tgt = float(a.get("target_quantity") or 0.0)
                    qty = float(a.get("quantity") or 0.0)
                    pct = (qty / tgt * 100.0) if tgt > 0 else 0.0
                    add_pct_by_pid[pid] = add_pct_by_pid.get(pid, 0.0) + pct
                except (KeyError, TypeError, ValueError):
                    pass

        for side, symbol, color in (("LONG", "triangle-up", UP_COLOR), ("SHORT", "triangle-down", DOWN_COLOR)):
            sub = trades[trades["side"] == side].sort_values("trade_id").drop_duplicates("position_id")
            if win_start is not None:
                sub = sub[(sub["entry_time"] >= win_start) & (sub["entry_time"] < win_end)]
            if not sub.empty:
                texts = []
                for s, p, t, pr in zip(sub["side"], sub["position_id"], sub["entry_time"], sub["entry_price"]):
                    pid = int(p)
                    epct = entry_pct_by_pid.get(pid, 100.0)
                    if epct < 100.0 - 1e-9:
                        if pid in add_pct_by_pid:
                            tag = f" · ENTRADA PARCIAL {epct:g}% → ADD +{add_pct_by_pid[pid]:g}% (completada)"
                        else:
                            tag = f" · ENTRADA PARCIAL {epct:g}% (sin confirmar)"
                    else:
                        tag = ""
                    texts.append(f"ENTRADA {s}{tag}<br>#{pid} · {t:%Y-%m-%d %H:%M}<br>precio {pr:,.4f}")
                fig.add_trace(_marker_trace(_x_ms(sub["entry_time"]), sub["entry_price"],
                                            f"entrada {side}", symbol, color, 13, text=texts),
                              row=r, col=1)

        # Marcadores de ADD: confirmación que completa la entrada parcial.
        if adds:
            add_df = pd.DataFrame(adds)
            add_df["time"] = pd.to_datetime(add_df["time"], utc=True)
            if win_start is not None:
                add_df = add_df[(add_df["time"] >= win_start) & (add_df["time"] < win_end)]
            if not add_df.empty:
                texts = []
                for a in add_df.itertuples():
                    pid = int(a.position_id)
                    side_lbl = "LONG" if int(a.side) == 1 else "SHORT"
                    epct = entry_pct_by_pid.get(pid, 100.0)
                    tgt = float(a.target_quantity or 0.0)
                    pct = (float(a.quantity) / tgt * 100.0) if tgt > 0 else 0.0
                    texts.append(
                        f"ADD CONFIRMACIÓN (entrada parcial {epct:g}% → 100%)<br>"
                        f"pos #{pid} {side_lbl}<br>{a.time:%Y-%m-%d %H:%M}<br>"
                        f"+{float(a.quantity):,.4f} (+{pct:g}%) @ {float(a.price):,.4f}"
                    )
                fig.add_trace(_marker_trace(_x_ms(add_df["time"]), add_df["price"].to_numpy(float),
                                            "ADD (confirmación entrada parcial)", "triangle-right",
                                            "#81c784", 11, text=texts),
                              row=r, col=1)

        # Marcadores de ADD bloqueado por entry_confirm_max_move_pct
        if add_skipped:
            skip_df = pd.DataFrame(add_skipped)
            skip_df["time"] = pd.to_datetime(skip_df["time"], utc=True)
            if win_start is not None:
                skip_df = skip_df[(skip_df["time"] >= win_start) & (skip_df["time"] < win_end)]
            if not skip_df.empty:
                texts = []
                for s in skip_df.itertuples():
                    pid = int(s.position_id)
                    side_lbl = "LONG" if int(s.side) == 1 else "SHORT"
                    epct = float(getattr(s, "entry_pct", 50.0) or 50.0)
                    m = float(getattr(s, "move_pct_against", 0.0) or 0.0)
                    th = float(getattr(s, "threshold", 0.0) or 0.0)
                    texts.append(
                        f"ADD BLOQUEADO · entry_confirm_max_move_pct<br>"
                        f"pos #{pid} {side_lbl} · entrada parcial {epct:g}%<br>"
                        f"{s.time:%Y-%m-%d %H:%M}<br>"
                        f"precio {float(s.price):,.4f}<br>"
                        f"Movido {m:.2f}% en contra (umbral {th:.2f}%)"
                    )
                fig.add_trace(_marker_trace(_x_ms(skip_df["time"]), skip_df["price"].to_numpy(float),
                                            "ADD bloqueado (max move en contra)", "x",
                                            "#ff7043", 12, text=texts),
                              row=r, col=1)

        for won, label, color in ((True, "salida +", UP_COLOR), (False, "salida −", DOWN_COLOR)):
            sub = trades[(trades["pnl_net"].to_numpy(float) >= 0) == won]
            if win_start is not None:
                sub = sub[(sub["exit_time"] >= win_start) & (sub["exit_time"] < win_end)]
            if not sub.empty:
                texts = [
                    f"SALIDA {t.side} · {t.reason}<br>{t.exit_time:%Y-%m-%d %H:%M}<br>"
                    f"precio {t.exit_price:,.4f}<br>PnL {t.pnl_net:+,.2f}"
                    for t in sub.itertuples()
                ]
                fig.add_trace(_marker_trace(_x_ms(sub["exit_time"]), sub["exit_price"],
                                            label, "square", color, 11, text=texts),
                              row=r, col=1)

        # Salidas por toque a la EMA slow (motivo EMA_SLOW_TOUCH_*), resaltadas
        # con un marcador propio para identificarlas en el gráfico.
        touch = trades[trades["reason"].astype(str).str.startswith(EMA_SLOW_TOUCH_REASON)]
        if win_start is not None:
            touch = touch[(touch["exit_time"] >= win_start) & (touch["exit_time"] < win_end)]
        if not touch.empty:
            touch_texts = [
                f"SALIDA {t.side} · {t.reason}<br>{t.exit_time:%Y-%m-%d %H:%M}<br>"
                f"precio {t.exit_price:,.4f}<br>PnL {t.pnl_net:+,.2f}"
                for t in touch.itertuples()
            ]
            fig.add_trace(_marker_trace(_x_ms(touch["exit_time"]), touch["exit_price"],
                                        "salida toque EMA slow", "circle-x",
                                        EMA_SLOW_TOUCH_COLOR, 12, text=touch_texts),
                          row=r, col=1)

        def _level_trace(name, color, dash, level_of):
            xs, ys = [], []
            for span_item in spans:
                sign = 1 if span_item["side"] == "LONG" else -1
                level = level_of(span_item, sign)
                if level is None:
                    continue
                start_ts, end_ts = span_item["entry_time"], span_item["exit_time"]
                if win_start is not None:
                    start_ts = max(start_ts, win_start)
                    end_ts = min(end_ts, win_end)
                xs += [*_x_ms([start_ts]), *_x_ms([end_ts]), None]
                ys += [level, level, None]
            if xs:
                fig.add_trace(go.Scatter(
                    x=xs, y=_sig(ys), mode="lines", name=name, legendgroup=name,
                    showlegend=True, line=dict(color=color, width=1, dash=dash),
                    hovertemplate=f"{name} %{{y:,.4f}}<extra></extra>"), row=r, col=1)

        if cfg.stop_loss_pct > 0:
            _level_trace("stop-loss", STOP_COLOR, "solid",
                         lambda s, sign: s["entry_price"] * (1 - sign * cfg.stop_loss_pct))
        if cfg.partial_tp_enabled and cfg.partial_tp_r > 0 and cfg.stop_loss_pct > 0:
            _level_trace(f"TP {cfg.partial_tp_r:g}R", TARGET_COLOR, "dash",
                         lambda s, sign: s["entry_price"] * (1 + sign * cfg.partial_tp_r * cfg.stop_loss_pct))
        if cfg.partial_tp2_r > 0 and cfg.stop_loss_pct > 0:
            _level_trace(f"TP2 {cfg.partial_tp2_r:g}R", TARGET_COLOR, "dashdot",
                         lambda s, sign: s["entry_price"] * (1 + sign * cfg.partial_tp2_r * cfg.stop_loss_pct))
        if cfg.move_stop_to_breakeven:
            _level_trace("breakeven", BREAKEVEN_COLOR, "dot", lambda s, sign: s["entry_price"])

    # -- Panel ADX de entrada ----------------------------------------------
    if "adx" in row:
        rr = row["adx"]
        fig.add_trace(_line_trace(sig_x, signals["adx_entry"].to_numpy(float), "ADX entrada", NEUTRAL_COLOR),
                      row=rr, col=1)
        if cfg.entry_adx_enabled and cfg.entry_adx_min:
            fig.add_hline(y=cfg.entry_adx_min, line=dict(color="#90a4ae", width=0.9, dash="dash"),
                          row=rr, col=1)
        fig.update_yaxes(title_text="ADX", row=rr, col=1)

    # -- Panel target ------------------------------------------------------
    if "target" in row:
        rr = row["target"]
        fig.add_trace(_line_trace(sig_x, signals["target"].to_numpy(float), "target", FAST_COLOR,
                                  width=1.3, shape="hv"), row=rr, col=1)
        fig.update_yaxes(title_text="señal", tickvals=[-1, 0, 1], ticktext=["short", "flat", "long"],
                         row=rr, col=1)

    # -- Timeframes de ADX (uno por timeframe configurado) ----------------
    for idx, adx_tf in enumerate(adx_tfs):
        name = f"adx_tf_{idx}"
        if name not in row:
            continue
        rr = row[name]
        low = adx_tf["bars"]
        low_x = _x_ms(low.index)
        adx_values = np.asarray(adx_tf["adx"], float)
        if len(low) <= _MAX_CANDLES:
            fig.add_trace(go.Candlestick(
                x=low_x, open=_sig(low["open"]), high=_sig(low["high"]),
                low=_sig(low["low"]), close=_sig(low["close"]),
                name=f"precio {adx_tf['timeframe']}", increasing_line_color=UP_COLOR,
                decreasing_line_color=DOWN_COLOR, showlegend=False, opacity=0.6),
                row=rr, col=1, secondary_y=False)
        else:
            fig.add_trace(_line_trace(low_x, low["close"].to_numpy(float), f"cierre {adx_tf['timeframe']}",
                                      "#90a4ae", width=1.0, showlegend=False, opacity=0.7),
                          row=rr, col=1, secondary_y=False)
        fig.add_trace(_line_trace(low_x, adx_values, f"ADX {adx_tf['timeframe']}", NEUTRAL_COLOR, width=1.5),
                      row=rr, col=1, secondary_y=True)
        fig.add_trace(go.Scatter(
            x=[int(low_x[0]), int(low_x[-1])], y=[adx_tf["key_level"], adx_tf["key_level"]],
            mode="lines", name=f"nivel ADX {adx_tf['key_level']:g}", legendgroup="adx-key",
            line=dict(color=ADX_EXIT_COLOR, width=0.9, dash="dash")), row=rr, col=1, secondary_y=True)
        flags = adx_tf["reversal"]
        if flags.any():
            fig.add_trace(_marker_trace(low_x[flags], adx_values[flags], "reversal ADX",
                                        "x", ADX_EXIT_COLOR, 9), row=rr, col=1, secondary_y=True)
        fig.update_yaxes(title_text="precio", row=rr, col=1, secondary_y=False)
        fig.update_yaxes(title_text="ADX", showgrid=False, row=rr, col=1, secondary_y=True)

    # -- Timeframes de weakening (uno por timeframe configurado) -----------
    for idx, weak_tf in enumerate(weak_tfs):
        name = f"weak_tf_{idx}"
        if name not in row:
            continue
        rr = row[name]
        low = weak_tf["bars"]
        low_x = _x_ms(low.index)
        if len(low) <= _MAX_CANDLES:
            fig.add_trace(go.Candlestick(
                x=low_x, open=_sig(low["open"]), high=_sig(low["high"]),
                low=_sig(low["low"]), close=_sig(low["close"]),
                name=f"precio {weak_tf['timeframe']}", increasing_line_color=UP_COLOR,
                decreasing_line_color=DOWN_COLOR, showlegend=False, opacity=0.6), row=rr, col=1)
        else:
            fig.add_trace(_line_trace(low_x, low["close"].to_numpy(float), f"cierre {weak_tf['timeframe']}",
                                      "#90a4ae", width=1.0, showlegend=False, opacity=0.7), row=rr, col=1)
        fig.add_trace(_line_trace(low_x, weak_tf["ema_fast"], f"EMA {cfg.ema_fast} ({weak_tf['timeframe']})",
                                  FAST_COLOR, width=1.2), row=rr, col=1)
        fig.add_trace(_line_trace(low_x, weak_tf["ema_slow"], f"EMA {cfg.ema_slow} ({weak_tf['timeframe']})",
                                  SLOW_COLOR, width=1.2), row=rr, col=1)
        flags = weak_tf["weakening"]
        if flags.any():
            hi = low["high"].to_numpy(float)
            fig.add_trace(_marker_trace(low_x[flags], hi[flags], "weakening",
                                        "diamond", WEAK_EXIT_COLOR, 8), row=rr, col=1)
        fig.update_yaxes(title_text=f"EMA {weak_tf['timeframe']}", row=rr, col=1)

    # -- Equity ------------------------------------------------------------
    if "equity" in row:
        rr = row["equity"]
        eq_x = _x_ms(equity["timestamp"])
        fig.add_trace(_line_trace(eq_x, equity["equity"].to_numpy(float),
                                  "equity", "#29b6f6", width=1.5), row=rr, col=1)
        fig.add_hline(y=cfg.initial_capital, line=dict(color="#90a4ae", width=0.9, dash="dash"),
                      row=rr, col=1)
        if "position_quantity" in equity and "position_side" in equity:
            held = equity["position_side"].to_numpy() != "FLAT"
            if held.any():
                fig.add_trace(_marker_trace(eq_x[held], equity["equity"].to_numpy(float)[held],
                                            "en posición", "circle", "#ffb74d", 6, showlegend=False),
                              row=rr, col=1)
        fig.update_yaxes(title_text="equity", row=rr, col=1)

    # -- Layout ------------------------------------------------------------
    for name in ("adx", "target"):
        if name in row:
            fig.update_xaxes(matches="x", row=row[name], col=1)
    fig.update_xaxes(rangeslider_visible=False, type="date")

    # Resumen de entradas parciales para el título/leyenda (si el motor lo aporta).
    partial_opens = 0
    if opens:
        for o in opens:
            try:
                if float(o.get("entry_pct") or 100.0) < 100.0 - 1e-9:
                    partial_opens += 1
            except (TypeError, ValueError):
                pass
    base_title = title or (f"{source or 'backtest'} · {cfg.timeframe} · {len(bars):,} velas · "
                           f"{trade_count} posiciones")
    if partial_opens:
        base_title += f" · {partial_opens} entradas parciales"
    fig.update_layout(
        title=base_title,
        template="plotly_dark", height=max(_MIN_HEIGHT, _PANEL_HEIGHT * len(panels)),
        margin=dict(l=60, r=20, t=80, b=40), hovermode="closest",
        legend=dict(orientation="h", y=1.02,
                    title=dict(text="● entrada · ▶ ADD confirmación · ■ salida")), bargap=0.15,
    )
    if partial_opens:
        epct = float(getattr(cfg, "entry_partial_pct", 50.0) or 50.0)
        mode = getattr(cfg, "entry_confirm_mode", "bars")
        detail = f"/{getattr(cfg, 'entry_confirm_bars', 2)} velas" if mode == "bars" else ""
        fig.add_annotation(
            text=(f"Entrada parcial: primera entrada al {epct:g}% y ▶ ADD del resto al "
                  f"confirmarse ({mode}{detail})"),
            xref="paper", yref="paper", x=0, y=1.045, xanchor="left", yanchor="bottom",
            showarrow=False, font=dict(size=11, color="#9e9e9e"),
        )

    if html_path is not None:
        path = Path(html_path)
        _write_plotly_html(fig, path, title=fig.layout.title.text or "backtest")
        if random.random() < 0.1:
            print(f"[PLOTLY] HTML guardado en {path.resolve()} ({path.stat().st_size / 1024:.0f} KB)")
    return fig


def _month_ranges(index: pd.DatetimeIndex) -> list[tuple[str, pd.Timestamp, pd.Timestamp]]:
    """Devuelve ``[(etiqueta, inicio, fin_exclusivo), ...]`` por mes calendario."""
    if len(index) == 0:
        return []
    tz = index.tz
    naive = index.tz_localize(None) if tz is not None else index
    ranges = []
    for period in sorted(set(naive.to_period("M"))):
        start, end = period.start_time, (period + 1).start_time
        if tz is not None:
            start, end = start.tz_localize(tz), end.tz_localize(tz)
        ranges.append((str(period), start, end))
    return ranges


def _write_backtest_index(path: Path, source: str, cfg: Config, bars: pd.DataFrame,
                          links: list[tuple[str, str, int]]) -> None:
    """Escribe una página índice que enlaza los HTML mensuales."""
    rows = "\n".join(
        f'      <li><a href="{href}">{label}</a><span>{count} posiciones</span></li>'
        for label, href, count in links
    )
    path.write_text(
        f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Backtest {source} · {cfg.timeframe}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background:#101418; color:#e6e6e6; margin:0; padding:1.5rem; }}
    h1 {{ font-weight:600; margin-bottom:.2rem; }}
    p {{ color:#9aa0a6; }}
    a {{ color:#4fc3f7; text-decoration:none; }}
    a:hover {{ text-decoration:underline; }}
    li {{ margin:.35rem 0; }}
    span {{ color:#9aa0a6; font-size:.85rem; margin-left:.6rem; }}
  </style>
</head>
<body>
  <h1>Backtest {source}</h1>
  <p>{cfg.timeframe} · {len(bars):,} velas · {len(links)} meses</p>
  <ul>
{rows}
  </ul>
</body>
</html>
""",
        encoding="utf-8",
    )


def build_monthly_backtest_htmls(
    bars: pd.DataFrame,
    cfg: Config,
    signals: pd.DataFrame | None = None,
    trades: pd.DataFrame | None = None,
    equity: pd.DataFrame | None = None,
    adx_exit: np.ndarray | None = None,
    weak_exit: np.ndarray | None = None,
    rsi_div_exit: np.ndarray | None = None,
    raw: pd.DataFrame | None = None,
    source: str = "",
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    index_path: str | Path | None = None,
    opens: list | None = None,
    adds: list | None = None,
    add_skipped: list | None = None,
    candle_exit: np.ndarray | None = None,
    doji_exit: np.ndarray | None = None,
) -> list[Path]:
    """Genera **un HTML de Plotly por mes** y un índice que los enlaza.

    Cada archivo cubre un mes calendario, pero los indicadores se calculan sobre
    la serie completa y solo se recortan al mostrar, así que no se pierde el
    warmup de las EMAs/ADX. Los archivos se guardan en ``output_dir/html/`` con
    nombres ``<stem>_<YYYY-MM>.html`` y el índice se escribe en ``index_path``
    (por defecto ``output_dir/html/backtest.html``).
    """
    index_path = (Path(index_path) if index_path is not None
                  else Path(output_dir) / _PLOTLY_SUBDIR / "backtest.html")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    links: list[tuple[str, str, int]] = []
    for label, start, end in _month_ranges(bars.index):
        target = index_path.with_name(f"{index_path.stem}_{label}.html")
        month_positions = 0
        if trades is not None and not trades.empty:
            overlap = trades[(trades["entry_time"] < end) & (trades["exit_time"] >= start)]
            month_positions = int(overlap["position_id"].nunique())
        build_backtest_figure(
            bars, cfg, signals=signals, trades=trades, equity=equity,
            adx_exit=adx_exit, weak_exit=weak_exit, rsi_div_exit=rsi_div_exit, raw=raw, source=source,
            title=f"{source or 'backtest'} · {label} · {month_positions} posiciones",
            window=(start, end), html_path=target, opens=opens, adds=adds, add_skipped=add_skipped,
            candle_exit=candle_exit, doji_exit=doji_exit,
        )
        written.append(target)
        links.append((label, target.name, month_positions))
    _write_backtest_index(index_path, source, cfg, bars, links)
    print(f"[PLOTLY] Índice mensual en {index_path.resolve()} ({len(written)} meses)", flush=True)
    return written


def generate_report(trades: pd.DataFrame, equity: pd.DataFrame, cfg: Config, data: pd.DataFrame,
                    source: str, output_dir: str | Path = DEFAULT_OUTPUT_DIR,
                    write_outputs_enabled: bool = True, write_equity_csv: bool = False,
                    plot_enabled: bool = True, plot_path: str | Path | None = None,
                    plot_dpi: int = 150, raw: pd.DataFrame | None = None,
                    signals: pd.DataFrame | None = None, adx_exit: np.ndarray | None = None,
                    weak_exit: np.ndarray | None = None,
                    rsi_div_exit: np.ndarray | None = None,
                    plotly_path: str | Path | None = None,
                    opens: list | None = None,
                    adds: list | None = None,
                    add_skipped: list | None = None,
                    candle_exit: np.ndarray | None = None,
                    doji_exit: np.ndarray | None = None) -> dict:
    """Calcula métricas, imprime el informe, guarda outputs y genera los gráficos.

    Punto de entrada único que unifica `calculate_metrics`, `print_report`,
    `equity_curve_from_trades`, `build_report`, `write_outputs`, `plot_equity` y
    `build_monthly_backtest_htmls`. Por defecto escribe **un HTML por mes** en
    `output_dir/html/backtest_<YYYY-MM>.html` más un índice
    `output_dir/html/backtest.html`.
    Devuelve el diccionario de métricas calculado.
    """
    metrics = calculate_metrics(trades, equity, cfg.initial_capital)
    print_report(metrics, cfg, data)
    curve = equity_curve_from_trades(trades, cfg.initial_capital)
    if write_outputs_enabled:
        report = build_report(cfg, data, trades, metrics, source)
        write_outputs(output_dir, trades, equity, curve, report, write_equity_csv)
    if plot_enabled:
        plot_equity(curve, plot_path or str(Path(output_dir) / "equity_curve.png"),
                    cfg.initial_capital, plot_dpi)
        build_monthly_backtest_htmls(
            data, cfg, signals=signals, trades=trades, equity=equity,
            adx_exit=adx_exit, weak_exit=weak_exit, rsi_div_exit=rsi_div_exit, raw=raw, source=source,
            output_dir=output_dir, index_path=plotly_path, opens=opens, adds=adds,
            add_skipped=add_skipped, candle_exit=candle_exit, doji_exit=doji_exit,
        )
    return metrics
