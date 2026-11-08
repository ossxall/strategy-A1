from __future__ import annotations

import numpy as np
import pandas as pd

from strategy.config import Config
from strategy.utils import resample_ohlcv, source_minutes


def ema(values: pd.Series, period: int) -> pd.Series:
    if period <= 0:
        raise ValueError("El periodo EMA debe ser positivo.")
    return values.ewm(alpha=2 / (period + 1), adjust=False).mean()


def ema_band_touch_flags(
    bars: pd.DataFrame, period: int = 55, tolerance_pct: float = 0.1
) -> tuple[np.ndarray, np.ndarray]:
    """Devuelve (EMA, toques) cuando el rango OHLC se acerca/cruza la banda EMA.

    Una vela toca la EMA si su rango ``[low, high]`` intersecta la banda
    ``EMA ± tolerance_pct``. Incluye tanto cruces como roces cercanos.
    """
    if period <= 0 or tolerance_pct < 0:
        raise ValueError("Periodo EMA positivo y tolerancia no negativa requeridos.")
    line = ema(bars["close"], period).to_numpy(float)
    high = bars["high"].to_numpy(float)
    low = bars["low"].to_numpy(float)
    band = np.abs(line) * tolerance_pct / 100.0
    touched = (
        np.isfinite(line)
        & (low <= line + band)
        & (high >= line - band)
    )
    return line, touched


def efficiency_ratio(values: pd.Series, period: int = 20) -> pd.Series:
    """Kaufman Efficiency Ratio: avance neto / recorrido total en ``period`` velas.

    Devuelve valores en [0, 1]; 0 significa movimiento lateral y 1 movimiento
    completamente direccional. Usa sólo cierres hasta la vela actual.
    """
    if period <= 0:
        raise ValueError("El periodo Efficiency Ratio debe ser positivo.")
    change = (values - values.shift(period)).abs()
    path = values.diff().abs().rolling(period, min_periods=period).sum()
    ratio = change / path.replace(0.0, np.nan)
    return ratio.replace([np.inf, -np.inf], np.nan).fillna(0.0).clip(0.0, 1.0)


def rsi(values: pd.Series, length: int = 14) -> pd.Series:
    """RSI (suavizado Wilder) alineado con el índice de entrada.

    Devuelve el RSI de ``values`` en ``[0, 100]``; con media de pérdidas a cero
    se devuelve 100 (mercado sin caídas) y con ganancias y pérdidas a cero, 50
    (mercado plano, sin señal direccional).
    """
    if length <= 0:
        raise ValueError("La longitud RSI debe ser positiva.")
    delta = values.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    out[avg_loss == 0] = 100.0
    out[(avg_gain == 0) & (avg_loss == 0)] = 50.0
    return out


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    prev = np.r_[np.nan, close[:-1]]
    tr = np.maximum.reduce([high - low, np.abs(high - prev), np.abs(low - prev)])
    if len(tr):
        tr[0] = high[0] - low[0]
    return tr


def atr(bars: pd.DataFrame, length: int = 14) -> pd.Series:
    """ATR con suavizado Wilder, alineado con el índice de ``bars``.

    Devuelve el Average True Range de ``bars`` (timeframe de la estrategia).
    Útil como medida de volatilidad relativa: ``atr(bars) / bars.close``.
    """
    if length <= 0:
        raise ValueError("La longitud ATR debe ser positiva.")
    h, l, c = (bars[k].to_numpy(float) for k in ("high", "low", "close"))
    tr = true_range(h, l, c)
    return pd.Series(tr, index=bars.index).ewm(alpha=1 / length, adjust=False).mean()


