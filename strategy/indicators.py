from __future__ import annotations

import numpy as np
import pandas as pd

from strategy.config import Config
from strategy.utils import resample_ohlcv, source_minutes


def ema(values: pd.Series, period: int) -> pd.Series:
    if period <= 0:
        raise ValueError("El periodo EMA debe ser positivo.")
    return values.ewm(alpha=2 / (period + 1), adjust=False).mean()


def _wilder_rma(values: np.ndarray, length: int) -> np.ndarray:
    """RMA de Wilder, sembrado con la primera SMA completa."""
    if length <= 0:
        raise ValueError("La longitud RMA debe ser positiva.")
    values = np.asarray(values, dtype=float)
    out = np.full(len(values), np.nan, dtype=float)
    seed: list[float] = []
    previous = np.nan
    for i, value in enumerate(values):
        if not np.isfinite(value):
            seed.clear()
            previous = np.nan
            continue
        if np.isfinite(previous):
            previous = (previous * (length - 1) + value) / length
        else:
            seed.append(float(value))
            if len(seed) < length:
                continue
            previous = float(np.mean(seed))
            seed.clear()
        out[i] = previous
    return out


def _wilder_dm_sum(values: np.ndarray, length: int) -> np.ndarray:
    """Suma direccional suavizada como en Wilder, antes de calcular el DI."""
    if length <= 0:
        raise ValueError("La longitud DMI debe ser positiva.")
    values = np.asarray(values, dtype=float)
    out = np.full(len(values), np.nan, dtype=float)
    seed: list[float] = []
    previous = np.nan
    for i, value in enumerate(values):
        if not np.isfinite(value):
            seed.clear()
            previous = np.nan
            continue
        if not np.isfinite(previous):
            if length == 1:
                previous = 0.0
            else:
                seed.append(float(value))
                if len(seed) < length - 1:
                    continue
                previous = float(np.sum(seed))
                seed.clear()
                continue
        previous = previous - previous / length + value
        out[i] = previous
    return out


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
    avg_gain = _wilder_rma(gain.to_numpy(float), length)
    avg_loss = _wilder_rma(loss.to_numpy(float), length)
    out = np.full(len(values), np.nan, dtype=float)
    valid = np.isfinite(avg_gain) & np.isfinite(avg_loss) & (avg_loss != 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        out[valid] = 100.0 - 100.0 / (1.0 + avg_gain[valid] / avg_loss[valid])
    out[(avg_loss == 0) & (avg_gain > 0)] = 100.0
    out[(avg_gain == 0) & (avg_loss == 0)] = 50.0
    return pd.Series(out, index=values.index)


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
    return pd.Series(_wilder_rma(tr, length), index=bars.index)


def gaussian_filter_trend(
    bars: pd.DataFrame,
    length: int = 14,
    poles: int = 4,
    atr_length: int = 14,
    fixed_multiplier: float = 1.5,
    adaptive_width: bool = True,
    efficiency_length: int = 10,
    trend_multiplier: float = 0.8,
    chop_multiplier: float = 2.5,
    efficiency_smooth: int = 5,
    source: str = "close",
) -> pd.DataFrame:
    """Gaussian Filter Trend line and direction, matching the  indicator.

    ``trend_dir`` is +1 when the stepped main line is bullish/rising, -1 when
    bearish/falling, and 0 before its first directional step. The efficiency
    ratio and ATR use only current and earlier candles, so this is safe to use
    as a closed-bar entry filter.
    """
    if length < 2 or not 1 <= poles <= 4:
        raise ValueError("Gaussian length must be >= 2 and poles must be in [1, 4].")
    if (atr_length <= 0 or efficiency_length < 2 or efficiency_smooth <= 0
            or fixed_multiplier <= 0 or trend_multiplier <= 0 or chop_multiplier <= 0):
        raise ValueError("Invalid Gaussian Filter Trend parameters.")
    if source not in {"close", "hl2", "hlc3"}:
        raise ValueError("Gaussian source must be 'close', 'hl2' or 'hlc3'.")

    source_values = (
        bars["close"].to_numpy(dtype=float)
        if source == "close"
        else (
            (bars["high"] + bars["low"]).to_numpy(dtype=float) / 2.0
            if source == "hl2"
            else (bars["high"] + bars["low"] + bars["close"]).to_numpy(dtype=float) / 3.0
        )
    )
    n = len(source_values)
    filtered = np.full(n, np.nan, dtype=float)
    beta = (1.0 - np.cos(2.0 * np.pi / length)) / (
        np.power(1.414, 2.0 / poles) - 1.0
    )
    alpha = -beta + np.sqrt(beta * beta + 2.0 * beta)

    # Four cascaded one-pole stages; the selected pole count determines output.
    stages = np.full((4, n), np.nan, dtype=float)
    for i, value in enumerate(source_values):
        if not np.isfinite(value):
            continue
        previous = value
        for stage in range(4):
            prior = stages[stage, i - 1] if i else np.nan
            if not np.isfinite(prior):
                prior = value
            stages[stage, i] = alpha * previous + (1.0 - alpha) * prior
            previous = stages[stage, i]
        filtered[i] = stages[poles - 1, i]

    # ta.atr is an RMA seeded by the first full SMA window.
    high, low = bars["high"].to_numpy(float), bars["low"].to_numpy(float)
    tr = (
        true_range(high, low, bars["close"].to_numpy(dtype=float))
        if n else np.empty(0, dtype=float)
    )
    atr_values = np.full(n, np.nan, dtype=float)
    if n >= atr_length:
        atr_values[atr_length - 1] = np.mean(tr[:atr_length])
        for i in range(atr_length, n):
            atr_values[i] = (
                atr_values[i - 1] * (atr_length - 1) + tr[i]
            ) / atr_length

    source_series = pd.Series(source_values, index=bars.index)
    net_move = (source_series - source_series.shift(efficiency_length)).abs()
    path_length = source_series.diff().abs().rolling(
        efficiency_length, min_periods=efficiency_length
    ).sum()
    efficiency = net_move.div(path_length.where(path_length != 0))
    efficiency = efficiency.mask(path_length.eq(0), 0.0).clip(0.0, 1.0)
    smoothed_efficiency = efficiency.ewm(
        alpha=1.0 / efficiency_smooth, adjust=False
    ).mean().to_numpy(float)

    if adaptive_width:
        width_multiplier = chop_multiplier + (
            trend_multiplier - chop_multiplier
        ) * smoothed_efficiency
    else:
        width_multiplier = np.full(n, fixed_multiplier, dtype=float)
    width = atr_values * width_multiplier
    upper_band, lower_band = filtered + width, filtered - width

    trend_line = np.full(n, np.nan, dtype=float)
    trend_dir = np.zeros(n, dtype=int)
    if n:
        trend_line[0] = filtered[0]
        for i in range(1, n):
            previous_line = trend_line[i - 1]
            current_line = previous_line
            if not np.isfinite(current_line):
                current_line = filtered[i]
            if np.isfinite(upper_band[i]) and upper_band[i] < current_line:
                current_line = upper_band[i]
            if np.isfinite(lower_band[i]) and lower_band[i] > current_line:
                current_line = lower_band[i]
            trend_line[i] = current_line
            trend_dir[i] = trend_dir[i - 1]
            if np.isfinite(previous_line) and current_line > previous_line:
                trend_dir[i] = 1
            elif np.isfinite(previous_line) and current_line < previous_line:
                trend_dir[i] = -1

    return pd.DataFrame(
        {
            "filtered_price": filtered,
            "trend_line": trend_line,
            "trend_dir": trend_dir,
            "atr": atr_values,
            "efficiency_ratio": efficiency.to_numpy(float),
            "smoothed_efficiency": smoothed_efficiency,
            "source": source_values,
        },
        index=bars.index,
    )


def atr_volatility_filter(
    bars: pd.DataFrame,
    length: int = 14,
    lookback: int = 50,
    min_ratio: float = 0.5,
    max_ratio: float = 2.5,
) -> pd.DataFrame:
    """Mide ATR relativo a su media reciente y determina si permite entradas.

    La media usa exclusivamente los ``lookback`` ATR anteriores, evitando que
    la vela actual diluya una compresión o un pico de volatilidad. Hasta contar
    con una ventana completa el filtro permite la señal (sin referencia fiable).
    """
    if length <= 0 or lookback <= 0:
        raise ValueError("Las longitudes ATR deben ser positivas.")
    if min_ratio < 0 or max_ratio <= min_ratio:
        raise ValueError("Los umbrales relativos ATR deben ser válidos.")

    current = atr(bars, length)
    baseline = current.shift(1).rolling(lookback, min_periods=lookback).mean()
    ready = baseline.notna()
    ratio = current.div(baseline)
    # Si el mercado previo fue completamente plano, un ATR actual cero es
    # compresión y uno positivo sigue siendo anómalo frente a esa referencia.
    zero_baseline = ready & baseline.eq(0)
    ratio = ratio.mask(zero_baseline & current.eq(0), 0.0)
    ratio = ratio.mask(zero_baseline & current.gt(0), np.inf)
    allowed = ~ready | (ratio.ge(min_ratio) & ratio.le(max_ratio))
    return pd.DataFrame(
        {
            "atr": current,
            "atr_mean": baseline,
            "atr_ratio": ratio,
            "atr_filter_ready": ready,
            "atr_filter_allowed": allowed,
        },
        index=bars.index,
    )


def adx(bars: pd.DataFrame, dilen: int = 14, adxlen: int = 14) -> pd.DataFrame:
    """ADX/DI con suavizado Wilder; salida alineada con el índice de entrada."""
    if dilen <= 0 or adxlen <= 0:
        raise ValueError("Las longitudes ADX deben ser positivas.")
    n = len(bars)
    out = pd.DataFrame(np.nan, index=bars.index, columns=["adx", "plus_di", "minus_di"])
    if not n:
        return out
    h, l, c = (bars[k].to_numpy(float) for k in ("high", "low", "close"))
    up = np.full(n, np.nan, dtype=float)
    down = np.full(n, np.nan, dtype=float)
    if n > 1:
        up[1:] = np.diff(h)
        down[1:] = -np.diff(l)
    tr = true_range(h, l, c)
    valid_ohlc = np.isfinite(h) & np.isfinite(l) & np.isfinite(c)
    tr[~valid_ohlc] = np.nan
    # ADX/DI debe reiniciar todas las series si cualquiera de sus componentes
    # no puede calcularse; de lo contrario TR y DM se suavizan sobre ventanas
    # distintas tras una vela incompleta.
    valid_movement = (
        np.isfinite(up)
        & np.isfinite(down)
        & np.isfinite(tr)
        & valid_ohlc
    )
    tr[~valid_movement] = np.nan
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    plus_dm[~valid_movement] = np.nan
    minus_dm[~valid_movement] = np.nan
    # DMI/ADX no dispone de movimiento direccional para la primera barra.
    plus_dm[0] = minus_dm[0] = np.nan
    tr[0] = np.nan
    tr_rma = _wilder_dm_sum(tr, dilen)
    p_rma = _wilder_dm_sum(plus_dm, dilen)
    m_rma = _wilder_dm_sum(minus_dm, dilen)

    valid = np.isfinite(tr_rma)
    pdi = np.full(n, np.nan, dtype=float)
    mdi = np.full(n, np.nan, dtype=float)
    nonzero_tr = valid & (tr_rma != 0)
    pdi[nonzero_tr] = 100 * p_rma[nonzero_tr] / tr_rma[nonzero_tr]
    mdi[nonzero_tr] = 100 * m_rma[nonzero_tr] / tr_rma[nonzero_tr]
    pdi[valid & (tr_rma == 0)] = 0.0
    mdi[valid & (tr_rma == 0)] = 0.0

    dx = np.full(n, np.nan, dtype=float)
    valid_di = np.isfinite(pdi) & np.isfinite(mdi)
    denom = pdi + mdi
    nonzero_denom = valid_di & (denom != 0)
    dx[nonzero_denom] = 100 * np.abs(pdi[nonzero_denom] - mdi[nonzero_denom]) / denom[nonzero_denom]
    dx[valid_di & (denom == 0)] = 0.0
    av = _wilder_rma(dx, adxlen)
    out["adx"] = av
    out["plus_di"] = pdi
    out["minus_di"] = mdi
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


def align_closed_events(
    source_index: pd.DatetimeIndex,
    events: np.ndarray,
    target_index: pd.DatetimeIndex,
    source_tf: str,
    target_tf: str,
) -> np.ndarray:
    """Alinea cada evento fuente a una única vela destino ya cerrada.

    Cada evento se asigna a la primera vela destino cuyo cierre ocurre en o
    después del cierre de la vela fuente. A diferencia de
    :func:`align_closed_values`, un evento no se mantiene activo en las velas
    destino posteriores. Si varios eventos coinciden en una vela destino,
    prevalece el último evento fuente.
    """
    events = np.asarray(events)
    if len(events) != len(source_index):
        raise ValueError("events debe tener la misma longitud que source_index.")
    out = np.zeros(len(target_index), dtype=events.dtype)
    if not len(target_index) or not len(source_index):
        return out

    source_end = (source_index + pd.Timedelta(source_tf)).asi8
    target_end = (target_index + pd.Timedelta(target_tf)).asi8
    loc = np.searchsorted(target_end, source_end, side="left")
    active = np.flatnonzero(events != 0)
    target_loc = loc[active]
    valid = target_loc < len(target_index)
    # Los eventos fuente están ordenados temporalmente; asignar en orden hace
    # que el último evento prevalezca si varios caen en la misma vela destino.
    out[target_loc[valid]] = events[active[valid]]
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
    return align_closed_events(low.index, flags, bars.index, timeframe, cfg.timeframe)


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
    return align_closed_events(
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
    raw: pd.DataFrame | None, bars: pd.DataFrame, cfg: Config, timeframe: str
) -> np.ndarray | None:
    """Flags de divergencia RSI para un único timeframe, alineadas a ``bars``."""
    timeframe_delta = pd.Timedelta(timeframe)
    strategy_delta = pd.Timedelta(cfg.timeframe)
    if timeframe_delta < strategy_delta:
        if raw is None or raw.empty:
            raise ValueError(
                f"El timeframe de divergencia RSI ({timeframe}) requiere datos raw "
                "de mayor resolución que el timeframe de estrategia."
            )
        raw_delta = pd.Timedelta(minutes=source_minutes(raw))
        if raw_delta > timeframe_delta:
            raise ValueError(
                f"Los datos raw ({raw_delta}) no tienen resolución suficiente "
                f"para el timeframe de divergencia RSI ({timeframe})."
            )
        source_bars = raw if raw_delta == timeframe_delta else resample_ohlcv(
            raw, timeframe
        )
        source_tf = timeframe
    elif raw is None or raw.empty:
        if timeframe_delta <= pd.Timedelta(cfg.timeframe):
            source_bars, source_tf = bars, cfg.timeframe
        else:
            source_bars, source_tf = resample_ohlcv(bars, timeframe), timeframe
    elif timeframe_delta <= pd.Timedelta(cfg.timeframe) or pd.Timedelta(
        minutes=source_minutes(raw)
    ) >= timeframe_delta:
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
    aligned = align_closed_events(
        source_bars.index, flags, bars.index, source_tf, cfg.timeframe
    )
    return aligned.astype(int)


def _build_rsi_divergence_flags(
    raw: pd.DataFrame | None,
    bars: pd.DataFrame,
    cfg: Config,
    timeframes: tuple[str, ...],
) -> np.ndarray | None:
    """Combina divergencias RSI con OR y las alinea a las velas de estrategia."""
    if bars.empty:
        return None
    bull = np.zeros(len(bars), bool)
    bear = np.zeros(len(bars), bool)
    built = False
    for timeframe in timeframes or (cfg.timeframe,):
        flags = _rsi_divergence_flags(raw, bars, cfg, timeframe)
        if flags is None:
            continue
        built = True
        bull |= flags > 0
        bear |= flags < 0
    if not built:
        return None
    # A single signed flag cannot represent both directions at once. Match
    # rsi_divergence_flags' documented tie-break: bearish takes precedence.
    return np.where(bear, -1, bull.astype(int)).astype(int)


def build_rsi_divergence_flags(
    raw: pd.DataFrame | None, bars: pd.DataFrame, cfg: Config
) -> np.ndarray | None:
    """Divergencias RSI usadas por el filtro de entrada."""
    return _build_rsi_divergence_flags(
        raw, bars, cfg, cfg.entry_rsi_divergence_timeframes
    )


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
    if not cfg.exit_rsi_divergence_enabled or bars.empty:
        return None
    return _build_rsi_divergence_flags(
        raw, bars, cfg, cfg.exit_rsi_divergence_timeframes
    )
