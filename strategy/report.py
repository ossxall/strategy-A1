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
        equity = equity.sort_values("timestamp", kind="stable")
        eq = equity.equity.astype(float)
        final = float(eq.iloc[-1])
        peak = pd.Series(
            np.maximum(eq.cummax().to_numpy(float), capital),
            index=eq.index,
        )
        dd = (eq / peak - 1) * 100
        max_dd = float(dd.min())
        min_eq = min(float(eq.min()), float(capital))
        min_pct = min(float((eq / capital - 1).min() * 100), 0.0)
        idx = int(np.argmin(dd.to_numpy()))
        peak_eq, trough_eq = float(peak.iloc[idx]), float(eq.iloc[idx])
        peak_pct, trough_pct = (peak_eq / capital - 1) * 100, (trough_eq / capital - 1) * 100
        daily = equity.set_index("timestamp").equity.resample("1D").last().dropna()
        daily_changes = daily.pct_change().dropna().to_numpy(float)
        rets = (
            np.r_[float(daily.iloc[0]) / capital - 1, daily_changes]
            if len(daily) else np.empty(0, dtype=float)
        )
        vol = rets.std(ddof=1) if len(rets) > 1 else 0
        sharpe = float(rets.mean() / vol * math.sqrt(365)) if len(rets) > 1 and np.isfinite(vol) and vol > 0 else 0.0
        downside = math.sqrt(float(np.mean(np.minimum(rets, 0) ** 2))) if len(rets) else 0
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
VOLUME_EXIT_COLOR = "#ff7043"
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
        risk = float(first.get("position_risk", 0.0) or 0.0)
        initial_quantity = float(first.get("position_initial_quantity", 0.0) or 0.0)
        if not np.isfinite(initial_quantity) or initial_quantity <= 0:
            initial_quantity = float(group["quantity"].sum())
        spans.append({
            "position_id": position_id,
            "side": str(first["side"]),
            "entry_time": first["entry_time"],
            "exit_time": last["exit_time"],
            "entry_price": float(first["entry_price"]),
            "exit_price": float(last["exit_price"]),
            "pnl_net": float(group["pnl_net"].sum()),
            "risk_per_unit": risk / initial_quantity if risk > 0 and initial_quantity > 0 else None,
        })
    return spans


def _executed_volume_exit_flags(
    bars: pd.DataFrame,
    trades: pd.DataFrame | None,
    timeframe: str,
) -> np.ndarray:
    """Marca solo la vela que precede una salida real por volumen."""
    flags = np.zeros(len(bars), dtype=bool)
    if (
        bars.empty
        or trades is None
        or trades.empty
        or not {"reason", "exit_time"}.issubset(trades.columns)
    ):
        return flags

    exits = trades[trades["reason"].astype(str).str.startswith("VOLUME_DECLINE")]
    step = pd.Timedelta(timeframe)
    for exit_time in exits["exit_time"]:
        if pd.isna(exit_time):
            continue
        trigger_time = pd.Timestamp(exit_time) - step
        index = bars.index.get_indexer([trigger_time])
        if len(index) and index[0] >= 0:
            flags[index[0]] = True
    return flags


def _configured_tp_levels(cfg: Config) -> list[tuple[str, float, str]]:
    """Niveles TP que el motor puede ejecutar con la configuración actual."""
    if not cfg.partial_tp_enabled or cfg.stop_loss_pct <= 0:
        return []
    return [
        (label, multiple, dash)
        for label, multiple, dash in (
            ("TP", cfg.partial_tp_r, "dash"),
            ("TP2", cfg.partial_tp2_r, "dashdot"),
            ("TP3", cfg.partial_tp3_r, "dot"),
            ("TP4", cfg.partial_tp4_r, "dashdot"),
            ("TP5", cfg.partial_tp5_r, "longdash"),
            ("TP6", cfg.partial_tp6_r, "longdashdot"),
            ("TP7", cfg.partial_tp7_r, "longdash"),
        )
        if multiple > 0
    ]


def _r_target_price(span: dict, side_sign: int, multiple: float) -> float | None:
    """Precio objetivo usando el mismo riesgo por unidad que el motor."""
    risk_per_unit = span.get("risk_per_unit")
    if risk_per_unit is None or not np.isfinite(risk_per_unit) or risk_per_unit <= 0:
        return None
    return float(span["entry_price"] + side_sign * multiple * risk_per_unit)


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