def adx(bars: pd.DataFrame, dilen: int = 14, adxlen: int = 14) -> pd.DataFrame:
    """ADX/DI con suavizado Wilder; salida alineada con el índice de entrada."""
    if dilen <= 0 or adxlen <= 0:
        raise ValueError("Las longitudes ADX deben ser positivas.")
    n = len(bars)
    out = pd.DataFrame(np.nan, index=bars.index, columns=["adx", "plus_di", "minus_di"])
    if not n:
        return out
    h, l, c = (bars[k].to_numpy(float) for k in ("high", "low", "close"))
    up = np.r_[0.0, np.diff(h)]
    down = np.r_[0.0, -np.diff(l)]
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    smooth = lambda x, a: pd.Series(x).ewm(alpha=a, adjust=False).mean().to_numpy()
    tr_rma = smooth(true_range(h, l, c), 1 / dilen)
    p_rma, m_rma = smooth(plus_dm, 1 / dilen), smooth(minus_dm, 1 / dilen)
    safe = np.where(tr_rma != 0, tr_rma, 1.0)
    pdi = np.where(tr_rma != 0, 100 * p_rma / safe, 0.0)
    mdi = np.where(tr_rma != 0, 100 * m_rma / safe, 0.0)
    denom = np.where(pdi + mdi != 0, pdi + mdi, 1.0)
    dx = 100 * np.abs(pdi - mdi) / denom
    av = smooth(dx, 1 / adxlen)
    first = dilen + adxlen - 2
    if first < n:
        out.iloc[first:, :] = np.column_stack([av[first:], pdi[first:], mdi[first:]])
    return out


def confirmed_crosses(cross: np.ndarray, side_ok: np.ndarray, bars: int) -> np.ndarray:
    cross, side_ok = np.asarray(cross, bool), np.asarray(side_ok, bool)
    if bars <= 0:
        return cross.copy()
    out = np.zeros(len(cross), dtype=bool)
    for idx in np.flatnonzero(cross):
        end = idx + bars
        if end < len(cross) and side_ok[idx : end + 1].all():
            out[end] = True
    return out


def adx_reversal_flags(values: np.ndarray, key_level: float) -> np.ndarray:
    values = np.asarray(values, float)
    flags = np.zeros(len(values), dtype=bool)
    if len(values) >= 3:
        flags[2:] = (
            (values[2:] < values[1:-1])
            & (values[1:-1] > values[:-2])
            & (values[1:-1] > key_level)
        )
    return flags


def ema_weakening_flags(
    fast: np.ndarray, slow: np.ndarray, gap_pct: float
) -> np.ndarray:
    fast, slow = np.asarray(fast, float), np.asarray(slow, float)
    flags = np.zeros(len(fast), dtype=bool)
    if len(fast) < 2 or gap_pct <= 0:
        return flags
    with np.errstate(divide="ignore", invalid="ignore"):
        gap = np.abs(fast - slow) / slow
    flags[1:] = (gap[:-1] > gap_pct) & (gap[1:] <= gap_pct)
    return flags


def align_closed_flags(
    source_index: pd.DatetimeIndex,
    flags: np.ndarray,
    target_index: pd.DatetimeIndex,
    source_tf: str,
    target_tf: str,
) -> np.ndarray:
    """Para cada cierre del timeframe destino, toma la última barra fuente ya cerrada."""
    if not len(target_index) or not len(source_index):
        return np.zeros(len(target_index), dtype=bool)
    source_end = (source_index + pd.Timedelta(source_tf)).asi8
    target_end = (target_index + pd.Timedelta(target_tf)).asi8
    loc = np.searchsorted(source_end, target_end, side="right") - 1
    out = np.zeros(len(target_index), dtype=bool)
    valid = (loc >= 0) & (loc < len(flags))
    out[valid] = np.asarray(flags, bool)[loc[valid]]
    return out


def align_closed_values(
    source_index: pd.DatetimeIndex,
    values: np.ndarray,
    target_index: pd.DatetimeIndex,
    source_tf: str,
    target_tf: str,
    fill: float = 0.0,
) -> np.ndarray:
    """Como `align_closed_flags` pero para valores numéricos.

    Para cada cierre del timeframe destino toma el valor de la última barra
    fuente ya cerrada; si no hay ninguna, rellena con ``fill``.
    """
    values = np.asarray(values)
    out = np.full(len(target_index), fill, dtype=float)
    if not len(target_index) or not len(source_index):
        return out
    source_end = (source_index + pd.Timedelta(source_tf)).asi8
    target_end = (target_index + pd.Timedelta(target_tf)).asi8
    loc = np.searchsorted(source_end, target_end, side="right") - 1
    valid = (loc >= 0) & (loc < len(values))
    out[valid] = values[loc[valid]]
    return out


def htf_trend(
    bars: pd.DataFrame,
    timeframe: str,
    fast_len: int,
    slow_len: int,
    strategy_timeframe: str,
) -> np.ndarray | None:
    """Tendencia (+1/-1/0) del timeframe superior, alineada a ``bars``.

    Calcula ``EMA(fast_len)`` y ``EMA(slow_len)`` sobre las velas de
    ``timeframe`` (remuestreadas desde ``bars``) y devuelve ``+1`` cuando la
    EMA rápida está por encima de la lenta, ``-1`` cuando está por debajo y
    ``0`` cuando son iguales o no hay dato. Para cada barra de ``bars`` se usa
    la última vela del HTF ya cerrada (sin look-ahead). Devuelve ``None`` si no
    hay velas suficientes para sembrar las EMAs.
    """
    if pd.Timedelta(timeframe) == pd.Timedelta(strategy_timeframe):
        source, source_tf = bars, strategy_timeframe
    else:
        source, source_tf = resample_ohlcv(bars, timeframe), timeframe
    if len(source) < max(fast_len, slow_len) + 2:
        return None
    fast = ema(source.close, fast_len).to_numpy(float)
    slow = ema(source.close, slow_len).to_numpy(float)
    trend = np.where(fast > slow, 1.0, np.where(fast < slow, -1.0, 0.0))
    if source.index.equals(bars.index):
        return trend
    return align_closed_values(
        source.index, trend, bars.index, source_tf, strategy_timeframe
    )


def spread_flags(flags: np.ndarray, window: int) -> np.ndarray:
    """Mantiene activo cada flag durante ``window`` velas consecutivas.

    Equivale a que en cada vela la señal se active si hubo una activación en
    cualquiera de las ``window`` velas anteriores (incluida la actual).
    """
    flags = np.asarray(flags, bool)
    if window <= 1 or not flags.any():
        return flags
    cumulative = np.concatenate([[0], np.cumsum(flags)])
    idx = np.arange(len(flags))
    start = np.maximum(idx - window + 1, 0)
    return (cumulative[idx + 1] - cumulative[start]) > 0


def _adx_exit_flags(
    raw: pd.DataFrame, bars: pd.DataFrame, cfg: Config, timeframe: str
) -> np.ndarray | None:
    """Flags de reversión ADX para un único timeframe, alineadas a ``bars``."""
    if pd.Timedelta(minutes=source_minutes(raw)) >= pd.Timedelta(timeframe):
        return None
    low = resample_ohlcv(raw, timeframe)
    if len(low) < cfg.adx_dilen + cfg.adx_adxlen + 2:
        return None
    flags = adx_reversal_flags(
        adx(low, cfg.adx_dilen, cfg.adx_adxlen)["adx"].to_numpy(), cfg.adx_key_level
    )
    return align_closed_flags(low.index, flags, bars.index, timeframe, cfg.timeframe)


def build_adx_exit(
    raw: pd.DataFrame, bars: pd.DataFrame, cfg: Config
) -> np.ndarray | None:
    """Salida ADX combinando con OR todos los ``cfg.adx_timeframes``.

    Devuelve ``None`` si no se pudo calcular ninguno (deshabilitado, sin datos
    suficientes o timeframes incompatibles); si al menos uno es válido, devuelve
    la unión de sus reversiones, mantenida activa ``cfg.adx_exit_range`` velas
    del timeframe de la estrategia tras cada reversión.
    """
    if not cfg.adx_exit_enabled or bars.empty:
        return None
    combined = np.zeros(len(bars), dtype=bool)
    built = False
    for timeframe in cfg.adx_timeframes:
        flags = _adx_exit_flags(raw, bars, cfg, timeframe)
        if flags is None:
            continue
        built = True
        combined |= flags
    if not built:
        return None
    return spread_flags(combined, cfg.adx_exit_range)