def _closed_x_ms(index: pd.DatetimeIndex, timeframe: str) -> np.ndarray:
    """Timestamps de cierre de velas con índices etiquetados por apertura."""
    return _x_ms(pd.DatetimeIndex(index) + pd.Timedelta(timeframe))


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
    volume_exit: np.ndarray | None = None,
    raw: pd.DataFrame | None = None,
    source: str = "",
    title: str | None = None,
    window: tuple | None = None,
    html_path: str | Path | None = None,
):
    """Construye una figura Plotly con todo el backtest, optimizada para web.

    Cubre la totalidad de la serie e incluye:

    * velas del timeframe de la estrategia y las EMAs rápida/lenta;
    * cruces aceptados/rechazados con motivos, sesgo de tendencia y bloqueos
      detectados durante la ejecución;
    * señal objetivo frente a posición real, trades con detalle de cantidad,
      costes, riesgo, múltiplos R y escalado de tamaño;
    * stop efectivo y activación del breakeven, take-profits y salidas por toque
      a la EMA slow (motivo ``EMA_SLOW_TOUCH_*``);
    * paneles de indicadores, equity, drawdown y PnL por lado/motivo de salida;
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

    if signals is None or adx_exit is None or weak_exit is None or rsi_div_exit is None:
        from strategy.strategy import EMACrossStrategy
        engine = EMACrossStrategy(cfg)
        if signals is None:
            signals = engine.generate_signals(bars, raw)
        if raw is not None and (adx_exit is None or weak_exit is None or rsi_div_exit is None):
            auto_adx, auto_weak, auto_div = engine.prepare_exits(raw, bars)
            adx_exit = auto_adx if adx_exit is None else adx_exit
            weak_exit = auto_weak if weak_exit is None else weak_exit
            rsi_div_exit = auto_div if rsi_div_exit is None else rsi_div_exit

    summary_trades = trades if trades is not None else pd.DataFrame()
    execution_events = (
        equity.loc[equity["execution_rejection"].fillna("").astype(str).ne("")].copy()
        if equity is not None and "execution_rejection" in equity
        else pd.DataFrame()
    )
    if equity is not None and not equity.empty and "equity" in equity:
        equity = equity.copy()
        if "drawdown_pct" not in equity:
            equity_values = equity["equity"].astype(float)
            peaks = np.maximum(equity_values.cummax().to_numpy(float), cfg.initial_capital)
            equity["drawdown_pct"] = np.where(
                peaks > 0, (equity_values.to_numpy(float) / peaks - 1) * 100, 0.0
            )

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
        if trades is not None and not trades.empty:
            summary_trades = trades[
                (trades["exit_time"] >= win_start) & (trades["exit_time"] < win_end)
            ]
            trades = trades[(trades["entry_time"] < win_end) & (trades["exit_time"] >= win_start)]
        else:
            summary_trades = pd.DataFrame()
        if equity is not None and not equity.empty:
            equity = equity[(equity["timestamp"] >= win_start) & (equity["timestamp"] < win_end)]
        if not execution_events.empty and "execution_rejection_time" in execution_events:
            event_times = pd.to_datetime(execution_events["execution_rejection_time"], utc=True)
            start_utc = pd.Timestamp(win_start)
            end_utc = pd.Timestamp(win_end)
            start_utc = start_utc.tz_localize("UTC") if start_utc.tzinfo is None else start_utc.tz_convert("UTC")
            end_utc = end_utc.tz_localize("UTC") if end_utc.tzinfo is None else end_utc.tz_convert("UTC")
            execution_events = execution_events[
                (event_times >= start_utc) & (event_times < end_utc)
            ]

    n = len(bars)
    adx_flags = np.asarray(adx_exit, bool) if adx_exit is not None else np.zeros(n, bool)
    weak_flags = np.asarray(weak_exit, bool) if weak_exit is not None else np.zeros(n, bool)
    div_flags = np.asarray(rsi_div_exit, int) if rsi_div_exit is not None else np.zeros(n, int)
    if len(adx_flags) != n:
        adx_flags = np.zeros(n, bool)
    if len(weak_flags) != n:
        weak_flags = np.zeros(n, bool)
    if len(div_flags) != n:
        div_flags = np.zeros(n, int)
    div_bull, div_bear = div_flags > 0, div_flags < 0
    # Solo se marcan las velas que realmente provocaron una salida por volumen;
    # `volume_exit` se conserva como argumento por compatibilidad; las señales
    # candidatas sin trade ejecutado no son eventos de salida.
    volume_flags = _executed_volume_exit_flags(bars, trades, cfg.timeframe)
    adx_tfs, weak_tfs = _higher_timeframes(raw, cfg)
    if window is not None:
        adx_tfs = [frame for frame in
                   (_slice_timeframe(frame, win_start, win_end) for frame in adx_tfs)
                   if frame is not None and np.isfinite(frame["adx"]).any()]
        weak_tfs = [frame for frame in
                    (_slice_timeframe(frame, win_start, win_end) for frame in weak_tfs)
                    if frame is not None
                    and (np.isfinite(frame["ema_fast"]).any()
                         or np.isfinite(frame["ema_slow"]).any())]
    else:
        adx_tfs = [frame for frame in adx_tfs if np.isfinite(frame["adx"]).any()]
        weak_tfs = [
            frame for frame in weak_tfs
            if np.isfinite(frame["ema_fast"]).any() or np.isfinite(frame["ema_slow"]).any()
        ]

    trade_count = 0 if trades is None or trades.empty else int(trades["position_id"].nunique())

    adx_panels = [f"adx_tf_{i}" for i in range(len(adx_tfs))]
    weak_panels = [f"weak_tf_{i}" for i in range(len(weak_tfs))]

    entry_adx_series: list[tuple[str | None, str]] = []
    if cfg.entry_adx_enabled and signals is not None:
        for idx, timeframe in enumerate(cfg.entry_adx_timeframes):
            column = f"adx_entry_{timeframe}"
            if column not in signals and idx == 0 and "adx_entry" in signals:
                column = "adx_entry"
            if column in signals and np.isfinite(signals[column].to_numpy(float)).any():
                entry_adx_series.append((timeframe, column))
    adx_entry_panels = [f"adx_entry_tf_{i}" for i in range(len(entry_adx_series))]

    summary_rows: list[tuple[str, float, int, int]] = []
    if summary_trades is not None and not summary_trades.empty:
        for side in ("LONG", "SHORT"):
            side_trades = summary_trades[summary_trades["side"] == side]
            if not side_trades.empty:
                summary_rows.append((
                    f"{side} · {side_trades['position_id'].nunique()} pos",
                    float(side_trades["pnl_net"].sum()),
                    int(len(side_trades)),
                    int(side_trades["position_id"].nunique()),
                ))
        for reason, group in summary_trades.groupby("reason", sort=False):
            summary_rows.append((
                f"{reason} · {group['position_id'].nunique()} pos",
                float(group["pnl_net"].sum()),
                int(len(group)),
                int(group["position_id"].nunique()),
            ))
        summary_rows.sort(key=lambda item: item[1])

    panels = ["price"]
    if (signals is not None and "macd_line" in signals
            and np.isfinite(signals["macd_line"].to_numpy(float)).any()):
        panels.append("macd")
    panels.extend(adx_entry_panels)
    atr_filter_enabled = (
        cfg.entry_atr_enabled
        and signals is not None
        and {"atr", "atr_mean", "atr_ratio", "atr_filter_ready", "atr_filter_allowed"}
        .issubset(signals.columns)
        and np.isfinite(signals["atr"].to_numpy(float)).any()
    )
    if atr_filter_enabled:
        panels.append("atr")
    if (signals is not None and "entry_mfi" in signals
            and np.isfinite(signals["entry_mfi"].to_numpy(float)).any()):
        panels.append("mfi")
    if (signals is not None and "entry_er" in signals
            and np.isfinite(signals["entry_er"].to_numpy(float)).any()):
        panels.append("er")
    if ((signals is not None and "target" in signals)
            or (equity is not None and not equity.empty and "position_side" in equity)):
        panels.append("target")
    panels.extend(adx_panels)
    panels.extend(weak_panels)
    if equity is not None and not equity.empty:
        panels.append("equity")
        if np.isfinite(equity["drawdown_pct"].to_numpy(float)).any():
            panels.append("drawdown")
    if summary_rows:
        panels.append("summary")

    weights = {"price": 1.0, "macd": 0.25, "atr": 0.25, "mfi": 0.2, "er": 0.2,
               "target": 0.2, "equity": 0.4, "drawdown": 0.25, "summary": 0.45}
    for name in adx_entry_panels:
        weights[name] = 0.25
    for name in adx_panels:
        weights[name] = 0.35
    for name in weak_panels:
        weights[name] = 0.35
    total_weight = sum(weights[name] for name in panels)
    specs = [[{"secondary_y": name == "price" or name == "atr" or name in adx_panels}]
             for name in panels]
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
        vol = bars["volume"].to_numpy(float)
        # Barras de volumen tradicionales: color según la dirección de la vela.
        vol_colors = np.where(bars["close"].to_numpy(float) >= bars["open"].to_numpy(float),
                              UP_COLOR, DOWN_COLOR)
        fig.add_trace(go.Bar(
            x=bars_x, y=_sig(vol), name="volumen", showlegend=False,
            marker=dict(color=vol_colors, opacity=0.55, line=dict(width=0)),
            hovertemplate="vol %{y:,.0f}<extra></extra>"),
            row=r, col=1, secondary_y=True)
        vol_max = float(np.nanmax(vol)) if len(vol) else 1.0
        fig.update_yaxes(title_text="vol", showgrid=False, range=[0, vol_max * 4 if vol_max > 0 else 1],
                         row=r, col=1, secondary_y=True)
    fig.update_yaxes(title_text="precio", row=r, col=1, secondary_y=False)
    if "volume" not in bars:
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
        for rejected_column, reasons_column, y, label, color in (
            ("cross_up_rejected", "entry_rejection_reasons_long",
             bars_low - 3 * pad, "cruce LONG rechazado", "#ff9800"),
            ("cross_down_rejected", "entry_rejection_reasons_short",
             bars_high + 6 * pad, "cruce SHORT rechazado", "#ab47bc"),
        ):
            if rejected_column in signals:
                mask = _flag(signals[rejected_column])
                if mask.any():
                    reasons = (
                        signals[reasons_column].fillna("").astype(str)
                        if reasons_column in signals else pd.Series("", index=signals.index)
                    )
                    texts = [
                        f"{label}<br>Motivo: {reasons.iloc[i] or 'filtro de entrada'}"
                        for i in np.flatnonzero(mask)
                    ]
                    fig.add_trace(_marker_trace(
                        sig_x[mask], y[mask], label, "x", color, 10, text=texts,
                    ), row=r, col=1)

    if (not execution_events.empty
            and {"execution_rejection_time", "execution_rejection_target",
                 "execution_rejection"}.issubset(execution_events.columns)):
        event_times = pd.DatetimeIndex(execution_events["execution_rejection_time"])
        event_indices = bars.index.get_indexer(event_times)
        valid_events = event_indices >= 0
        if valid_events.any():
            event_rows = execution_events.iloc[np.flatnonzero(valid_events)]
            event_indices = event_indices[valid_events]
            event_targets = event_rows["execution_rejection_target"].to_numpy(int)
            event_y = np.where(
                event_targets > 0,
                bars_low[event_indices] - 4 * pad,
                bars_high[event_indices] + 7 * pad,
            )
            event_text = [
                f"ENTRADA NO EJECUTADA<br>Señal: {'LONG' if target > 0 else 'SHORT'}"
                f"<br>Motivo: {reason}"
                for target, reason in zip(
                    event_targets, event_rows["execution_rejection"].astype(str)
                )
            ]
            fig.add_trace(_marker_trace(
                _x_ms(event_rows["execution_rejection_time"]), event_y, "entrada bloqueada",
                "circle-x", "#ff7043", 11, text=event_text,
            ), row=r, col=1)
    for mask, y, symbol, color, label in (
        (adx_flags, bars_high + 2 * pad, "x", ADX_EXIT_COLOR, "salida ADX"),
        (weak_flags, bars_high + 3 * pad, "diamond", WEAK_EXIT_COLOR, "salida weakening"),
        (div_bull, bars_low - 2 * pad, "star", RSI_DIV_COLOR, "divergencia RSI alcista"),
        (div_bear, bars_high + 4 * pad, "star", RSI_DIV_COLOR, "divergencia RSI bajista"),
        (volume_flags, bars_high + 5 * pad, "circle", VOLUME_EXIT_COLOR, "salida volumen"),
    ):
        if mask.any():
            fig.add_trace(_marker_trace(bars_x[mask], y[mask], label, symbol, color, 8),
                          row=r, col=1)
    # Ayuda visual: línea horizontal que muestra el rango (volume_exit_bars) en el
    # que opera la lógica de volume_exit para cada barra señalada.
    if volume_flags.any() and cfg.volume_exit_enabled:
        window = max(2, int(cfg.volume_exit_bars))
        xs, ys = [], []
        for i in np.where(volume_flags)[0]:
            start = max(0, i - window + 1)
            xs += [int(bars_x[start]), int(bars_x[i]), None]
            ys += [float(bars_high[i] + 5 * pad), float(bars_high[i] + 5 * pad), None]
        if xs:
            fig.add_trace(go.Scatter(x=xs, y=_sig(ys), mode="lines", name="rango volume_exit",
                                      showlegend=True, line=dict(color=VOLUME_EXIT_COLOR, width=2)),
                          row=r, col=1)

    # -- Trades (conectores y niveles fusionados) --------------------------
    if trades is not None and not trades.empty:
        spans = _position_spans(trades)
        position_stats_by_id = positions_from_trades(trades).set_index("position_id")

        def _trade_detail_text(trade, entry: bool = False) -> str:
            risk = float(getattr(trade, "position_risk", 0.0) or 0.0)
            pnl = float(getattr(trade, "pnl_net", 0.0) or 0.0)
            r_value = pnl / risk if risk > 0 else None
            position_r = position_stats_by_id.loc[trade.position_id, "r_multiple"]
            position_r_text = (
                f"{float(position_r):+.3f}R" if pd.notna(position_r) else "n/d"
            )
            initial_qty = float(getattr(
                trade, "position_initial_quantity", getattr(trade, "quantity", 0.0)
            ) or 0.0)
            base_qty = float(getattr(
                trade, "quantity_without_entry_scaling", initial_qty
            ) or 0.0)
            mfi_mult = float(getattr(trade, "entry_mfi_multiplier", 1.0) or 1.0)
            er_mult = float(getattr(trade, "entry_er_multiplier", 1.0) or 1.0)
            adaptive_mult = float(getattr(trade, "entry_adaptive_multiplier", 1.0) or 1.0)
            size_mult = float(getattr(
                trade, "entry_size_multiplier", mfi_mult * er_mult * adaptive_mult
            ) or 1.0)
            sizing = (
                f"<br>Riesgo inicial: {risk:,.2f}"
                f"<br>Cantidad inicial: {initial_qty:,.6g}"
                f"<br>Cantidad sin MFI/ER: {base_qty:,.6g}"
                f"<br>Multiplicadores MFI×{mfi_mult:.3f} · ER×{er_mult:.3f}"
                f" · adaptativo×{adaptive_mult:.3f} · total×{size_mult:.3f}"
            )
            if entry:
                return sizing
            fees = float(getattr(trade, "fees", 0.0) or 0.0)
            gross = float(getattr(trade, "pnl_gross", 0.0) or 0.0)
            r_text = f"{r_value:+.3f}R" if r_value is not None else "n/d"
            return (
                f"<br>Cantidad del tramo: {float(trade.quantity):,.6g}"
                f"<br>PnL bruto: {gross:+,.2f} · comisiones: {fees:,.2f}"
                f"<br>PnL neto: {pnl:+,.2f} · R tramo: {r_text} · R posición: {position_r_text}"
                + sizing
            )

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

        for side, symbol, color in (("LONG", "triangle-up", UP_COLOR), ("SHORT", "triangle-down", DOWN_COLOR)):
            sub = trades[trades["side"] == side].sort_values("trade_id").drop_duplicates("position_id")
            if win_start is not None:
                sub = sub[(sub["entry_time"] >= win_start) & (sub["entry_time"] < win_end)]
            if not sub.empty:
                texts = []
                for trade in sub.itertuples():
                    texts.append(
                        f"ENTRADA {trade.side}<br>#{int(trade.position_id)} · "
                        f"{trade.entry_time:%Y-%m-%d %H:%M}<br>"
                        f"precio {trade.entry_price:,.4f}"
                        f"{_trade_detail_text(trade, entry=True)}"
                    )
                fig.add_trace(_marker_trace(_x_ms(sub["entry_time"]), sub["entry_price"],
                                            f"entrada {side}", symbol, color, 13, text=texts),
                              row=r, col=1)

        for won, label, color in ((True, "salida +", UP_COLOR), (False, "salida −", DOWN_COLOR)):
            sub = trades[(trades["pnl_net"].to_numpy(float) >= 0) == won]
            if win_start is not None:
                sub = sub[(sub["exit_time"] >= win_start) & (sub["exit_time"] < win_end)]
            if not sub.empty:
                texts = [
                    f"SALIDA {t.side} · {t.reason}<br>{t.exit_time:%Y-%m-%d %H:%M}<br>"
                    f"precio {t.exit_price:,.4f}{_trade_detail_text(t)}"
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
                f"precio {t.exit_price:,.4f}{_trade_detail_text(t)}"
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

        for label, multiple, dash in _configured_tp_levels(cfg):
            _level_trace(
                f"{label} {multiple:g}R",
                TARGET_COLOR,
                dash,
                lambda span, sign, r=multiple: _r_target_price(span, sign, r),
            )

    if equity is not None and not equity.empty and "effective_stop_price" in equity:
        stop_values = equity["effective_stop_price"].to_numpy(float)
        stop_mask = np.isfinite(stop_values)
        if stop_mask.any():
            stop_step = np.where(stop_mask, stop_values, np.nan)
            fig.add_trace(
                _line_trace(_x_ms(equity["timestamp"]), stop_step, "stop efectivo",
                            STOP_COLOR, width=1.5, shape="hv"),
                row=r, col=1, secondary_y=False,
            )
        if {"breakeven_activated", "breakeven_price"}.issubset(equity.columns):
            be_mask = _flag(equity["breakeven_activated"]).copy()
            be_prices = equity["breakeven_price"].to_numpy(float)
            be_mask &= np.isfinite(be_prices)
            if be_mask.any():
                be_text = [
                    f"BREAKEVEN ACTIVADO<br>{ts:%Y-%m-%d %H:%M}<br>stop efectivo {price:,.4f}"
                    for ts, price in zip(equity.loc[be_mask, "timestamp"], be_prices[be_mask])
                ]
                fig.add_trace(_marker_trace(
                    _x_ms(equity.loc[be_mask, "timestamp"]), be_prices[be_mask],
                    "activación breakeven", "diamond", BREAKEVEN_COLOR, 11, text=be_text,
                ), row=r, col=1, secondary_y=False)

    # -- Panel MACD --------------------------------------------------------
    if "macd" in row:
        rr = row["macd"]
        macd_values = signals["macd_line"].to_numpy(float)
        fig.add_trace(_line_trace(sig_x, macd_values, "MACD", NEUTRAL_COLOR, width=1.5),
                      row=rr, col=1)
        # Sombreado del signo: positivo en verde, negativo en rojo (zonas).
        pos_mask = np.isfinite(macd_values) & (macd_values > 0)
        neg_mask = np.isfinite(macd_values) & (macd_values < 0)
        if pos_mask.any():
            fig.add_trace(go.Scatter(
                x=sig_x[pos_mask], y=_sig(macd_values[pos_mask]),
                mode="markers", name="MACD > 0", showlegend=False,
                marker=dict(size=3, color=UP_COLOR, opacity=0.35)), row=rr, col=1)
        if neg_mask.any():
            fig.add_trace(go.Scatter(
                x=sig_x[neg_mask], y=_sig(macd_values[neg_mask]),
                mode="markers", name="MACD < 0", showlegend=False,
                marker=dict(size=3, color=DOWN_COLOR, opacity=0.35)), row=rr, col=1)
        fig.add_hline(y=0, line=dict(color="#90a4ae", width=0.9, dash="dash"), row=rr, col=1)
        # Marcadores de cruces filtrados por el MACD (sólo los aceptados).
        if signals is not None and "cross_up" in signals:
            cu = _flag(signals["cross_up"])
            cd = _flag(signals["cross_down"])
            if cu.any():
                fig.add_trace(_marker_trace(sig_x[cu], macd_values[cu], "cruce ↑ (MACD>0)",
                                            "triangle-up", UP_COLOR, 9), row=rr, col=1)
            if cd.any():
                fig.add_trace(_marker_trace(sig_x[cd], macd_values[cd], "cruce ↓ (MACD<0)",
                                            "triangle-down", DOWN_COLOR, 9), row=rr, col=1)
        fig.update_yaxes(title_text=f"MACD ({cfg.macd_timeframe or cfg.timeframe})", row=rr, col=1)

    # -- Paneles ADX de entrada (uno por timeframe) -------------------------
    adx_colors = (NEUTRAL_COLOR, "#ffca28", "#ab47bc", "#26a69a")
    for panel_idx, (timeframe, column) in enumerate(entry_adx_series):
        panel_name = f"adx_entry_tf_{panel_idx}"
        if panel_name not in row:
            continue
        rr = row[panel_name]
        higher_tf = timeframe is not None and pd.Timedelta(timeframe) > pd.Timedelta(cfg.timeframe)
        label = "ADX entrada" if len(entry_adx_series) == 1 else f"ADX entrada {timeframe}"
        fig.add_trace(
            _line_trace(
                sig_x,
                signals[column].to_numpy(float),
                label,
                adx_colors[panel_idx % len(adx_colors)],
                shape="hv" if higher_tf else None,
            ),
            row=rr,
            col=1,
        )
        if cfg.entry_adx_enabled and cfg.entry_adx_min:
            fig.add_hline(y=cfg.entry_adx_min, line=dict(color="#90a4ae", width=0.9, dash="dash"),
                          row=rr, col=1)
        title = "ADX" if timeframe is None else f"ADX {timeframe}"
        fig.update_yaxes(title_text=title, row=rr, col=1)

    # -- Panel del filtro ATR ----------------------------------------------
    if "atr" in row:
        rr = row["atr"]
        atr_values = signals["atr"].to_numpy(float)
        atr_mean = signals["atr_mean"].to_numpy(float)
        ratio = signals["atr_ratio"].to_numpy(float)
        ready = _flag(signals["atr_filter_ready"])
        allowed = _flag(signals["atr_filter_allowed"])

        fig.add_trace(_line_trace(sig_x, atr_values, "ATR", "#ffca28", width=1.3),
                      row=rr, col=1, secondary_y=False)
        fig.add_trace(_line_trace(sig_x, atr_mean, "ATR media previa", "#90a4ae",
                                  width=1.0, dash="dash"),
                      row=rr, col=1, secondary_y=False)

        # Acota valores extremos para que el rango permitido siga siendo legible;
        # el hover conserva el ratio real, incluso si se dibuja en el borde.
        finite_ratio = ratio[np.isfinite(ratio) & (ratio >= 0)]
        percentile = float(np.percentile(finite_ratio, 99)) if len(finite_ratio) else 0.0
        ratio_top = max(cfg.entry_atr_max_ratio * 1.2, percentile * 1.05, 0.1)
        ratio_draw = ratio.copy()
        ratio_draw[np.isposinf(ratio_draw)] = ratio_top
        ratio_draw[np.isneginf(ratio_draw)] = 0.0
        ratio_draw = np.where(np.isfinite(ratio_draw),
                              np.clip(ratio_draw, 0.0, ratio_top), np.nan)
        ratio_label = np.array([
            f"{value:.4g}" if np.isfinite(value) else "∞" if value > 0 else "—"
            for value in ratio
        ], dtype=object)
        customdata = np.column_stack((atr_values, atr_mean, ratio_label))
        trace_builder = go.Scattergl if len(sig_x) >= _GL_THRESHOLD else go.Scatter
        pass_mask = ready & allowed & np.isfinite(ratio)
        blocked_mask = ready & ~allowed

        pass_values = np.where(pass_mask, ratio_draw, np.nan)
        fig.add_trace(trace_builder(
            x=sig_x, y=_sig(pass_values), customdata=customdata, mode="lines+markers",
            name="ATR ratio · permitido", connectgaps=False,
            line=dict(color=UP_COLOR, width=1.5),
            marker=dict(size=3, color=UP_COLOR),
            hovertemplate=(
                "ATR/media: %{customdata[2]}<br>"
                "ATR: %{customdata[0]:,.6g}<br>"
                "Media previa: %{customdata[1]:,.6g}<extra></extra>"
            ),
        ), row=rr, col=1, secondary_y=True)

        if blocked_mask.any():
            blocked_builder = (
                go.Scattergl if int(blocked_mask.sum()) >= _GL_THRESHOLD else go.Scatter
            )
            fig.add_trace(blocked_builder(
                x=sig_x[blocked_mask], y=_sig(ratio_draw[blocked_mask]),
                customdata=customdata[blocked_mask], mode="markers",
                name="ATR ratio · bloquea entrada",
                marker=dict(symbol="x", size=8, color=DOWN_COLOR,
                            line=dict(width=1.2, color="white")),
                hovertemplate=(
                    "<b>Filtro ATR: bloquea entrada</b><br>"
                    "ATR/media: %{customdata[2]}<br>"
                    "ATR: %{customdata[0]:,.6g}<br>"
                    "Media previa: %{customdata[1]:,.6g}<extra></extra>"
                ),
            ), row=rr, col=1, secondary_y=True)

        warmup_mask = ~ready
        if warmup_mask.any():
            warmup_builder = (
                go.Scattergl if int(warmup_mask.sum()) >= _GL_THRESHOLD else go.Scatter
            )
            fig.add_trace(warmup_builder(
                x=sig_x[warmup_mask],
                y=np.full(int(warmup_mask.sum()), ratio_top * 0.015),
                mode="markers", name="ATR warm-up · permitido",
                marker=dict(symbol="circle-open", size=6, color="#90a4ae"),
                text=np.full(int(warmup_mask.sum()),
                             "Filtro permitido: aún no hay media ATR de referencia"),
                hovertemplate="%{text}<extra></extra>",
            ), row=rr, col=1, secondary_y=True)

        fig.add_hrect(
            y0=cfg.entry_atr_min_ratio, y1=cfg.entry_atr_max_ratio,
            fillcolor="rgba(38,166,154,0.12)", line_width=0, layer="below",
            row=rr, col=1, secondary_y=True,
        )
        fig.add_hline(
            y=cfg.entry_atr_min_ratio,
            line=dict(color=UP_COLOR, width=1, dash="dash"),
            annotation_text=f"mín {cfg.entry_atr_min_ratio:g}",
            annotation_position="bottom right",
            row=rr, col=1, secondary_y=True,
        )
        fig.add_hline(
            y=cfg.entry_atr_max_ratio,
            line=dict(color=UP_COLOR, width=1, dash="dash"),
            annotation_text=f"máx {cfg.entry_atr_max_ratio:g}",
            annotation_position="top right",
            row=rr, col=1, secondary_y=True,
        )
        fig.update_yaxes(title_text="ATR", row=rr, col=1, secondary_y=False)
        fig.update_yaxes(title_text="ATR / media previa", range=[0, ratio_top],
                         row=rr, col=1, secondary_y=True)

    # -- Panel MFI ---------------------------------------------------------
    if "mfi" in row:
        rr = row["mfi"]
        fig.add_trace(_line_trace(sig_x, signals["entry_mfi"].to_numpy(float), "MFI", "#26a69a", width=1.3),
                      row=rr, col=1)
        fig.add_hline(y=50, line=dict(color="#90a4ae", width=0.9, dash="dash"), row=rr, col=1)
        fig.update_yaxes(title_text="MFI", range=[0, 100], row=rr, col=1)

    # -- Panel Efficiency Ratio -------------------------------------------
    if "er" in row:
        rr = row["er"]
        fig.add_trace(_line_trace(sig_x, signals["entry_er"].to_numpy(float), "Efficiency Ratio", "#ab47bc",
                                  width=1.3), row=rr, col=1)
        fig.update_yaxes(title_text="ER", range=[0, 1], row=rr, col=1)

    # -- Panel target ------------------------------------------------------
    if "target" in row:
        rr = row["target"]
        if signals is not None and "target" in signals:
            fig.add_trace(_line_trace(
                sig_x, signals["target"].to_numpy(float), "señal objetivo (filtrada)",
                FAST_COLOR, width=1.3, shape="hv",
            ), row=rr, col=1)
        if (cfg.streak_trend_only and signals is not None and "trend_bias" in signals
                and np.isfinite(signals["trend_bias"].to_numpy(float)).any()):
            fig.add_trace(_line_trace(
                sig_x, signals["trend_bias"].to_numpy(float),
                "sesgo tendencia (tras pérdida)", "#ab47bc",
                width=1.1, dash="dot", shape="hv",
            ), row=rr, col=1)
        if (equity is not None and not equity.empty and "position_side" in equity):
            actual_position = equity["position_side"].map(
                {"SHORT": -1, "FLAT": 0, "LONG": 1}
            ).fillna(0).to_numpy(float)
            fig.add_trace(_line_trace(
                _x_ms(equity["timestamp"]), actual_position, "posición real",
                "#29b6f6", width=1.6, shape="hv",
            ), row=rr, col=1)
        fig.update_yaxes(title_text="señal / posición", tickvals=[-1, 0, 1],
                         ticktext=["short", "flat", "long"],
                         row=rr, col=1)

    # -- Timeframes de ADX (uno por timeframe configurado) ----------------
    for idx, adx_tf in enumerate(adx_tfs):
        name = f"adx_tf_{idx}"
        if name not in row:
            continue
        rr = row[name]
        low = adx_tf["bars"]
        bar_x = _x_ms(low.index)
        closed_x = _closed_x_ms(low.index, adx_tf["timeframe"])
        adx_values = np.asarray(adx_tf["adx"], float)
        if len(low) <= _MAX_CANDLES:
            fig.add_trace(go.Candlestick(
                x=bar_x, open=_sig(low["open"]), high=_sig(low["high"]),
                low=_sig(low["low"]), close=_sig(low["close"]),
                name=f"precio {adx_tf['timeframe']}", increasing_line_color=UP_COLOR,
                decreasing_line_color=DOWN_COLOR, showlegend=False, opacity=0.6),
                row=rr, col=1, secondary_y=False)
        else:
            fig.add_trace(_line_trace(closed_x, low["close"].to_numpy(float), f"cierre {adx_tf['timeframe']}",
                                      "#90a4ae", width=1.0, showlegend=False, opacity=0.7),
                          row=rr, col=1, secondary_y=False)
        fig.add_trace(_line_trace(closed_x, adx_values, f"ADX {adx_tf['timeframe']}", NEUTRAL_COLOR, width=1.5),
                      row=rr, col=1, secondary_y=True)
        fig.add_trace(go.Scatter(
            x=[int(closed_x[0]), int(closed_x[-1])], y=[adx_tf["key_level"], adx_tf["key_level"]],
            mode="lines", name=f"nivel ADX {adx_tf['key_level']:g}", legendgroup="adx-key",
            line=dict(color=ADX_EXIT_COLOR, width=0.9, dash="dash")), row=rr, col=1, secondary_y=True)
        flags = adx_tf["reversal"]
        if flags.any():
            fig.add_trace(_marker_trace(closed_x[flags], adx_values[flags], "reversal ADX",
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
        bar_x = _x_ms(low.index)
        closed_x = _closed_x_ms(low.index, weak_tf["timeframe"])
        if len(low) <= _MAX_CANDLES:
            fig.add_trace(go.Candlestick(
                x=bar_x, open=_sig(low["open"]), high=_sig(low["high"]),
                low=_sig(low["low"]), close=_sig(low["close"]),
                name=f"precio {weak_tf['timeframe']}", increasing_line_color=UP_COLOR,
                decreasing_line_color=DOWN_COLOR, showlegend=False, opacity=0.6), row=rr, col=1)
        else:
            fig.add_trace(_line_trace(closed_x, low["close"].to_numpy(float), f"cierre {weak_tf['timeframe']}",
                                      "#90a4ae", width=1.0, showlegend=False, opacity=0.7), row=rr, col=1)
        fig.add_trace(_line_trace(closed_x, weak_tf["ema_fast"], f"EMA {cfg.ema_fast} ({weak_tf['timeframe']})",
                                  FAST_COLOR, width=1.2), row=rr, col=1)
        fig.add_trace(_line_trace(closed_x, weak_tf["ema_slow"], f"EMA {cfg.ema_slow} ({weak_tf['timeframe']})",
                                  SLOW_COLOR, width=1.2), row=rr, col=1)
        flags = weak_tf["weakening"]
        if flags.any():
            hi = low["high"].to_numpy(float)
            fig.add_trace(_marker_trace(closed_x[flags], hi[flags], "weakening",
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

    if "drawdown" in row:
        rr = row["drawdown"]
        drawdown = equity["drawdown_pct"].to_numpy(float)
        fig.add_trace(_line_trace(
            _x_ms(equity["timestamp"]), drawdown, "drawdown", DOWN_COLOR,
            width=1.3, fill="tozeroy", fillcolor="rgba(239,83,80,0.18)",
        ), row=rr, col=1)
        fig.add_hline(y=0, line=dict(color="#90a4ae", width=0.9, dash="dash"),
                      row=rr, col=1)
        fig.update_yaxes(title_text="drawdown %", ticksuffix="%", row=rr, col=1)

    if "summary" in row:
        rr = row["summary"]
        labels = [item[0] for item in summary_rows]
        pnl_values = [item[1] for item in summary_rows]
        detail = [[item[2], item[3]] for item in summary_rows]
        colors = [UP_COLOR if value >= 0 else DOWN_COLOR for value in pnl_values]
        fig.add_trace(go.Bar(
            x=pnl_values, y=labels, orientation="h", name="PnL neto",
            marker=dict(color=colors, opacity=0.88),
            customdata=detail,
            hovertemplate=(
                "%{y}<br>PnL neto: %{x:+,.2f}<br>"
                "tramos: %{customdata[0]} · posiciones: %{customdata[1]}<extra></extra>"
            ),
        ), row=rr, col=1)
        fig.add_vline(x=0, line=dict(color="#90a4ae", width=0.9, dash="dash"),
                      row=rr, col=1)
        fig.update_xaxes(title_text="PnL neto", row=rr, col=1, zeroline=False)
        fig.update_yaxes(title_text="lado / motivo de salida", row=rr, col=1,
                         categoryorder="array", categoryarray=labels)

    # -- Layout ------------------------------------------------------------
    for name in (["macd", "atr", "mfi", "er", "target", "equity", "drawdown"]
                 + adx_entry_panels):
        if name in row:
            fig.update_xaxes(matches="x", row=row[name], col=1)
    for name in panels:
        fig.update_xaxes(
            rangeslider_visible=False,
            type="linear" if name == "summary" else "date",
            row=row[name],
            col=1,
        )

    base_title = title or (f"{source or 'backtest'} · {cfg.timeframe} · {len(bars):,} velas · "
                           f"{trade_count} posiciones")
    base_height = max(_MIN_HEIGHT, _PANEL_HEIGHT * len(panels))
    if summary_rows:
        base_height = max(
            base_height,
            _PANEL_HEIGHT * (len(panels) - 1) + 180 + 30 * len(summary_rows),
        )
    fig.update_layout(
        title=dict(text=base_title, x=0.5, xanchor="center"),
        template="plotly_dark", height=base_height, autosize=True,
        margin=dict(l=84, r=84, t=90, b=50), hovermode="closest",
        legend=dict(orientation="h", x=0.5, xanchor="center", y=1.02,
                    title=dict(text="● entrada · ■ salida")), bargap=0.15,
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


def _write_paginated_backtest_html(
    path: Path,
    source: str,
    cfg: Config,
    bars: pd.DataFrame,
    pages: list[dict],
) -> None:
    """Escribe un HTML paginado que renderiza cada panel mensual por separado."""
    import html
    from plotly.offline import get_plotlyjs_version

    page_json = json.dumps(pages, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    # Evita que contenido de los datos pueda cerrar la etiqueta <script>.
    page_json = page_json.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    safe_source = html.escape(source or "backtest")
    title = html.escape(f"Backtest {source or 'backtest'} · {cfg.timeframe}")
    plotly_js_version = get_plotlyjs_version()
    path.write_text(
        f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <script src="https://cdn.plot.ly/plotly-{plotly_js_version}.min.js"></script>
  <style>
    :root {{
      color-scheme:dark;
      --page:#0b1118;
      --surface:#111a24;
      --surface-raised:#172330;
      --border:#263544;
      --text:#e8eef5;
      --muted:#91a1b2;
      --accent:#56c2d6;
    }}
    *, *::before, *::after {{ box-sizing:border-box; }}
    html, body {{ width:100%; min-height:100%; margin:0; background:var(--page); color:var(--text); }}
    body {{ overflow-x:hidden; font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
            line-height:1.45; -webkit-font-smoothing:antialiased; }}
    .page-header {{
      position:relative; z-index:2; display:flex; align-items:center; justify-content:space-between;
      gap:1.5rem; padding:1.1rem clamp(1rem,3vw,2.5rem);
      background:rgba(17,26,36,.96); border-bottom:1px solid var(--border);
      box-shadow:0 8px 24px rgba(0,0,0,.16);
    }}
    .title-block {{ min-width:0; }}
    .eyebrow {{ display:block; margin-bottom:.25rem; color:var(--accent); font-size:.68rem;
                font-weight:700; letter-spacing:.14em; text-transform:uppercase; }}
    h1 {{ margin:0; color:var(--text); font-size:clamp(1.1rem,2vw,1.45rem);
          font-weight:650; letter-spacing:-.025em; overflow-wrap:anywhere; }}
    .subtitle {{ margin:.3rem 0 0; color:var(--muted); font-size:.86rem; }}
    .pager {{
      display:grid; grid-template-columns:auto minmax(10rem,auto) auto; align-items:center;
      gap:.45rem; padding:.35rem; border:1px solid var(--border); border-radius:.75rem;
      background:var(--page); flex-shrink:0;
    }}
    .pager-button {{
      min-height:2.45rem; padding:.55rem .85rem; border:1px solid transparent; border-radius:.5rem;
      background:var(--surface-raised); color:var(--text); font:inherit; font-size:.85rem;
      font-weight:600; cursor:pointer; transition:background .15s ease,border-color .15s ease,
        color .15s ease,opacity .15s ease;
    }}
    .pager-button:hover:not(:disabled) {{ border-color:#3c5668; background:#203141; }}
    .pager-button:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
    .pager-button:disabled {{ color:#637282; cursor:not-allowed; opacity:.62; }}
    .page-label {{ min-width:10rem; padding:0 .35rem; color:#c8d4df; text-align:center;
                   font-size:.82rem; font-variant-numeric:tabular-nums; }}
    main {{ display:block; width:100%; padding:0; }}
    .chart-shell {{
      display:block; width:100%; max-width:none; margin:0; overflow:hidden;
      padding:16px;
      border:1px solid var(--border);
      background:#000; box-shadow:0 18px 45px rgba(0,0,0,.22);
    }}
    .chart-title {{ margin:0; padding:0; color:var(--text); text-align:center;
                    font-size:1rem; font-weight:600; }}
    .chart-panels {{ display:flex; flex-direction:column; gap:12px; width:100%; }}
    .panel-card {{ width:100%; min-width:0; margin:0; padding:12px; overflow:hidden;
                   border:1px solid var(--border); border-radius:.75rem;
                   background:var(--surface); }}
    .panel-title {{ margin:0; padding:0; color:var(--text); text-align:center;
                    font-size:.9rem; font-weight:600; line-height:1.3; }}
    .panel-plot {{ display:block; width:100%; min-width:0; max-width:100%; margin:0; padding:0; }}
    .panel-plot .js-plotly-plot,
    .panel-plot .plot-container,
    .panel-plot .svg-container {{
      box-sizing:border-box !important; width:100% !important; max-width:100% !important;
    }}
    @media (max-width:700px) {{
      .page-header {{ align-items:stretch; flex-direction:column; gap:.9rem; padding:1rem; }}
      .pager {{ width:100%; grid-template-columns:auto minmax(0,1fr) auto; }}
      .page-label {{ min-width:0; }}
      .chart-shell {{ padding:8px; }}
      .chart-panels {{ gap:8px; }}
      .panel-card {{ padding:8px; border-radius:.55rem; }}
    }}
    @media (max-width:390px) {{
      .pager-button {{ padding:.5rem .6rem; font-size:.8rem; }}
      .page-label {{ font-size:.75rem; }}
    }}
  </style>
</head>
<body>
  <header class="page-header">
    <div class="title-block">
      <span class="eyebrow">Estrategia · Backtest</span>
      <h1>Backtest {safe_source}</h1>
      <p class="subtitle">{cfg.timeframe} · {len(bars):,} velas · {len(pages)} meses</p>
    </div>
    <nav class="pager" aria-label="Paginación mensual">
      <button class="pager-button" id="previous" type="button">← Anterior</button>
      <span class="page-label" id="page-label" aria-live="polite"></span>
      <button class="pager-button" id="next" type="button">Siguiente →</button>
    </nav>
  </header>
  <main><section class="chart-shell" aria-label="Gráficos mensuales del backtest"><div class="chart-title" id="chart-title"></div><div class="chart-panels" id="chart-panels"></div></section></main>
  <script id="monthly-data" type="application/json">{page_json}</script>
  <script>
    const pages = JSON.parse(document.getElementById("monthly-data").textContent);
    const chartHeight = Math.max(
      0,
      ...pages.map(page => Number(page.figure.layout.height) || 0)
    );
    const panelsRoot = document.getElementById("chart-panels");
    const chartTitle = document.getElementById("chart-title");
    const label = document.getElementById("page-label");
    const previous = document.getElementById("previous");
    const next = document.getElementById("next");
    const config = {{responsive:true, displaylogo:false, scrollZoom:true,
                     displayModeBar:"hover",
                     modeBarButtonsToRemove:["select2d","lasso2d"]}};
    let current = 0;
    let activePlots = [];
    let renderSequence = 0;

    function axisNumber(name) {{
      return Number(name.replace(/^[xy]axis/, "")) || 1;
    }}

    function remapReference(reference, axisMap, fallback) {{
      const value = reference || fallback;
      const parts = String(value).split(" ");
      if (parts[0] === "paper") return value;
      const mapped = axisMap.get(parts[0]);
      return mapped ? [mapped, ...parts.slice(1)].join(" ") : value;
    }}

    function remapItems(items, xMap, yMap, xRef, yRef) {{
      return (items || []).filter(item => {{
        const oldX = String(item.xref || xRef).split(" ")[0];
        const oldY = String(item.yref || yRef).split(" ")[0];
        return (oldX === "paper" || xMap.has(oldX))
          && (oldY === "paper" || yMap.has(oldY))
          && !(oldX === "paper" && oldY === "paper");
      }}).map(item => ({{
        ...item,
        xref: remapReference(item.xref, xMap, xRef),
        yref: remapReference(item.yref, yMap, yRef)
      }}));
    }}

    function splitFigure(figure) {{
      const sourceLayout = figure.layout || {{}};
      const xAxes = Object.entries(sourceLayout)
        .filter(([name, axis]) => name.startsWith("xaxis") && axis?.domain)
        .sort(([a], [b]) => axisNumber(a) - axisNumber(b));

      return xAxes.map(([xAxisName, sourceXAxis]) => {{
        const xRef = xAxisName.replace("axis", "");
        const yAxes = Object.entries(sourceLayout)
          .filter(([name, axis]) => name.startsWith("yaxis") && axis?.anchor === xRef)
          .sort(([, a], [, b]) => Number(Boolean(b.domain)) - Number(Boolean(a.domain)));
        const yMap = new Map(yAxes.map(([name], i) => [
          name.replace("axis", ""), i === 0 ? "y" : `y${{i + 1}}`
        ]));
        const xMap = new Map([[xRef, "x"]]);
        const traces = figure.data.filter(trace => (trace.xaxis || "x") === xRef);
        const data = traces.map(trace => ({{
          ...trace,
          xaxis: "x",
          yaxis: yMap.get(trace.yaxis || "y") || "y"
        }}));
        const primaryYAxis = yAxes.find(([, axis]) => axis.domain) || yAxes[0];
        const domain = primaryYAxis?.[1].domain || [0, 1];
        const legendItems = traces.filter(trace => trace.showlegend !== false && trace.name);
        const legendWidth = legendItems.reduce(
          (total, trace) => total + String(trace.name).length * 7 + 34, 0
        );
        const legendRows = legendItems.length
          ? Math.max(1, Math.ceil(legendWidth / Math.max(320, window.innerWidth - 96)))
          : 0;
        const legendMargin = legendItems.length
          ? Math.min(180, 20 + legendRows * 24)
          : 20;
        const panelHeight = Math.max(
          280,
          Math.round(Math.max(0, chartHeight - 340) * (domain[1] - domain[0]))
            + legendMargin + 40
        );
        const layout = {{
          autosize:true,
          height:panelHeight,
          template:sourceLayout.template,
          font:sourceLayout.font,
          paper_bgcolor:sourceLayout.paper_bgcolor,
          plot_bgcolor:sourceLayout.plot_bgcolor,
          hovermode:sourceLayout.hovermode,
          bargap:sourceLayout.bargap,
          margin:{{l:48, r:48, t:legendMargin, b:24}},
          showlegend:legendItems.length > 0,
          legend:{{...sourceLayout.legend, x:0.5, y:1.02, yanchor:"bottom"}}
        }};

        const xAxis = {{...sourceXAxis, domain:[0, 1], anchor:"y"}};
        delete xAxis.matches;
        layout.xaxis = xAxis;

        yAxes.forEach(([axisName, sourceYAxis], i) => {{
          const newRef = yMap.get(axisName.replace("axis", ""));
          const newAxisName = `yaxis${{newRef === "y" ? "" : newRef.slice(1)}}`;
          const axis = {{...sourceYAxis, anchor:"x"}};
          if (sourceYAxis.domain) axis.domain = [0, 1];
          if (sourceYAxis.overlaying) axis.overlaying = yMap.get(sourceYAxis.overlaying) || "y";
          delete axis.matches;
          delete axis.title;
          layout[newAxisName] = axis;
        }});

        const yRef = yAxes[0]?.[0].replace("axis", "") || "y";
        layout.shapes = remapItems(sourceLayout.shapes, xMap, yMap, xRef, yRef);
        layout.annotations = remapItems(sourceLayout.annotations, xMap, yMap, xRef, yRef);
        const title = yAxes.map(([, axis]) => axis.title?.text).filter(Boolean).join(" · ");

        return {{title, data, layout}};
      }}).filter(panel => panel.data.length);
    }}

    async function showPage(index) {{
      if (!pages.length) {{
        label.textContent = "No hay meses para mostrar";
        previous.disabled = true;
        next.disabled = true;
        return;
      }}
      current = index;
      const sequence = ++renderSequence;
      const page = pages[current];
      label.textContent = `${{page.label}} · ${{page.positions}} posiciones (${{current + 1}}/${{pages.length}})`;
      previous.disabled = true;
      next.disabled = true;
      const pageTitle = page.figure.layout?.title;
      chartTitle.textContent = typeof pageTitle === "string" ? pageTitle : pageTitle?.text || "";
      const panels = splitFigure(page.figure);

      activePlots.forEach(plot => Plotly.purge(plot));
      activePlots = [];
      panelsRoot.replaceChildren();
      const renders = panels.map(panel => {{
        const card = document.createElement("section");
        card.className = "panel-card";
        const heading = document.createElement("h2");
        heading.className = "panel-title";
        heading.textContent = panel.title;
        const plot = document.createElement("div");
        plot.className = "panel-plot";
        card.append(heading, plot);
        panelsRoot.appendChild(card);
        activePlots.push(plot);
        return Plotly.newPlot(plot, panel.data, panel.layout, config);
      }});

      try {{
        await Promise.all(renders);
      }} catch (error) {{
        console.error("Error al dibujar los paneles:", error);
        panelsRoot.textContent = "No se pudieron renderizar los gráficos de este mes.";
      }}
      if (sequence !== renderSequence) return;
      previous.disabled = current === 0;
      next.disabled = current === pages.length - 1;
    }}

    previous.addEventListener("click", () => showPage(current - 1));
    next.addEventListener("click", () => showPage(current + 1));
    window.addEventListener("resize", () => {{
      activePlots.forEach(plot => Plotly.Plots.resize(plot));
    }});
    showPage(0);
  </script>
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
    volume_exit: np.ndarray | None = None,
    raw: pd.DataFrame | None = None,
    source: str = "",
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    index_path: str | Path | None = None,
) -> list[Path]:
    """Genera un único HTML de Plotly con paginación por mes y paneles aislados.

    Cada página cubre un mes calendario, pero los indicadores se calculan sobre
    la serie completa y solo se recortan al mostrar, así que no se pierde el
    warmup de las EMAs/ADX. Cada panel se renderiza en su propio contenedor.
    El archivo se guarda en ``index_path`` (por defecto
    ``output_dir/html/backtest.html``) y los botones Anterior/Siguiente cambian
    los gráficos al mes seleccionado.
    """
    index_path = (Path(index_path) if index_path is not None
                  else Path(output_dir) / _PLOTLY_SUBDIR / "backtest.html")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    pages = []
    for label, start, end in _month_ranges(bars.index):
        month_positions = 0
        if trades is not None and not trades.empty:
            overlap = trades[(trades["entry_time"] < end) & (trades["exit_time"] >= start)]
            month_positions = int(overlap["position_id"].nunique())
        figure = build_backtest_figure(
            bars, cfg, signals=signals, trades=trades, equity=equity,
            adx_exit=adx_exit, weak_exit=weak_exit, rsi_div_exit=rsi_div_exit, volume_exit=volume_exit, raw=raw, source=source,
            title=f"{source or 'backtest'} · {label} · {month_positions} posiciones",
            window=(start, end),
        )
        pages.append({
            "label": label,
            "positions": month_positions,
            "figure": json.loads(figure.to_json()),
        })
    _write_paginated_backtest_html(index_path, source, cfg, bars, pages)
    print(f"[PLOTLY] HTML mensual en {index_path.resolve()} ({len(pages)} meses)", flush=True)
    return [index_path]


def generate_report(trades: pd.DataFrame, equity: pd.DataFrame, cfg: Config, data: pd.DataFrame,
                    source: str, output_dir: str | Path = DEFAULT_OUTPUT_DIR,
                    write_outputs_enabled: bool = True, write_equity_csv: bool = False,
                    plot_enabled: bool = True, plot_path: str | Path | None = None,
                    plot_dpi: int = 150, raw: pd.DataFrame | None = None,
                    signals: pd.DataFrame | None = None, adx_exit: np.ndarray | None = None,
                    weak_exit: np.ndarray | None = None,
                    rsi_div_exit: np.ndarray | None = None,
                    volume_exit: np.ndarray | None = None,
                    plotly_path: str | Path | None = None) -> dict:
    """Calcula métricas, imprime el informe, guarda outputs y genera los gráficos.

    Punto de entrada único que unifica `calculate_metrics`, `print_report`,
    `equity_curve_from_trades`, `build_report`, `write_outputs`, `plot_equity` y
    `build_monthly_backtest_htmls`. Por defecto escribe un único HTML paginado
    por mes en `output_dir/html/backtest.html`.
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
            adx_exit=adx_exit, weak_exit=weak_exit, rsi_div_exit=rsi_div_exit, volume_exit=volume_exit, raw=raw, source=source,
            output_dir=output_dir, index_path=plotly_path,
        )
    return metrics