def _weakening_exit_flags(
    raw: pd.DataFrame, bars: pd.DataFrame, cfg: Config, timeframe: str
) -> np.ndarray | None:
    """Flags de weakening para un único timeframe, alineadas a ``bars``."""
    if pd.Timedelta(timeframe) == pd.Timedelta(cfg.timeframe) or pd.Timedelta(
        minutes=source_minutes(raw)
    ) >= pd.Timedelta(timeframe):
        source_bars, source_tf = bars, cfg.timeframe
    else:
        source_bars, source_tf = resample_ohlcv(raw, timeframe), timeframe
    fast_len, slow_len = cfg.ema_weakening_lengths
    if len(source_bars) < slow_len + 2:
        return None
    flags = ema_weakening_flags(
        ema(source_bars.close, fast_len).to_numpy(),
        ema(source_bars.close, slow_len).to_numpy(),
        cfg.ema_weakening_gap_pct,
    )
    if source_bars.index.equals(bars.index):
        return flags
    return align_closed_flags(
        source_bars.index, flags, bars.index, source_tf, cfg.timeframe
    )


def build_weakening_exit(
    raw: pd.DataFrame, bars: pd.DataFrame, cfg: Config
) -> np.ndarray | None:
    """Salida EMA weakening combinando con OR todos los ``cfg.ema_weakening_timeframes``.

    Devuelve ``None`` si no se pudo calcular ninguno (deshabilitado, sin datos
    suficientes o timeframes incompatibles); si al menos uno es válido, devuelve
    la unión de sus señales de debilitamiento.
    """
    if not cfg.ema_weakening_exit_enabled or bars.empty:
        return None
    combined = np.zeros(len(bars), dtype=bool)
    built = False
    for timeframe in cfg.ema_weakening_timeframes or (cfg.timeframe,):
        flags = _weakening_exit_flags(raw, bars, cfg, timeframe)
        if flags is None:
            continue
        built = True
        combined |= flags
    if not built:
        return None
    return combined


def rsi_divergence_flags(
    high: np.ndarray,
    low: np.ndarray,
    rsi_values: np.ndarray,
    pivot: int,
    lookback: int,
) -> np.ndarray:
    """Divergencias RSI respecto a los pivotes previos del precio.

    Devuelve ``+1`` en divergencia **alcista** (el precio hace mínimos más
    bajos pero el RSI mínimos más altos), ``-1`` en **bajista** (el precio hace
    máximos más altos pero el RSI máximos más bajos) y ``0`` sin señal.

    Un pivote es un máximo/mínimo local estricto respecto de las ``pivot``
    velas a su izquierda (los empates con la derecha se resuelven en la barra
    más a la izquierda, para no duplicar señales). Sólo se compara con el pivote
    anterior del mismo tipo dentro de ``lookback`` velas y el flag se emite en
    la vela que **confirma** el pivote (índice del pivote + ``pivot``), de modo
    que en esa vela sólo se usan datos ya cerrados: no hay lookahead. Si
    coinciden divergencias alcista y bajista en la misma vela, gana la bajista.
    """
    high = np.asarray(high, float)
    low = np.asarray(low, float)
    rsi_values = np.asarray(rsi_values, float)
    n = len(high)
    flags = np.zeros(n, dtype=int)
    if pivot < 1 or lookback < 1 or len(low) != n or len(rsi_values) != n or n < 2 * pivot + 1:
        return flags
    # Máximo/mínimo de las `pivot` velas a cada lado (excluida la propia).
    # Para el lado derecho se recorre la serie con `.iloc[::-1]`, que invierte
    # el orden pero conserva las etiquetas del índice.
    high_s, low_s = pd.Series(high), pd.Series(low)
    left_high = high_s.rolling(pivot).max().shift(1)
    left_low = low_s.rolling(pivot).min().shift(1)
    right_high = high_s.iloc[::-1].rolling(pivot).max().shift(1).iloc[::-1]
    right_low = low_s.iloc[::-1].rolling(pivot).min().shift(1).iloc[::-1]
    swing_high = ((high_s > left_high) & (high_s >= right_high)).fillna(False).to_numpy(bool)
    swing_low = ((low_s < left_low) & (low_s <= right_low)).fillna(False).to_numpy(bool)
    prev_high = prev_low = None
    for confirm in range(2 * pivot, n):
        p = confirm - pivot  # pivote que acaba de confirmarse en esta vela
        if swing_high[p]:
            if (prev_high is not None and p - prev_high <= lookback
                    and high[p] > high[prev_high] and rsi_values[p] < rsi_values[prev_high]):
                flags[confirm] = -1  # máximos al alza con el RSI a la baja
            prev_high = p
        if swing_low[p]:
            if (prev_low is not None and p - prev_low <= lookback
                    and low[p] < low[prev_low] and rsi_values[p] > rsi_values[prev_low]
                    and flags[confirm] == 0):
                flags[confirm] = 1  # mínimos a la baja con el RSI al alza
            prev_low = p
    return flags


def _rsi_divergence_flags(
    raw: pd.DataFrame, bars: pd.DataFrame, cfg: Config, timeframe: str
) -> np.ndarray | None:
    """Flags de divergencia RSI para un único timeframe, alineadas a ``bars``."""
    if pd.Timedelta(timeframe) == pd.Timedelta(cfg.timeframe) or pd.Timedelta(
        minutes=source_minutes(raw)
    ) >= pd.Timedelta(timeframe):
        source_bars, source_tf = bars, cfg.timeframe
    else:
        source_bars, source_tf = resample_ohlcv(raw, timeframe), timeframe
    if len(source_bars) < 2 * cfg.rsi_divergence_pivot + 1:
        return None
    flags = rsi_divergence_flags(
        source_bars["high"].to_numpy(float),
        source_bars["low"].to_numpy(float),
        rsi(source_bars.close, cfg.rsi_divergence_len).to_numpy(float),
        cfg.rsi_divergence_pivot,
        cfg.rsi_divergence_lookback,
    )
    if source_bars.index.equals(bars.index):
        return flags
    aligned = align_closed_values(
        source_bars.index, flags, bars.index, source_tf, cfg.timeframe
    )
    return np.nan_to_num(aligned, nan=0.0).astype(int)


def build_rsi_divergence_exit(
    raw: pd.DataFrame, bars: pd.DataFrame, cfg: Config
) -> np.ndarray | None:
    """Salida por divergencia RSI combinando con OR todos los timeframes.

    Devuelve ``None`` si no se pudo calcular ninguna (deshabilitado, sin datos
    suficientes o timeframes incompatibles); si al menos una es válida, devuelve
    un array entero alineado a ``bars`` con ``+1`` (divergencia alcista),
    ``-1`` (bajista) y ``0`` (sin señal). Al coincidir ambas direcciones gana
    la bajista.
    """
    if not cfg.rsi_divergence_exit_enabled or bars.empty:
        return None
    bull = np.zeros(len(bars), bool)
    bear = np.zeros(len(bars), bool)
    built = False
    for timeframe in cfg.rsi_divergence_timeframes or (cfg.timeframe,):
        flags = _rsi_divergence_flags(raw, bars, cfg, timeframe)
        if flags is None:
            continue
        built = True
        bull |= flags > 0
        bear |= flags < 0
    if not built:
        return None
    return bull.astype(int) - bear.astype(int)
