#!/usr/bin/env python3
"""
Strategy 2 — EMA Cross + ADX / EMA Weakening
============================================

Gestión de salida por defecto: take-profit parcial a 1R (50%) y stop a
breakeven del resto (`partial_tp_enabled`). Sube el win rate y recorta la
racha de pérdidas en whipsaw; puede desactivarse con `partial_tp_enabled=False`.

Además, tras una entrada por cross EMA, un toque a la EMA slow cierra una parte
configurable de la posición (`close_on_ema_slow_touch_exit_pct`, 100 = cierre
total) una sola vez por posición. La tolerancia `close_on_ema_slow_touch_pct`
desplaza el nivel de disparo; con `close_on_ema_slow_touch_after=False` (default)
se aplica antes del toque y con `True` después.

Cuando la entrada se completa por confirmación (scale-in), una salida parcial
adicional cierra `entry_confirm_move_exit_portion_pct` % de la posición si el
precio se mueve `entry_confirm_move_exit_pct` % en contra del precio de la
entrada de confirmación (`entry_confirm_move_exit_enabled`; 50 % a 0,3 % por
defecto, una sola vez por posición).

La salida por debilitamiento de las EMAs (`ema_weakening_exit_enabled`) cierra
`ema_weakening_exit_pct` % de la posición cuando el cruce de las EMAs del
timeframe `ema_weakening_timeframe` se estrecha por debajo de
`ema_weakening_gap_pct` (paso de separado a convergido). Sus periodos son
independientes de los del cruce de entrada: `ema_weakening_fast` /
`ema_weakening_slow` (0 = reutilizar `ema_fast` / `ema_slow`).

Y, por último, una **divergencia RSI en contra de la posición cierra el 100 %
de la misma** (`rsi_divergence_exit_enabled`): la divergencia bajista (precio
con máximos más altos, RSI con máximos más bajos) cierra un LONG y la alcista
(precio con mínimos más bajos, RSI con mínimos más altos) cierra un SHORT. El
pivote de precio se confirma tras `rsi_divergence_pivot` velas y se compara con
el pivote anterior del mismo tipo en `rsi_divergence_lookback` velas.

Los patrones TA-Lib también pueden cerrar/reducir una posición. Los de
reversión y los bidireccionales se consideran contra la posición según su signo;
un doji sólo confirma el patrón. Además, el precio debe romper el extremo de la
vela y la posición debe superar el umbral de pérdida configurado, para evitar
salidas por ruido (`candle_pattern_exit_*`).

Archivo único con API reutilizable para backtesting y bots.

Protecciones de riesgo (ver `Config`; el filtro de régimen y el escalado de
riesgo por mes arrancan desactivados, la de racha, la mensual y el tamaño
adaptativo vienen activos):

* `streak_trend_only` (default True): tras una posición cerrada en pérdida,
  la siguiente entrada sólo se abre si la señal coincide con el sesgo de
  tendencia de `trend_timeframe` (vela superior ya cerrada, sin lookahead).
  Corta las rachas de -1R del whipsaw sin recortar los cruces en tendencia.
* `monthly_loss_limit_pct` (default 5.0): si la equity cae más de X% por
  debajo de la equity al inicio del mes calendario se cierra la posición (opcional)
  y no se abren nuevas hasta el siguiente mes (o `monthly_loss_limit_days` días).
* `monthly_trend_gate_pct` (default 0 = off) + `monthly_gate_mode` ("trend" |
  "all"): con el mes acumulando ≥ X% de pérdida, las nuevas entradas sólo se
  abren a favor del sesgo de tendencia (o se bloquean todas, en modo "all").
  Sobre el drawdown del mes, no sobre la racha de posiciones: en meses en
  verde no toca nada, así que no recorta los cruces ganadores.
* `monthly_risk_scaling` (default off) + `monthly_risk_scaling_min` (0.35):
  el tamaño de cada nueva posición baja en proporción al drawdown del mes
  (dd 0 → 1, dd `monthly_loss_limit_pct` → mínimo), para que las entradas de
  la segunda mitad de un mes rojo no profundicen el mes.
* `consecutive_loss_pause` (default 0) + `consecutive_loss_pause_bars` (96):
  espera de 48h (en 30min) antes de volver a entrar tras cada posición cerrada
  en pérdida. Con `streak_trend_only` activo no baja la racha y cuesta
  rendimiento, así que arranca apagada.
* `trend_filter` ("off" | "cross" | "slow") y `chop_filter`: filtro de régimen
  permanente; sólo se opera en la dirección de la tendencia de `trend_timeframe`
  / con el ADX de `chop_timeframe` por encima del mínimo.
* `adaptive_risk_window` (default 20) + `adaptive_risk_k`: escala `risk_pct`
  con el win rate de las últimas n posiciones: penaliza las rachas frías
  (donde se concentran los meses rojos) y reutiliza el tamaño en las cálidas.

Reducción de meses rojos (BTCUSDT 2020-2026, 30min, 82 meses). Partiendo del
uso previo (`streak_trend_only`, `monthly_loss_limit_pct=5`, `adaptive_risk_window=0`):

| variante                                   | meses en rojo | peor mes | suma meses rojos | retorno | max DD | PF   |
|--------------------------------------------|---------------|----------|------------------|---------|--------|------|
| sin tamaño adaptativo                      | 25 (30,5 %)   | -3,15 %  | -28,6 %          | 477,9 % | -8,70% | 2,43 |
| `adaptive_risk_window=20` (**default**)    | 25 (30,5 %)   | -2,49 %  | -26,1 %          | 439,8 % | -7,96% | 2,49 |
| `+ monthly_trend_gate_pct=0.3`             | 25 (30,5 %)   | -2,17 %  | -25,5 %          | 495,6 % | -8,23% | 2,52 |
| `+ monthly_trend_gate_pct=0.4`             | 25 (30,5 %)   | -2,45 %  | -24,0 %          | 447,2 % | -7,46% | 2,58 |

El mismo recorte se repite en train (2020-2023) y test (2024-2026) y con costes
realistas (comisión 4 bps + slippage 2 bps), donde el tamaño adaptativo también
mejora el drawdown (-10,1 % -> -8,2 %). El `monthly_trend_gate_pct` es más
dependiente del régimen: se probó con costes y con `trend_filter="cross"`, donde
no aporta, por eso arranca desactivado.

Uso:
    python -m strategy.strategy dataset/binance/BTCUSDT/merged/BTCUSDT-1m.parquet --output-dir backtest_out
    python -m strategy.strategy dataset/binance/BTCUSDT/merged/BTCUSDT-1m.parquet --no-entry-adx-filter
    python -m strategy.strategy dataset/binance/BTCUSDT/merged/BTCUSDT-1m.parquet --help

API para bots:
    cfg = Config()
    bars = load_parquet("dataset/binance/BTCUSDT/merged/BTCUSDT-1m.parquet")
    strategy = EMACrossStrategy(cfg)
    signals = strategy.generate_signals(bars)
    # signals contiene señales conocidas al cierre de cada vela.
    # En ejecución real, actuar en la siguiente apertura/iteración.

Dependencias:
    pip install "pandas>=2" pyarrow numpy matplotlib TA-Lib
"""
from __future__ import annotations
from dataclasses import asdict
from typing import Optional
from strategy.config import Config
from strategy.models import (
    Position,
    Trade,
    CROSS_REASON,
    MONTHLY_LIMIT_REASON,
    RSI_DIVERGENCE_BULL_REASON,
    RSI_DIVERGENCE_BEAR_REASON,
    DEFAULT_OUTPUT_DIR,
    empty_trades,
)
from strategy.indicators import (
    ema,
    ema_band_touch_flags,
    efficiency_ratio,
    adx,
    confirmed_crosses,
    align_closed_values,
    build_adx_exit,
    build_weakening_exit,
    build_rsi_divergence_exit,
)
from strategy.utils import (
    load_parquet,
    resample_ohlcv,
    source_minutes,
    to_timeframe
)
from strategy.report import generate_report
import argparse
import time
import talib
import numpy as np
import pandas as pd

# Patrones unidireccionales según la clasificación solicitada. Los patrones
# bidireccionales se interpretan por el signo que devuelve TA-Lib. Los dojis
# se publican como información, pero nunca confirman una entrada por sí solos.
LONG_CANDLE_PATTERNS = (
    "CDLHAMMER",
    "CDLINVERTEDHAMMER",
    "CDLPIERCING",
    "CDLMORNINGSTAR",
    "CDLMORNINGDOJISTAR",
    "CDL3WHITESOLDIERS",
    "CDLHOMINGPIGEON",
    "CDLMATCHINGLOW",
    "CDLLADDERBOTTOM",
    "CDLSTICKSANDWICH",
    "CDLUNIQUE3RIVER",
    "CDL3STARSINSOUTH",
    "CDLCONCEALBABYSWALL",
    "CDLTAKURI",
    "CDLMATHOLD",
)

SHORT_CANDLE_PATTERNS = (
    "CDLHANGINGMAN",
    "CDLSHOOTINGSTAR",
    "CDLDARKCLOUDCOVER",
    "CDLEVENINGSTAR",
    "CDLEVENINGDOJISTAR",
    "CDL3BLACKCROWS",
    "CDLIDENTICAL3CROWS",
    "CDL2CROWS",
    "CDLUPSIDEGAP2CROWS",
    "CDLADVANCEBLOCK",
    "CDLSTALLEDPATTERN",
    "CDLINNECK",
    "CDLONNECK",
    "CDLTHRUSTING",
)

SIGNED_CANDLE_PATTERNS = (
    "CDLENGULFING",
    "CDLHARAMI",
    "CDLHARAMICROSS",
    "CDL3INSIDE",
    "CDL3OUTSIDE",
    "CDL3LINESTRIKE",
    "CDLABANDONEDBABY",
    "CDLKICKING",
    "CDLKICKINGBYLENGTH",
    "CDLBREAKAWAY",
    "CDLCOUNTERATTACK",
    "CDLDOJISTAR",
    "CDLTRISTAR",
    "CDLHIKKAKE",
    "CDLHIKKAKEMOD",
    "CDLSEPARATINGLINES",
    "CDLTASUKIGAP",
    "CDLGAPSIDESIDEWHITE",
    "CDLXSIDEGAP3METHODS",
    "CDLRISEFALL3METHODS",
    "CDLBELTHOLD",
    "CDLMARUBOZU",
    "CDLCLOSINGMARUBOZU",
    "CDLLONGLINE",
    "CDLSHORTLINE",
    "CDLSPINNINGTOP",
    "CDLHIGHWAVE",
)

NEUTRAL_CANDLE_PATTERNS = (
    "CDLDOJI",
    "CDLDRAGONFLYDOJI",
    "CDLGRAVESTONEDOJI",
    "CDLLONGLEGGEDDOJI",
    "CDLRICKSHAWMAN",
)

# ---------------------------------------------------------------------------
# Estrategia: compartida por backtest y bots
# ---------------------------------------------------------------------------

class EMACrossStrategy:
    """Generador de señales sin estado de cartera ni ejecución de órdenes."""

    def __init__(self, config: Config):
        self.cfg = config

    def _upper_bars(self, bars: pd.DataFrame, timeframe: str) -> Optional[tuple[pd.DataFrame, str]]:
        """Velas del timeframe superior alineadas a ``bars`` (o ``None``)."""
        if pd.Timedelta(timeframe) <= pd.Timedelta(self.cfg.timeframe):
            upper, upper_tf = bars, self.cfg.timeframe
        else:
            try:
                upper, upper_tf = resample_ohlcv(bars, timeframe), timeframe
            except ValueError:
                return None
        if len(upper) < 2:
            return None
        return upper, upper_tf

    def trend_bias(self, bars: pd.DataFrame) -> np.ndarray:
        """Sesgo de tendencia (+1 alcista, -1 bajista, 0 sin información).

        Calcula el sesgo en ``cfg.trend_timeframe`` y lo alinea a cada barra del
        timeframe de la estrategia usando **sólo la vela superior ya cerrada**
        (ver :func:`align_closed_values`), de modo que no hay lookahead. Con
        ``cfg.trend_filter == "off"`` devuelve todo ceros (sin filtro).
        """
        cfg = self.cfg
        if (cfg.trend_filter == "off" and not cfg.streak_trend_only) or not len(bars):
            return np.zeros(len(bars), int)
        upper = self._upper_bars(bars, cfg.trend_timeframe)
        if upper is None:
            return np.zeros(len(bars), int)
        bars_upper, upper_tf = upper
        close = bars_upper.close.to_numpy(float)
        if cfg.trend_filter == "slow":
            level = ema(bars_upper.close, cfg.ema_slow).to_numpy(float)
            direction = np.where(close > level, 1.0, -1.0)
        else:
            fast = ema(bars_upper.close, cfg.ema_fast).to_numpy(float)
            slow = ema(bars_upper.close, cfg.ema_slow).to_numpy(float)
            direction = np.sign(fast - slow)
        aligned = align_closed_values(bars_upper.index, direction, bars.index, upper_tf, cfg.timeframe)
        return np.where(np.isfinite(aligned), np.sign(aligned), 0.0).astype(int)

    def chop_ok(self, bars: pd.DataFrame) -> np.ndarray:
        """``True`` donde el ADX de ``cfg.chop_timeframe`` (ya cerrado) supera el mínimo."""
        cfg = self.cfg
        if not cfg.chop_filter or not len(bars):
            return np.ones(len(bars), bool)
        upper = self._upper_bars(bars, cfg.chop_timeframe)
        if upper is None:
            return np.zeros(len(bars), bool)
        bars_upper, upper_tf = upper
        needed = cfg.adx_dilen + cfg.adx_adxlen + 2
        if len(bars_upper) < needed:
            return np.zeros(len(bars), bool)
        values = adx(bars_upper, cfg.adx_dilen, cfg.adx_adxlen)["adx"].to_numpy(float)
        aligned = align_closed_values(bars_upper.index, values, bars.index, upper_tf, cfg.timeframe)
        return np.nan_to_num(aligned, nan=-np.inf) >= cfg.chop_adx_min

    def _candle_confirmations(
        self, bars: pd.DataFrame, raw: Optional[pd.DataFrame] = None
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Detecta confirmaciones LONG/SHORT y dojis neutrales en varios marcos.

        Los patrones de marcos distintos se asignan al cierre de estrategia que
        los contiene. Dentro de un mismo marco prevalece el patrón más reciente;
        entre marcos, prevalece el marco mayor. Así se evita que una señal menor
        y antigua sobreescriba contexto reciente, sin usar velas abiertas.
        """
        empty = np.zeros(len(bars), dtype=bool)
        if bars.empty:
            return empty.copy(), empty.copy(), empty.copy()

        strategy_delta = pd.Timedelta(self.cfg.timeframe)
        frames: list[tuple[pd.DataFrame, pd.Timedelta]] = []
        seen: set[tuple[int, int]] = set()

        def add_frame(frame: pd.DataFrame, delta: pd.Timedelta) -> None:
            if frame.empty or delta <= pd.Timedelta(0):
                return
            key = (int(delta.value), int(frame.index[0].value))
            if key not in seen:
                seen.add(key)
                frames.append((frame, delta))

        # El marco de estrategia tiene la máxima prioridad y sigue siendo útil
        # aunque sea mayor que 30m.
        add_frame(bars, strategy_delta)

        if raw is not None and not raw.empty:
            source_delta = pd.Timedelta(minutes=source_minutes(raw))
            # En estrategias de 5m o menos no hay agregados estándar inferiores;
            # usar el origen. En marcos mayores, evitar el ruido de velas de 1m
            # cuando se pueden construir agregados de 5m/15m/30m.
            if source_delta < strategy_delta and (
                source_delta >= pd.Timedelta("5min")
                or strategy_delta <= pd.Timedelta("5min")
            ):
                add_frame(raw, source_delta)
            for timeframe in ("5min", "15min", "30min", "1h", "4h", "1D"):
                target_delta = pd.Timedelta(timeframe)
                if source_delta < target_delta < strategy_delta:
                    try:
                        add_frame(resample_ohlcv(raw, timeframe), target_delta)
                    except ValueError:
                        continue

        def align_latest(values: np.ndarray, source_index: pd.DatetimeIndex,
                         source_delta: pd.Timedelta) -> np.ndarray:
            """Último evento cerrado de cada timeframe al cierre de estrategia."""
            values = np.asarray(values)
            if source_index.equals(bars.index) and source_delta == strategy_delta:
                return values.copy()
            aligned = np.zeros(len(bars), dtype=values.dtype)
            source_ends = (source_index + source_delta).asi8
            target_ends = (bars.index + strategy_delta).asi8
            target_starts = bars.index.asi8
            # side=left asigna una vela que cierra exactamente junto con la
            # estrategia a esa misma barra (y nunca a una anterior).
            target_locs = np.searchsorted(target_ends, source_ends, side="left")
            valid = (target_locs < len(bars)) & (source_ends <= target_ends[-1])
            safe_locs = np.minimum(target_locs, len(bars) - 1)
            # No arrastrar un patrón anterior al primer intervalo disponible ni
            # asignar señales que caigan dentro de huecos del timeframe destino.
            valid &= source_ends > target_starts[safe_locs]
            for source_loc in np.flatnonzero(valid & (values != 0)):
                aligned[target_locs[source_loc]] = values[source_loc]
            return aligned

        # Resolución descendente: una señal direccional del marco más alto gana;
        # si ese marco no tiene patrón (o tiene conflicto), se consulta el menor.
        frame_signals: list[tuple[pd.Timedelta, np.ndarray, np.ndarray, np.ndarray]] = []
        for frame, source_delta in sorted(frames, key=lambda item: item[1], reverse=True):
            o, h, low, c = (
                np.ascontiguousarray(frame[column].to_numpy(dtype=np.float64))
                for column in ("open", "high", "low", "close")
            )
            bullish = np.zeros(len(frame), dtype=bool)
            bearish = np.zeros(len(frame), dtype=bool)
            neutral = np.zeros(len(frame), dtype=bool)
            for pattern_name in LONG_CANDLE_PATTERNS:
                bullish |= getattr(talib, pattern_name)(o, h, low, c) != 0
            for pattern_name in SHORT_CANDLE_PATTERNS:
                bearish |= getattr(talib, pattern_name)(o, h, low, c) != 0
            for pattern_name in SIGNED_CANDLE_PATTERNS:
                pattern = getattr(talib, pattern_name)(o, h, low, c)
                bullish |= pattern > 0
                bearish |= pattern < 0
            for pattern_name in NEUTRAL_CANDLE_PATTERNS:
                neutral |= getattr(talib, pattern_name)(o, h, low, c) != 0

            # Un conflicto dentro del mismo marco no aporta dirección. Para
            # direccionales, conservar sólo el evento más reciente del marco.
            direction = np.zeros(len(frame), dtype=np.int8)
            direction[bullish & ~bearish] = 1
            direction[bearish & ~bullish] = -1
            direction[bullish & bearish] = 2
            aligned_direction = align_latest(direction, frame.index, source_delta)
            aligned_neutral = align_latest(neutral.astype(np.int8), frame.index, source_delta) > 0
            frame_signals.append((source_delta, aligned_direction == 1,
                                  aligned_direction == -1, aligned_neutral))

        long_confirmation = np.zeros(len(bars), dtype=bool)
        short_confirmation = np.zeros(len(bars), dtype=bool)
        neutral_confirmation = np.zeros(len(bars), dtype=bool)
        unresolved = np.ones(len(bars), dtype=bool)
        for _, bullish, bearish, neutral in frame_signals:
            has_direction = bullish ^ bearish
            accepted = unresolved & has_direction
            long_confirmation |= accepted & bullish
            short_confirmation |= accepted & bearish
            unresolved &= ~accepted
            neutral_confirmation |= neutral

        return long_confirmation, short_confirmation, neutral_confirmation

    def _candle_exit_patterns(self, bars: pd.DataFrame) -> tuple[
        np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray
    ]:
        """Clasifica TA-Lib en reversión alcista/bajista, signo bidireccional y doji.

        Las salidas se calculan en el timeframe de estrategia (sin patrones de
        marcos inferiores, que generaban ruido). Los dojis son sólo confluencia:
        por sí solos nunca indican dirección ni cierran una posición.
        """
        n = len(bars)
        reversal_bull = np.zeros(n, dtype=bool)
        reversal_bear = np.zeros(n, dtype=bool)
        signed_bull = np.zeros(n, dtype=bool)
        signed_bear = np.zeros(n, dtype=bool)
        neutral = np.zeros(n, dtype=bool)
        if not n:
            return reversal_bull, reversal_bear, signed_bull, signed_bear, neutral

        o, h, low, c = (
            np.ascontiguousarray(bars[column].to_numpy(dtype=np.float64))
            for column in ("open", "high", "low", "close")
        )
        for name in LONG_CANDLE_PATTERNS:
            reversal_bull |= getattr(talib, name)(o, h, low, c) != 0
        for name in SHORT_CANDLE_PATTERNS:
            reversal_bear |= getattr(talib, name)(o, h, low, c) != 0
        for name in SIGNED_CANDLE_PATTERNS:
            values = getattr(talib, name)(o, h, low, c)
            signed_bull |= values > 0
            signed_bear |= values < 0
        for name in NEUTRAL_CANDLE_PATTERNS:
            neutral |= getattr(talib, name)(o, h, low, c) != 0
        return reversal_bull, reversal_bear, signed_bull, signed_bear, neutral

    def _long_candle_confirmation(
        self, bars: pd.DataFrame, raw: Optional[pd.DataFrame] = None
    ) -> np.ndarray:
        """Compatibilidad con consumidores del antiguo detector alcista."""
        return self._candle_confirmations(bars, raw)[0]

    def generate_signals(
        self, bars: pd.DataFrame, raw: Optional[pd.DataFrame] = None
    ) -> pd.DataFrame:
        cfg = self.cfg
        fast = ema(bars.close, cfg.ema_fast).to_numpy(float)
        slow = ema(bars.close, cfg.ema_slow).to_numpy(float)
        prev_fast, prev_slow = np.r_[np.nan, fast[:-1]], np.r_[np.nan, slow[:-1]]
        up = (fast > slow) & (prev_fast <= prev_slow)
        down = (fast < slow) & (prev_fast >= prev_slow)
        valid = np.isfinite(fast) & np.isfinite(slow) & np.isfinite(prev_fast) & np.isfinite(prev_slow)
        if cfg.confirm_bars:
            up = confirmed_crosses(up, fast > slow, cfg.confirm_bars)
            down = confirmed_crosses(down, fast < slow, cfg.confirm_bars)
        entry_dmi = (
            adx(bars, cfg.entry_adx_len, cfg.entry_adx_len)
            if cfg.entry_adx_enabled or cfg.entry_dmi_risk_scaling_enabled
            else None
        )
        entry_adx = (
            entry_dmi["adx"].to_numpy(float)
            if cfg.entry_adx_enabled and entry_dmi is not None
            else np.full(len(bars), np.nan)
        )
        entry_mfi = None
        if cfg.entry_mfi_risk_scaling_enabled and "volume" in bars:
            mfi_inputs = tuple(
                np.ascontiguousarray(bars[column].to_numpy(dtype=np.float64))
                for column in ("high", "low", "close", "volume")
            )
            entry_mfi = talib.MFI(*mfi_inputs, timeperiod=cfg.entry_mfi_len)
        entry_er = (
            efficiency_ratio(bars.close, cfg.entry_er_len).to_numpy(float)
            if cfg.entry_er_risk_scaling_enabled else None
        )
        if cfg.entry_adx_enabled:
            allowed = np.nan_to_num(entry_adx, nan=0.0) >= cfg.entry_adx_min
            up, down = up & allowed, down & allowed
        bias = self.trend_bias(bars) if (cfg.trend_filter != "off" or cfg.streak_trend_only) else None
        if bias is not None and cfg.trend_filter != "off":
            up, down = up & (bias > 0), down & (bias < 0)
        chop = self.chop_ok(bars) if cfg.chop_filter else None
        if chop is not None:
            up, down = up & chop, down & chop
        up &= valid
        down &= valid
        (long_candle_confirmation, short_candle_confirmation,
         neutral_candle_pattern) = self._candle_confirmations(bars, raw)
        output = {
            "ema_fast": fast, "ema_slow": slow, "adx_entry": entry_adx,
            "cross_up": up, "cross_down": down, "valid": valid,
            "long_candle_confirmation": long_candle_confirmation,
            "short_candle_confirmation": short_candle_confirmation,
            "neutral_candle_pattern": neutral_candle_pattern,
            "trend_bias": bias if bias is not None else np.zeros(len(bars), int),
            "chop_ok": chop if chop is not None else np.ones(len(bars), bool),
            "target": np.select([up, down], [1, -1], default=0),
        }
        if cfg.entry_dmi_risk_scaling_enabled and entry_dmi is not None:
            output["entry_plus_di"] = entry_dmi["plus_di"].to_numpy(float)
            output["entry_minus_di"] = entry_dmi["minus_di"].to_numpy(float)
        if entry_mfi is not None:
            output["entry_mfi"] = np.asarray(entry_mfi, dtype=float)
        if entry_er is not None:
            output["entry_er"] = entry_er
        return pd.DataFrame(output, index=bars.index)

    def prepare_exits(self, raw: pd.DataFrame, bars: pd.DataFrame) -> tuple[
            Optional[np.ndarray], Optional[np.ndarray], Optional[np.ndarray]]:
        """Señales existentes: (ADX reversal, EMA weakening, divergencia RSI)."""
        return (
            build_adx_exit(raw, bars, self.cfg),
            build_weakening_exit(raw, bars, self.cfg),
            build_rsi_divergence_exit(raw, bars, self.cfg),
        )

    def prepare_candle_exits(self, bars: pd.DataFrame) -> tuple[
        Optional[np.ndarray], Optional[np.ndarray], Optional[np.ndarray],
        Optional[np.ndarray], Optional[np.ndarray],
    ]:
        """Devuelve señales TA-Lib por categoría, sin mezclar signo ni dojis.

        Orden: reversión alcista/bajista (15/14), bidireccional alcista/bajista
        (27) y doji neutral (5). Todas se calculan sobre velas de estrategia
        cerradas para evitar el ruido de patrones intratimeframe.
        """
        if not self.cfg.candle_pattern_exit_enabled:
            return None, None, None, None, None
        return self._candle_exit_patterns(bars)

    def prepare_confirm_exit(self, raw: pd.DataFrame, bars: pd.DataFrame) -> Optional[np.ndarray]:
        cfg = self.cfg
        enabled = getattr(cfg, "entry_confirm_move_exit_enabled", None)
        if enabled is None:
            enabled = getattr(cfg, "confirm_entry_adverse_exit_enabled", True)
        if not enabled or bars.empty:
            return np.zeros(len(bars), bool)
        return np.zeros(len(bars), bool)


# ---------------------------------------------------------------------------
# Motor de ejecución
# ---------------------------------------------------------------------------

class Backtester:
    """Motor de ejecución. Las señales se evalúan al cierre y se ejecutan en la apertura siguiente."""

    def __init__(self, bars: pd.DataFrame, config: Config, signals: Optional[pd.DataFrame] = None,
                 adx_exit: Optional[np.ndarray] = None, weak_exit: Optional[np.ndarray] = None,
                 rsi_div_exit: Optional[np.ndarray] = None, verbose: bool = False,
                 candle_bull_exit: Optional[np.ndarray] = None,
                 candle_bear_exit: Optional[np.ndarray] = None,
                 signed_bull_exit: Optional[np.ndarray] = None,
                 signed_bear_exit: Optional[np.ndarray] = None,
                 doji_exit: Optional[np.ndarray] = None):
        self.bars, self.cfg, self.verbose = bars, config, verbose
        self.signals = signals if signals is not None else EMACrossStrategy(config).generate_signals(bars)
        self.entry_plus_di = (
            self.signals["entry_plus_di"].to_numpy(float)
            if "entry_plus_di" in self.signals else None
        )
        self.entry_minus_di = (
            self.signals["entry_minus_di"].to_numpy(float)
            if "entry_minus_di" in self.signals else None
        )
        self.entry_mfi = (
            self.signals["entry_mfi"].to_numpy(float)
            if "entry_mfi" in self.signals else None
        )
        self.entry_er = (
            self.signals["entry_er"].to_numpy(float)
            if "entry_er" in self.signals else None
        )
        pattern_arrays = (
            candle_bull_exit, candle_bear_exit, signed_bull_exit, signed_bear_exit, doji_exit
        )
        if config.candle_pattern_exit_enabled and any(array is None for array in pattern_arrays):
            detected = EMACrossStrategy(config)._candle_exit_patterns(bars)
            pattern_arrays = tuple(
                array if array is not None else detected[index]
                for index, array in enumerate(pattern_arrays)
            )
        (candle_bull_exit, candle_bear_exit, signed_bull_exit,
         signed_bear_exit, doji_exit) = pattern_arrays
        self.adx_exit = np.asarray(adx_exit, bool) if adx_exit is not None else np.zeros(len(bars), bool)
        self.weak_exit = np.asarray(weak_exit, bool) if weak_exit is not None else np.zeros(len(bars), bool)
        # Divergencia RSI: +1 alcista (cierra SHORT), -1 bajista (cierra LONG), 0 sin señal.
        self.rsi_div = np.asarray(rsi_div_exit, int) if rsi_div_exit is not None else np.zeros(len(bars), int)
        self.candle_bull_exit = np.asarray(candle_bull_exit, bool) if candle_bull_exit is not None else np.zeros(len(bars), bool)
        self.candle_bear_exit = np.asarray(candle_bear_exit, bool) if candle_bear_exit is not None else np.zeros(len(bars), bool)
        self.signed_bull_exit = np.asarray(signed_bull_exit, bool) if signed_bull_exit is not None else np.zeros(len(bars), bool)
        self.signed_bear_exit = np.asarray(signed_bear_exit, bool) if signed_bear_exit is not None else np.zeros(len(bars), bool)
        self.doji_exit = np.asarray(doji_exit, bool) if doji_exit is not None else np.zeros(len(bars), bool)
        self.ema55_touch_exit_active = (
            config.ema55_touch_exit_enabled
            and pd.Timedelta(config.timeframe) == pd.Timedelta("30min")
        )
        if self.ema55_touch_exit_active:
            self.ema55_line, self.ema55_touches = ema_band_touch_flags(
                bars, 55, config.ema55_touch_tolerance_pct
            )
        else:
            self.ema55_line = np.full(len(bars), np.nan, dtype=float)
            self.ema55_touches = np.zeros(len(bars), dtype=bool)
        if len(self.adx_exit) != len(bars): self.adx_exit = np.zeros(len(bars), bool)
        if len(self.weak_exit) != len(bars): self.weak_exit = np.zeros(len(bars), bool)
        if len(self.rsi_div) != len(bars): self.rsi_div = np.zeros(len(bars), int)
        if len(self.candle_bull_exit) != len(bars): self.candle_bull_exit = np.zeros(len(bars), bool)
        if len(self.candle_bear_exit) != len(bars): self.candle_bear_exit = np.zeros(len(bars), bool)
        if len(self.signed_bull_exit) != len(bars): self.signed_bull_exit = np.zeros(len(bars), bool)
        if len(self.signed_bear_exit) != len(bars): self.signed_bear_exit = np.zeros(len(bars), bool)
        if len(self.doji_exit) != len(bars): self.doji_exit = np.zeros(len(bars), bool)
        self.o, self.h, self.l, self.c = (bars[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        self.times = bars.index
        self.cash = config.initial_capital
        self.position: Optional[Position] = None
        self.pending_target: Optional[int] = None
        self.pending_exit: Optional[tuple[str, float]] = None
        self.pending_add: Optional[tuple[int, float]] = None  # (bar_index_a_ejecutar, % de lo que falta)
        self.trades: list[Trade] = []
        # Aperturas y añadidos de entrada parcial (scale-in) para reportar/públicar.
        self.opened: list[dict] = []
        self.adds: list[dict] = []
        self.add_skipped: list[dict] = []
        self.trade_id = self.position_id = 0
        self.eq_time, self.eq_cash, self.eq_value, self.eq_side, self.eq_qty = [], [], [], [], []
        # Protección de riesgo: estado del límite mensual, de la pausa por
        # racha de pérdidas y del pnl acumulado de la posición abierta.
        self.halt_until: Optional[pd.Timestamp] = None
        self.pause_until = -1
        self._bar_index = -1
        self.consec_losses = 0
        self.skip_signals_left = 0
        self.streak_in_loss = False
        self._position_pnl = 0.0
        self._recent_wins: list[bool] = []
        # Estado mensual compartido por `run`, el gate de entradas y el
        # escalado de riesgo: equity de referencia del mes y del cierre
        # anterior (sin lookahead: se usa en la apertura de la vela siguiente).
        self.month_start_equity = config.initial_capital
        self._prev_close_equity = config.initial_capital
        self.month_dd_pct = 0.0

    def _price(self, price: float, side: int) -> float:
        slip = self.cfg.slippage_bps / 10_000
        return price * (1 + slip if side == 1 else 1 - slip)

    def _fee(self, notional: float) -> float:
        return abs(notional) * self.cfg.commission_bps / 10_000

    def _stop_price(self, side: int, entry: float, pct: float) -> float:
        return entry * (1 - pct if side == 1 else 1 + pct)

    def _risk_per_unit(self, side: int, entry: float, pct: float) -> float:
        stop_exec = self._price(self._stop_price(side, entry, pct), -side)
        return abs(stop_exec - entry) + (entry + abs(stop_exec)) * self.cfg.commission_bps / 10_000

    def open_position(self, side: int, raw_price: float, ts: pd.Timestamp, partial_pct: float = 100.0):
        if self.position is not None or self.cash <= 0:
            return
        price = self._price(raw_price, side)
        if not np.isfinite(price) or price <= 0:
            return
        pct_sl = self.cfg.stop_loss_pct
        risk_unit = self._risk_per_unit(side, price, pct_sl) if pct_sl > 0 else 0.0
        by_risk = (
            self.cash * self.cfg.risk_pct * self._risk_multiplier()
            * self._monthly_risk_scale() * self._entry_dmi_risk_multiplier(side)
            * self._entry_mfi_risk_multiplier(side) * self._entry_er_risk_multiplier()
            / risk_unit
            if risk_unit > 0 else np.inf
        )
        by_lev = self.cash * self.cfg.max_leverage / price
        qty_total = min(by_risk, by_lev)
        if not np.isfinite(qty_total) or qty_total <= 0:
            return
        if partial_pct >= 99.999:
            qty_init = qty_total
            target_q = 0.0
            pe_done = True
            ep_pct = 100.0
        else:
            if not (0 < partial_pct < 100):
                qty_init = qty_total
                target_q = 0.0
                pe_done = True
                ep_pct = 100.0
            else:
                qty_init = qty_total * partial_pct / 100.0
                target_q = qty_total
                pe_done = False
                ep_pct = partial_pct
        fee = self._fee(price * qty_init)
        self.cash -= fee
        self.position_id += 1
        self._position_pnl = 0.0
        self.position = Position(
            position_id=self.position_id,
            side=side,
            quantity=qty_init,
            initial_quantity=qty_total,  # total objetivo (coherente con prorrateo de fees)
            entry_price=price,
            entry_time=ts,
            entry_fee=fee,
            stop_pct=pct_sl,
            risk_amount=qty_total * risk_unit if risk_unit > 0 else 0.0,
            breakeven=False,
            partial_done=False,
            partial2_done=False,
            touch_done=False,
            target_quantity=target_q,
            partial_entry_done=pe_done,
            entry_partial_pct=ep_pct,
            entry_bar_index=self._bar_index,
            last_add_price=price if qty_init >= qty_total else 0.0,
            add_count=0,
            confirm_exit_done=False,
        )
        self.opened.append({
            "position_id": self.position_id, "time": ts, "price": price, "side": side,
            "quantity": qty_init, "entry_pct": ep_pct, "target_quantity": target_q or qty_total,
        })

    def close_position(self, percent: float, raw_price: float, ts: pd.Timestamp, reason: str):
        pos = self.position
        if pos is None:
            return
        if not 0 < percent <= 100:
            raise ValueError("El porcentaje de cierre debe estar en (0, 100].")
        qty = pos.quantity * percent / 100
        side = pos.side
        price = self._price(raw_price, -side)
        gross = (price - pos.entry_price) * qty * side
        exit_fee = self._fee(price * qty)
        # La comisión de entrada se distribuye proporcionalmente a la cantidad original restante.
        entry_fee = pos.entry_fee * (qty / pos.quantity)
        fees = entry_fee + exit_fee
        net = gross - fees
        self.cash += gross - exit_fee
        self._position_pnl += net
        self.trade_id += 1
        self.trades.append(Trade(self.trade_id, pos.position_id, "LONG" if side == 1 else "SHORT",
            pos.entry_time, ts, pos.entry_price, price, qty, gross, fees, net,
            net / (pos.entry_price * qty) * 100 if pos.entry_price * qty else 0.0,
            reason, pos.risk_amount))
        remaining = pos.quantity - qty
        if percent >= 100 or remaining <= max(1e-12, pos.initial_quantity * 1e-12):
            self.position = None
            self._close_streak()
        else:
            pos.quantity = remaining
            pos.entry_fee -= entry_fee

    def _risk_multiplier(self) -> float:
        """Multiplicador de `risk_pct` según el resultado de las últimas posiciones.

        Con `adaptive_risk_window` a 0 devuelve 1 (tamaño fijo). Si no,
        ``1 + k * (win_rate de las últimas n posiciones - 0.5)`` recortado a
        ``[adaptive_risk_min, adaptive_risk_max]``: penaliza las rachas frías
        (donde salen los meses rojos) y reutiliza el tamaño en las cálidas.
        """
        cfg = self.cfg
        if cfg.adaptive_risk_window <= 0 or not self._recent_wins:
            return 1.0
        wins = self._recent_wins[-cfg.adaptive_risk_window:]
        win_rate = sum(wins) / len(wins)
        factor = 1 + cfg.adaptive_risk_k * (win_rate - 0.5)
        return float(min(max(factor, cfg.adaptive_risk_min), cfg.adaptive_risk_max))

    def _entry_dmi_risk_multiplier(self, side: int) -> float:
        """Escala tamaño por DMI de la vela confirmada, sin bloquear entradas."""
        cfg = self.cfg
        if (not cfg.entry_dmi_risk_scaling_enabled or self.entry_plus_di is None
                or self.entry_minus_di is None or side not in (1, -1)):
            return 1.0
        signal_index = self._bar_index - 1
        if signal_index < 0 or signal_index >= len(self.entry_plus_di):
            return 1.0
        plus_di = self.entry_plus_di[signal_index]
        minus_di = self.entry_minus_di[signal_index]
        if not (np.isfinite(plus_di) and np.isfinite(minus_di)):
            return 1.0
        spread_in_trade_direction = (plus_di - minus_di) * side
        factor = cfg.entry_dmi_risk_base + (
            spread_in_trade_direction / cfg.entry_dmi_risk_spread_scale
        )
        return float(np.clip(factor, cfg.entry_dmi_risk_min, cfg.entry_dmi_risk_max))

    def _entry_mfi_risk_multiplier(self, side: int) -> float:
        """Escala tamaño por flujo MFI de la última vela de señal ya cerrada."""
        cfg = self.cfg
        if (not cfg.entry_mfi_risk_scaling_enabled or self.entry_mfi is None
                or side not in (1, -1)):
            return 1.0
        signal_index = self._bar_index - 1
        if signal_index < 0 or signal_index >= len(self.entry_mfi):
            return 1.0
        value = self.entry_mfi[signal_index]
        if not np.isfinite(value):
            return 1.0
        directional_flow = (value - 50.0) * side
        factor = cfg.entry_mfi_risk_base + (
            directional_flow / cfg.entry_mfi_risk_spread_scale
        )
        return float(np.clip(factor, cfg.entry_mfi_risk_min, cfg.entry_mfi_risk_max))

    def _entry_er_risk_multiplier(self) -> float:
        """Escala riesgo por eficiencia direccional de los cierres previos."""
        cfg = self.cfg
        if not cfg.entry_er_risk_scaling_enabled or self.entry_er is None:
            return 1.0
        signal_index = self._bar_index - 1
        if signal_index < 0 or signal_index >= len(self.entry_er):
            return 1.0
        value = self.entry_er[signal_index]
        if not np.isfinite(value):
            return 1.0
        factor = cfg.entry_er_risk_base + cfg.entry_er_risk_slope * value
        return float(np.clip(factor, cfg.entry_er_risk_min, cfg.entry_er_risk_max))

    def _monthly_risk_scale(self) -> float:
        """Escala de tamaño según el drawdown acumulado del mes calendario.

        Con ``monthly_risk_scaling`` desactivado devuelve 1. Si no, devuelve
        ``1 - clamp(dd/budget, 0, 1) * (1 - monthly_risk_scaling_min)``: al
        abrir el mes (dd = 0) se arriesga el tamaño habitual y conforme la
        equity se acerca al límite ``monthly_loss_limit_pct`` el tamaño baja
        hasta ``monthly_risk_scaling_min``. Corta las pérdidas que profundizan
        un mes ya en rojo sin cerrar posiciones ni bloquear entradas.

        Usa la equity del **cierre de la vela anterior** (``_prev_close_equity``),
        la última conocida al ejecutar la apertura de la vela actual, así que
        no hay lookahead.
        """
        cfg = self.cfg
        if not cfg.monthly_risk_scaling or self.month_start_equity <= 0:
            return 1.0
        budget = cfg.monthly_loss_limit_pct / 100 if cfg.monthly_loss_limit_pct > 0 else 0.02
        dd = max(0.0, 1.0 - self._prev_close_equity / self.month_start_equity)
        used = min(dd / budget, 1.0)
        floor = min(max(cfg.monthly_risk_scaling_min, 0.0), 1.0)
        return float(1.0 - used * (1.0 - floor))

    def _close_streak(self) -> None:
        """Actualiza la racha de pérdidas y arma la pausa de entrada si procede.

        Una posición se considera en pérdida si el pnl neto acumulado de todos
        sus tramos es negativo. Al alcanzar ``consecutive_loss_pause`` posiciones
        seguidas en pérdida se bloquean las entradas durante
        ``consecutive_loss_pause_bars`` velas.
        """
        cfg = self.cfg
        won = self._position_pnl >= 0
        if cfg.adaptive_risk_window > 0:
            self._recent_wins.append(won)
            del self._recent_wins[:-cfg.adaptive_risk_window]
        if not won:
            self.consec_losses += 1
            self.skip_signals_left = max(self.skip_signals_left, cfg.consecutive_loss_skip_signals)
            self.streak_in_loss = True
        else:
            self.consec_losses = 0
            self.skip_signals_left = 0
            self.streak_in_loss = False
        self._position_pnl = 0.0
        if cfg.consecutive_loss_pause > 0 and self.consec_losses >= cfg.consecutive_loss_pause:
            self.pause_until = self._bar_index + cfg.consecutive_loss_pause_bars
            self.consec_losses = 0
            if self.verbose:
                print(f"[PAUSA] {cfg.consecutive_loss_pause} pérdidas seguidas: "
                      f"sin entradas {cfg.consecutive_loss_pause_bars} velas", flush=True)

    def entries_blocked(self) -> bool:
        """``True`` si está prohibido abrir posiciones nuevas en esta vela."""
        if self.pause_until is not None and self._bar_index <= self.pause_until:
            return True
        return self.halt_until is not None and self.times[self._bar_index] < self.halt_until

    def _risk_unit(self, pos: Position) -> float:
        return pos.risk_amount / pos.initial_quantity if pos.initial_quantity else 0.0

    def _ema_slow_touch_level(self, i: int) -> Optional[float]:
        """Nivel de cierre por toque a la EMA slow en la vela ``i``.

        Devuelve el precio de disparo con la tolerancia ya aplicada, o ``None``
        si el cierre por toque no está activo. Con ``close_on_ema_slow_touch_after``
        en False (default) la tolerancia se aplica ANTES del toque: para LONG el
        disparo es ``EMA_slow * (1 + tol)`` y para SHORT ``EMA_slow * (1 - tol)``.
        Con el flag en True se aplica DESPUÉS del toque: LONG ``EMA_slow * (1 - tol)``
        y SHORT ``EMA_slow * (1 + tol)``. Con tolerancia 0 es la propia EMA slow.
        """
        cfg, pos = self.cfg, self.position
        if pos is None or not cfg.close_on_ema_slow_touch or self.signals is None:
            return None
        if "ema_slow" not in self.signals.columns or i >= len(self.signals):
            return None
        ema_slow = float(self.signals["ema_slow"].iloc[i])
        if not np.isfinite(ema_slow) or ema_slow <= 0:
            return None
        tol = max(cfg.close_on_ema_slow_touch_pct, 0.0) / 100.0
        offset = -tol if cfg.close_on_ema_slow_touch_after else tol
        return ema_slow * (1 + offset) if pos.side == 1 else ema_slow * (1 - offset)

    def _ema_slow_touch(self, i: int) -> bool:
        """Salida (total o parcial) si el precio toca la EMA slow tras el cross.

        El parámetro `close_on_ema_slow_touch_pct` define la tolerancia en % y
        `close_on_ema_slow_touch_after` su sentido. Por defecto (False) la
        tolerancia se aplica ANTES del toque: para LONG se dispara cuando el
        mínimo llega a EMA_slow * (1 + tol), y para SHORT cuando el máximo llega
        a EMA_slow * (1 - tol). Con el flag en True se aplica DESPUÉS del toque:
        LONG cuando el mínimo llega a EMA_slow * (1 - tol) y SHORT cuando el
        máximo llega a EMA_slow * (1 + tol). Con tolerancia 0 el disparo ocurre
        justo al tocar la EMA slow.

        Se cierra `close_on_ema_slow_touch_exit_pct` de la posición: con 100 es
        un cierre total; con un valor menor es una **salida parcial** que sólo
        se aplica una vez por posición (`pos.touch_done`); el resto sigue
        gestionándose con las reglas habituales (stop, TP, cross, ADX, …).
        """
        cfg, pos = self.cfg, self.position
        trigger = self._ema_slow_touch_level(i)
        if pos is None or trigger is None or pos.touch_done:
            return False
        if pos.side == 1:
            if self.l[i] > trigger:
                return False
            fill = min(self.o[i], trigger)
        else:
            if self.h[i] < trigger:
                return False
            fill = max(self.o[i], trigger)
        prefix = "EMA_SLOW_TOUCH_AFTER" if cfg.close_on_ema_slow_touch_after else "EMA_SLOW_TOUCH"
        self.close_position(cfg.close_on_ema_slow_touch_exit_pct, fill, self.times[i],
                            f"{prefix}_{cfg.close_on_ema_slow_touch_pct:g}%")
        pos = self.position
        if pos is not None:
            pos.touch_done = True
        return True

    def _confirm_move_exit_level(self) -> Optional[float]:
        """Nivel de salida parcial si el precio va en contra de la confirmación.

        Devuelve el precio de disparo calculado desde el precio de la entrada
        de confirmación (``pos.last_add_price``), o ``None`` si la regla no
        está activa, la posición todavía no se confirmó, ya se aplicó una vez
        o la entrada fue completa (sin add de confirmación).
        """
        cfg, pos = self.cfg, self.position
        if pos is None:
            return None
        enabled = getattr(cfg, "entry_confirm_move_exit_enabled", None)
        if enabled is None:
            enabled = getattr(cfg, "confirm_entry_adverse_exit_enabled", True)
        if not enabled or getattr(pos, "confirm_exit_done", False):
            return None
        if pos.add_count <= 0 or pos.last_add_price <= 0:
            return None
        move_pct = getattr(cfg, "entry_confirm_move_exit_pct", None)
        if move_pct is None:
            move_pct = getattr(cfg, "confirm_entry_adverse_move_pct", 0.5)
        pct = max(float(move_pct), 0.0) / 100.0
        return pos.last_add_price * (1 - pct) if pos.side == 1 else pos.last_add_price * (1 + pct)


    def _confirm_move_exit(self, i: int) -> bool:
        """Salida parcial cuando el precio se mueve en contra de la confirmación.

        Tras completarse la entrada por confirmación, si el precio recorre
        ``entry_confirm_move_exit_pct`` % en contra de ``pos.last_add_price``,
        se cierra ``entry_confirm_move_exit_portion_pct`` % de la posición
        restante. Sólo se aplica una vez por posición (``pos.confirm_exit_done``)
        y se resuelve intrabar, como el stop.
        """
        cfg, pos = self.cfg, self.position
        trigger = self._confirm_move_exit_level()
        if pos is None or trigger is None:
            return False
        if pos.side == 1:
            if self.l[i] > trigger:
                return False
            fill = min(self.o[i], trigger)
        else:
            if self.h[i] < trigger:
                return False
            fill = max(self.o[i], trigger)
        exit_pct = getattr(cfg, "entry_confirm_move_exit_portion_pct", None)
        if exit_pct is None:
            exit_pct = getattr(cfg, "confirm_entry_adverse_exit_pct", 50.0)
        move_pct = getattr(cfg, "entry_confirm_move_exit_pct", None)
        if move_pct is None:
            move_pct = getattr(cfg, "confirm_entry_adverse_move_pct", 0.5)
        self.close_position(float(exit_pct), fill, self.times[i],
                            f"CONFIRM_MOVE_AGAINST_{float(move_pct):g}%")
        pos = self.position
        if pos is not None:
            pos.confirm_exit_done = True
        return True


    def _rsi_divergence_reason(self, i: int) -> Optional[str]:
        """Motivo de salida total si hay divergencia RSI en contra de la posición.

        La divergencia **bajista** de la vela ``i`` (``self.rsi_div[i] == -1``:
        precio con máximos más altos, RSI con máximos más bajos) cierra un LONG
        y la **alcista** (``== 1``: precio con mínimos más bajos, RSI con
        mínimos más altos) cierra un SHORT. Devuelve el motivo (``RSI_DIVERGENCE_*``)
        o ``None`` si no hay divergencia contra la posición abierta.
        """
        pos = self.position
        if pos is None or i >= len(self.rsi_div):
            return None
        flag = int(self.rsi_div[i])
        if flag < 0 and pos.side == 1:
            return RSI_DIVERGENCE_BEAR_REASON
        if flag > 0 and pos.side == -1:
            return RSI_DIVERGENCE_BULL_REASON
        return None

    def _candle_pattern_exit_reason(self, i: int) -> Optional[str]:
        """Salida TA-Lib confirmada por doji, ruptura de extremo y pérdida abierta.

        Una señal de patrón por sí sola no cierra la posición: durante las
        siguientes `candle_pattern_exit_confirm_bars` velas el cierre debe
        atravesar el mínimo/máximo del patrón y la posición debe seguir en
        pérdida. Los patrones bidireccionales tienen un umbral más estricto.
        """
        pos = self.position
        if pos is None or pos.candle_exit_done or i <= 0:
            return None
        pnl_pct = (self.c[i] / pos.entry_price - 1.0) * pos.side * 100.0
        reversal_loss_pct = self.cfg.candle_pattern_loss_threshold(pos.side)
        start = max(0, i - self.cfg.candle_pattern_exit_confirm_bars)
        prior = np.arange(start, i)
        if pos.side == 1:
            reversal = prior[self.candle_bear_exit[prior] & self.doji_exit[prior]]
            signed = prior[self.signed_bear_exit[prior] & self.doji_exit[prior]]
            if len(reversal) and pnl_pct <= -reversal_loss_pct \
                    and np.any(self.c[i] < self.l[reversal]):
                return "CANDLE_PATTERN_BEAR"
            if len(signed) and pnl_pct <= -self.cfg.candle_pattern_exit_signed_loss_pct \
                    and np.any(self.c[i] < self.l[signed]):
                return "CANDLE_SIGNED_BEAR"
        else:
            reversal = prior[self.candle_bull_exit[prior] & self.doji_exit[prior]]
            signed = prior[self.signed_bull_exit[prior] & self.doji_exit[prior]]
            if len(reversal) and pnl_pct <= -reversal_loss_pct \
                    and np.any(self.c[i] > self.h[reversal]):
                return "CANDLE_PATTERN_BULL"
            if len(signed) and pnl_pct <= -self.cfg.candle_pattern_exit_signed_loss_pct \
                    and np.any(self.c[i] > self.h[signed]):
                return "CANDLE_SIGNED_BULL"
        return None

    def _ema55_touch_weakness_exit_reason(self, i: int) -> Optional[str]:
        """Cierra LONG tras toques repetidos a EMA55 sin ruptura confirmada.

        El toque se cuenta cuando el rango de la vela roza/cruza EMA55 ± banda.
        Se exige una secuencia de toques o varios dentro de la ventana y una
        pérdida abierta mínima. Para LONG, el cierre no debe confirmar ruptura
        arriba; para SHORT, no debe confirmar ruptura abajo. Sólo corre en 30m.
        """
        pos = self.position
        cfg = self.cfg
        if (pos is None or pos.side not in (1, -1) or not self.ema55_touch_exit_active
                or i >= len(self.ema55_touches)):
            return None
        line = self.ema55_line[i]
        if not np.isfinite(line):
            return None
        open_pnl_pct = (self.c[i] / pos.entry_price - 1.0) * pos.side * 100.0
        loss_threshold = cfg.ema55_touch_exit_loss_pct if pos.side == 1 \
            else cfg.ema55_touch_short_exit_loss_pct
        if open_pnl_pct > -loss_threshold:
            return None

        start = max(0, i - cfg.ema55_touch_lookback_bars + 1, pos.entry_bar_index)
        window = self.ema55_touches[start : i + 1]
        repeated = int(window.sum()) >= cfg.ema55_touch_min_touches
        consecutive = 0
        for touched in self.ema55_touches[i : start - 1 if start else None : -1]:
            if not touched:
                break
            consecutive += 1
        if not (repeated or consecutive >= cfg.ema55_touch_consecutive):
            return None

        failed_break_level = line * (1.0 + cfg.ema55_touch_breakout_buffer_pct / 100.0)
        if pos.side == 1:
            if self.c[i] > failed_break_level:
                return None
            return "EMA55_REPEATED_TOUCH_WEAKNESS"
        failed_downside_break_level = line * (
            1.0 - cfg.ema55_touch_breakout_buffer_pct / 100.0
        )
        if self.c[i] < failed_downside_break_level:
            return None
        return "EMA55_REPEATED_TOUCH_WEAKNESS_SHORT"

    def _manage(self, i: int) -> bool:
        """Take-profit parcial y salidas parciales por precio.

        Gestiona, en orden: toque a la EMA slow, movimiento adverso desde la
        entrada de confirmación y take-profits parciales. Devuelve ``True`` si
        una salida parcial cerró (parte de) la posición, para que `run` pueda
        resolver el resto del stop en la misma vela.
        """
        cfg, pos = self.cfg, self.position
        if pos is None:
            return False
        if self._ema_slow_touch(i):
            return True
        if self._confirm_move_exit(i):
            return True
        if not cfg.partial_tp_enabled:
            return False
        risk_unit = self._risk_unit(pos)
        if risk_unit <= 0:
            return False
        pos_after = pos
        # TP2 primero si no hecho y trigger alcanzado (mayor distancia)
        if cfg.partial_tp2_r > 0 and not pos.partial2_done:
            trigger2 = pos.entry_price + pos.side * cfg.partial_tp2_r * risk_unit
            hit2 = self.h[i] >= trigger2 if pos.side == 1 else self.l[i] <= trigger2
            if hit2:
                fill2 = max(trigger2, self.o[i]) if pos.side == 1 else min(trigger2, self.o[i])
                self.close_position(cfg.partial_tp2_pct, fill2, self.times[i], f"PARTIAL_TP2_{cfg.partial_tp2_r:g}R")
                pos_after = self.position
                if pos_after is not None:
                    pos_after.partial2_done = True
                return False
        # TP1
        if not pos.partial_done:
            trigger = pos.entry_price + pos.side * cfg.partial_tp_r * risk_unit
            hit = self.h[i] >= trigger if pos.side == 1 else self.l[i] <= trigger
            if hit:
                fill = max(trigger, self.o[i]) if pos.side == 1 else min(trigger, self.o[i])
                self.close_position(cfg.partial_tp_pct, fill, self.times[i], f"PARTIAL_TP_{cfg.partial_tp_r:g}R")
                pos_after = self.position
                if pos_after is not None:
                    pos_after.partial_done = True
                    if cfg.move_stop_to_breakeven:
                        pos_after.breakeven = True
        return False

    def _stop(self, i: int, touch_level: Optional[float] = None,
              confirm_level: Optional[float] = None) -> bool:
        pos = self.position
        if pos is None or pos.stop_pct <= 0: return False
        stop = pos.entry_price if pos.breakeven else self._stop_price(pos.side, pos.entry_price, pos.stop_pct)
        hit = self.l[i] <= stop if pos.side == 1 else self.h[i] >= stop
        if not hit: return False
        # Si en la misma vela también se toca la EMA slow o el nivel de salida
        # por confirmación y éste se alcanza antes que el stop (está más cerca
        # de la entrada), gana la salida parcial: el stop se pospone y
        # `_manage` cerrará por EMA_SLOW_TOUCH o CONFIRM_MOVE_AGAINST. En salida
        # parcial el resto se cierra al stop en la misma vela (ver `run`).
        for level, done in ((touch_level, pos.touch_done), (confirm_level, pos.confirm_exit_done)):
            if level is None or done:
                continue
            level_hit = self.l[i] <= level if pos.side == 1 else self.h[i] >= level
            level_first = level >= stop if pos.side == 1 else level <= stop
            if level_hit and level_first:
                return False
        fill = min(self.o[i], stop) if pos.side == 1 else max(self.o[i], stop)
        reason = "BREAKEVEN" if pos.breakeven else f"STOP_LOSS_{pos.stop_pct*100:.2f}%"
        self.close_position(100, fill, self.times[i], reason)
        if self.verbose: print(f"[STOP] {self.times[i]} fill={fill:.8f}", flush=True)
        return True

    def add_position(self, percent_of_remaining: float, raw_price: float, ts: pd.Timestamp):
        """Añade a la posición abierta. `percent_of_remaining` % de lo que falta
        para alcanzar `target_quantity` (respecto al restante)."""
        pos = self.position
        if pos is None:
            return
        if not 0 < percent_of_remaining <= 100:
            raise ValueError("percent_of_remaining debe estar en (0, 100]")
        if pos.target_quantity <= 0:
            pos.partial_entry_done = True
            return
        remaining_target = pos.target_quantity - pos.quantity
        if remaining_target <= 1e-12:
            pos.quantity = max(pos.quantity, pos.target_quantity)
            pos.target_quantity = pos.quantity
            pos.partial_entry_done = True
            return
        qty_add = remaining_target * percent_of_remaining / 100.0
        if qty_add <= 1e-12:
            return
        side = pos.side
        price = self._price(raw_price, side)
        if not np.isfinite(price) or price <= 0:
            return
        fee_add = self._fee(price * qty_add)
        self.cash -= fee_add
        total_qty_new = pos.quantity + qty_add
        if total_qty_new > 0:
            pos.entry_price = (pos.entry_price * pos.quantity + price * qty_add) / total_qty_new
        pos.quantity = total_qty_new
        pos.entry_fee += fee_add
        pos.add_count += 1
        pos.last_add_price = price
        self.adds.append({
            "position_id": pos.position_id, "time": ts, "price": price,
            "quantity": qty_add, "target_quantity": pos.target_quantity, "side": pos.side,
        })
        if pos.quantity >= pos.target_quantity - 1e-12:
            pos.quantity = pos.target_quantity
            pos.partial_entry_done = True

    def _entry_confirmed_by_bars(self, i: int) -> bool:
        """Confirma el scale-in tras N cierres y, opcionalmente, a favor de la entrada.

        La vela de la primera entrada tiene antigüedad cero. Por tanto, con la
        configuración recomendada (``entry_confirm_bars=2``) la confirmación se
        evalúa en el segundo cierre posterior y `run` ejecuta el resto en la
        apertura de la vela siguiente. La dirección se mide desde el precio de
        entrada inicial, no desde el cierre anterior ni el color de la vela.
        """
        cfg, pos = self.cfg, self.position
        if pos is None or i - pos.entry_bar_index < cfg.entry_confirm_bars:
            return False
        if not cfg.entry_confirm_close_in_dir:
            return True
        return (self.c[i] - pos.entry_price) * pos.side >= -1e-12

    def _should_add_partial(self, i: int) -> bool:
        """Indica si al cierre de ``i`` debe programarse el resto de la entrada."""
        pos = self.position
        if pos is None or pos.target_quantity <= 0:
            return False
        if pos.partial_entry_done:
            return False
        if pos.quantity >= pos.target_quantity - 1e-12:
            pos.partial_entry_done = True
            return False
        cfg = self.cfg
        if cfg.entry_confirm_max_move_pct > 0 and pos.entry_price > 0:
            move_pct_against = (self.c[i] / pos.entry_price - 1.0) * 100.0 * pos.side
            if move_pct_against <= -cfg.entry_confirm_max_move_pct:
                if self.verbose:
                    print(f"[SKIP_ADD] {self.times[i]} precio se ha movido {abs(move_pct_against):.2f}% en contra de la entrada, no se añade el resto", flush=True)
                try:
                    self.add_skipped.append({
                        "position_id": pos.position_id,
                        "time": self.times[i],
                        "price": float(self.c[i]),
                        "entry_price": float(pos.entry_price),
                        "side": pos.side,
                        "move_pct_against": float(abs(move_pct_against)),
                        "threshold": float(cfg.entry_confirm_max_move_pct),
                        "entry_pct": float(pos.entry_partial_pct or 50.0),
                    })
                except Exception:
                    pass
                return False
        # Un patrón direccional puede anticipar el scale-in, pero no debe
        # añadir riesgo si el cierre ya va contra la entrada parcial cuando la
        # configuración exige confirmación de precio en dirección.
        pattern_column = "long_candle_confirmation" if pos.side == 1 else "short_candle_confirmation"
        if self.signals is not None and pattern_column in self.signals.columns \
                and bool(self.signals[pattern_column].iloc[i]):
            move_in_direction = (self.c[i] - pos.entry_price) * pos.side
            if not cfg.entry_confirm_close_in_dir or move_in_direction >= -1e-12:
                return True
        if cfg.entry_confirm_mode == "bars":
            return self._entry_confirmed_by_bars(i)
        elif cfg.entry_confirm_mode == "close_above_ema":
            if self.signals is None:
                return False
            if "ema_fast" not in self.signals.columns or "ema_slow" not in self.signals.columns:
                return False
            ef = float(self.signals["ema_fast"].iloc[i])
            es = float(self.signals["ema_slow"].iloc[i])
            if not (np.isfinite(ef) and np.isfinite(es)):
                return False
            if pos.side == 1:
                if self.c[i] > ef and self.c[i] > es:
                    return True
            else:
                if self.c[i] < ef and self.c[i] < es:
                    return True
            return False
        elif cfg.entry_confirm_mode == "pullback_retest":
            if self.signals is None:
                return False
            if "ema_slow" not in self.signals.columns:
                return False
            es = float(self.signals["ema_slow"].iloc[i])
            if not np.isfinite(es) or es <= 0:
                return False
            tol = max(cfg.entry_confirm_retest_tol_pct, 0.0) / 100.0
            if pos.side == 1:
                if self.c[i] > es and self.c[i] >= self.o[i]:
                    if self.l[i] <= es * (1 + tol * 0.25):
                        return True
                    if i - pos.entry_bar_index >= 1:
                        return self.c[i] > es
            else:
                if self.c[i] < es and self.c[i] <= self.o[i]:
                    if self.h[i] >= es * (1 - tol * 0.25):
                        return True
                    if i - pos.entry_bar_index >= 1:
                        return self.c[i] < es
            return False
        return False

    def _execute_target(self, i: int, target: int):
        if self.position is not None and self.position.side != target:
            self.close_position(100, self.o[i], self.times[i], CROSS_REASON)
        if self.position is None:
            if self.cfg.entry_partial_enabled:
                partial_pct = self.cfg.entry_partial_pct
            else:
                partial_pct = 100.0
            self.open_position(target, self.o[i], self.times[i], partial_pct=partial_pct)

    def _equity_value(self, close: float) -> float:
        """Equity mark-to-market al precio ``close`` (caja + posición a precio de liquidación)."""
        pos = self.position
        value = self.cash
        if pos is not None:
            liq = self._price(close, -pos.side)
            value += (liq - pos.entry_price) * pos.quantity * pos.side - self._fee(liq * pos.quantity)
        return value

    def _mark(self, ts: pd.Timestamp, close: float):
        pos = self.position
        value = self._equity_value(close)
        self.eq_time.append(ts); self.eq_cash.append(self.cash); self.eq_value.append(value)
        self.eq_side.append("FLAT" if pos is None else ("LONG" if pos.side == 1 else "SHORT"))
        self.eq_qty.append(0.0 if pos is None else pos.quantity)

    def run(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        n, cfg = len(self.bars), self.cfg
        if not n:
            return empty_trades(), pd.DataFrame(columns=["timestamp", "cash", "equity", "position_side", "position_quantity"])
        adx_labels = "_".join(f"{int(pd.Timedelta(tf).total_seconds() // 60)}M"
                              for tf in cfg.adx_timeframes)
        adx_base = f"ADX_{adx_labels}_REVERSAL"
        adx_reason = adx_base if cfg.adx_exit_pct >= 100 else f"{adx_base}_{cfg.adx_exit_pct:g}%"
        weak_tfs = cfg.ema_weakening_timeframes or (cfg.timeframe,)
        weak_fast, weak_slow = cfg.ema_weakening_lengths
        weak_base = "EMA_WEAKENING_" + "_".join(tf.upper() for tf in weak_tfs)
        weak_len = f"EMA{weak_fast}/{weak_slow}"
        if (weak_fast, weak_slow) != (cfg.ema_fast, cfg.ema_slow):
            weak_base = f"{weak_base}_{weak_len}"
        weak_reason = weak_base if cfg.ema_weakening_exit_pct >= 100 else f"{weak_base}_{cfg.ema_weakening_exit_pct:g}%"
        started = time.perf_counter()
        print(f"[BACKTEST] {n:,} velas {cfg.timeframe}", flush=True)
        month_key: Optional[tuple[int, int]] = None
        month_start_equity = cfg.initial_capital
        for i in range(n):
            self._bar_index = i
            # Reinicio del límite mensual en cada mes calendario; la referencia
            # es la equity mark-to-market del cierre de la vela anterior.
            key = (self.times[i].year, self.times[i].month)
            if key != month_key:
                month_key = key
                self.halt_until = None
                month_start_equity = self._equity_value(self.c[i - 1]) if i else cfg.initial_capital
                self.month_start_equity = month_start_equity
            # 1) Ejecutar add pendiente (decidido al cierre de vela anterior)
            if self.pending_add is not None:
                exec_i, pct_rem = self.pending_add
                if exec_i == i:
                    self.add_position(pct_rem, self.o[i], self.times[i])
                    self.pending_add = None

            # 2) Ejecutar target/exit pendientes (decididos al cierre anterior)
            if self.pending_target is not None:
                self._execute_target(i, self.pending_target)
                self.pending_target = None
                self.pending_exit = None
            elif self.pending_exit is not None:
                reason, pct = self.pending_exit
                self.close_position(pct, self.o[i], self.times[i], reason)
                self.pending_exit = None

            # 3) Gestión de stops y salidas parciales
            confirm_level = self._confirm_move_exit_level()
            stop_hit = self._stop(i, self._ema_slow_touch_level(i), confirm_level)
            if not stop_hit:
                # Tras una salida parcial por toque a la EMA slow, si la vela
                # también recorrió hasta el stop, se cierra el resto al stop.
                if self._manage(i) and self.position is not None:
                    stop_hit = self._stop(i)

            # 4) Añadir entrada parcial pendiente (scale-in) — decidido al cierre de i
            if not stop_hit and self.position is not None and self.cfg.entry_partial_enabled:
                if self._should_add_partial(i):
                    pos = self.position
                    # Si la vela donde ocurre la confirmación cierra en negativo respecto a la entrada parcial, cerrar todo
                    if getattr(self.cfg, "entry_confirm_close_on_negative", True):
                        if (pos.side == 1 and self.c[i] < pos.entry_price - 1e-12) or (pos.side == -1 and self.c[i] > pos.entry_price + 1e-12):
                            self.pending_exit = ("CONFIRM_CLOSE_NEG", 100.0)
                            continue
                    # Ejecutar resto completo en apertura de i+1
                    self.pending_add = (i + 1, 100.0)

            # Equity mark-to-market del cierre de la vela y pérdida acumulada
            # del mes: base del guard duro, del gate de entradas y de la
            # referencia de riesgo del mes siguiente.
            equity_now = self._equity_value(self.c[i])
            if month_start_equity > 0:
                self.month_dd_pct = max(0.0, (1.0 - equity_now / month_start_equity) * 100.0)
            self._prev_close_equity = equity_now

            # Límite de pérdida mensual: si la equity cae por debajo del umbral
            # respecto al inicio del mes, se protege el mes completo (se cierra
            # la posición, si la hay, y no se abren nuevas hasta el próximo mes).
            if cfg.monthly_loss_limit_pct > 0 and self.halt_until is None:
                if equity_now <= month_start_equity * (1 - cfg.monthly_loss_limit_pct / 100):
                    if cfg.monthly_loss_limit_days > 0:
                        self.halt_until = self.times[i] + pd.Timedelta(days=cfg.monthly_loss_limit_days)
                        scope = f"{cfg.monthly_loss_limit_days} días"
                    else:
                        self.halt_until = pd.Timestamp.max.tz_localize(self.times[i].tz) if self.times[i].tz else pd.Timestamp.max
                        scope = "resto del mes"
                    if cfg.monthly_loss_limit_close and self.position is not None:
                        reason = f"{MONTHLY_LIMIT_REASON}_{cfg.monthly_loss_limit_pct:g}%"
                        self.close_position(100, self.c[i], self.times[i], reason)
                    print(f"[GUARD] {self.times[i]:%Y-%m-%d} límite "
                          f"-{cfg.monthly_loss_limit_pct:g}% alcanzado; sin entradas hasta "
                          f"{scope}", flush=True)

            if not stop_hit and bool(self.signals["valid"].iloc[i]):
                target = int(self.signals["target"].iloc[i])
                if target == 1 and not cfg.allow_long: target = 0
                if target == -1 and not cfg.allow_short: target = 0
                current = self.position.side if self.position else 0
                if target and target != current and not self.entries_blocked():
                    bias_val = int(self.signals["trend_bias"].iloc[i])
                    # Gate mensual: con el mes en pérdida ≥ umbral se frenan las
                    # entradas (todas o sólo las contra tendencia) para que un
                    # mes rojo no se profundice con nuevas trampas de whipsaw.
                    month_red = (cfg.monthly_trend_gate_pct > 0
                                 and self.month_dd_pct >= cfg.monthly_trend_gate_pct)
                    blocked_month = month_red and (cfg.monthly_gate_mode == "all"
                                                   or bias_val != target)
                    if self.skip_signals_left > 0:
                        # Tras una pérdida se ignoran las primeras señales: en
                        # whipsaw la siguiente señal suele ser otra trampa.
                        self.skip_signals_left -= 1
                        if self.verbose:
                            print(f"[SKIP] {self.times[i]} señal ignorada "
                                  f"({self.skip_signals_left} restantes)", flush=True)
                    elif blocked_month:
                        if self.verbose:
                            print(f"[GATE] {self.times[i]} mes {self.month_dd_pct:.2f}% en rojo: "
                                  f"señal {target:+d} bloqueada (modo {cfg.monthly_gate_mode})",
                                  flush=True)
                    elif cfg.streak_trend_only and self.streak_in_loss and bias_val != target:
                        # Tras una pérdida sólo se entra con la tendencia mayor.
                        if self.verbose:
                            print(f"[RACHA] {self.times[i]} señal {target:+d} "
                                  f"contra tendencia {int(self.signals['trend_bias'].iloc[i]):+d}: ignorada",
                                  flush=True)
                    else:
                        self.pending_target = target
                elif self.position is not None and self.weak_exit[i]:
                    self.pending_exit = (weak_reason, cfg.ema_weakening_exit_pct)
                elif self.position is not None and self.adx_exit[i]:
                    self.pending_exit = (adx_reason, cfg.adx_exit_pct)
                elif self.position is not None:
                    # Primero se atiende la divergencia RSI confirmada; después,
                    # patrones TA-Lib contrarios y dojis de indecisión.
                    div_reason = self._rsi_divergence_reason(i)
                    if div_reason is not None:
                        self.pending_exit = (div_reason, 100.0)
                    else:
                        candle_reason = self._candle_pattern_exit_reason(i)
                        if candle_reason is not None:
                            self.pending_exit = (candle_reason, cfg.candle_pattern_exit_pct)
                            self.position.candle_exit_done = True
                        else:
                            ema55_reason = self._ema55_touch_weakness_exit_reason(i)
                            if ema55_reason is not None:
                                self.pending_exit = (ema55_reason, 100.0)

            self._mark(self.times[i] + pd.Timedelta(cfg.timeframe), self.c[i])
            if (i + 1) % max(1, n // 10) == 0 or i == n - 1:
                print(f"[BACKTEST] {(i+1)/n:6.1%} | {i+1:,}/{n:,} velas | trades={len(self.trades)}", flush=True)

        if self.position is not None and cfg.force_close_at_end:
            ts = self.times[-1] + pd.Timedelta(cfg.timeframe)
            self.close_position(100, self.c[-1], ts, "END_OF_DATA")
            self._mark(ts, self.c[-1])
        trades = pd.DataFrame([asdict(t) for t in self.trades]) if self.trades else empty_trades()
        equity = pd.DataFrame({"timestamp": self.eq_time, "cash": self.eq_cash, "equity": self.eq_value,
                               "position_side": self.eq_side, "position_quantity": self.eq_qty})
        print(f"[BACKTEST] Finalizado en {time.perf_counter()-started:.2f}s | tramos={len(trades):,}", flush=True)
        return trades, equity


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    d = Config()
    p = argparse.ArgumentParser(description="Backtest EMA cross con salidas ADX y EMA weakening.")
    p.add_argument("parquet", help="Dataset OHLCV en Parquet")
    p.add_argument("--capital", type=float, default=d.initial_capital)
    p.add_argument("--ema-fast", type=int, default=d.ema_fast)
    p.add_argument("--ema-slow", type=int, default=d.ema_slow)
    p.add_argument("--timeframe", default=d.timeframe)
    p.add_argument("--commission-bps", type=float, default=d.commission_bps)
    p.add_argument("--slippage-bps", type=float, default=d.slippage_bps)
    p.add_argument("--risk-pct", type=float, default=d.risk_pct)
    p.add_argument("--max-leverage", type=float, default=d.max_leverage)
    p.add_argument("--stop-loss-pct", type=float, default=d.stop_loss_pct)
    p.add_argument("--confirm-bars", type=int, default=d.confirm_bars)
    p.add_argument("--entry-adx-filter", action=argparse.BooleanOptionalAction, default=d.entry_adx_enabled)
    p.add_argument("--entry-adx-len", type=int, default=d.entry_adx_len)
    p.add_argument("--entry-adx-min", type=float, default=d.entry_adx_min)
    p.add_argument("--entry-dmi-risk-scaling", action=argparse.BooleanOptionalAction,
                   default=d.entry_dmi_risk_scaling_enabled,
                   help="Modula el tamaño con DI+/DI- sin bloquear entradas.")
    p.add_argument("--entry-dmi-risk-base", type=float, default=d.entry_dmi_risk_base)
    p.add_argument("--entry-dmi-risk-spread-scale", type=float,
                   default=d.entry_dmi_risk_spread_scale,
                   help="Escala de diferencia DI direccional para el multiplicador de riesgo.")
    p.add_argument("--entry-dmi-risk-min", type=float, default=d.entry_dmi_risk_min)
    p.add_argument("--entry-dmi-risk-max", type=float, default=d.entry_dmi_risk_max)
    p.add_argument("--entry-mfi-risk-scaling", action=argparse.BooleanOptionalAction,
                   default=d.entry_mfi_risk_scaling_enabled,
                   help="Modula el tamaño con MFI (precio/volumen), sin bloquear entradas.")
    p.add_argument("--entry-mfi-len", type=int, default=d.entry_mfi_len)
    p.add_argument("--entry-mfi-risk-base", type=float, default=d.entry_mfi_risk_base)
    p.add_argument("--entry-mfi-risk-spread-scale", type=float,
                   default=d.entry_mfi_risk_spread_scale)
    p.add_argument("--entry-mfi-risk-min", type=float, default=d.entry_mfi_risk_min)
    p.add_argument("--entry-mfi-risk-max", type=float, default=d.entry_mfi_risk_max)
    p.add_argument("--entry-er-risk-scaling", action=argparse.BooleanOptionalAction,
                   default=d.entry_er_risk_scaling_enabled,
                   help="Modula el tamaño según la eficiencia direccional, sin bloquear entradas.")
    p.add_argument("--entry-er-len", type=int, default=d.entry_er_len)
    p.add_argument("--entry-er-risk-base", type=float, default=d.entry_er_risk_base)
    p.add_argument("--entry-er-risk-slope", type=float, default=d.entry_er_risk_slope)
    p.add_argument("--entry-er-risk-min", type=float, default=d.entry_er_risk_min)
    p.add_argument("--entry-er-risk-max", type=float, default=d.entry_er_risk_max)
    p.add_argument("--adx-exit", action=argparse.BooleanOptionalAction, default=d.adx_exit_enabled)
    p.add_argument("--adx-timeframe", default=d.adx_timeframe,
                   help="Timeframe(s) ADX de salida separados por coma; se combinan con OR (p. ej. 1D,30min).")
    p.add_argument("--adx-dilen", type=int, default=d.adx_dilen)
    p.add_argument("--adx-adxlen", type=int, default=d.adx_adxlen)
    p.add_argument("--adx-key-level", type=float, default=d.adx_key_level)
    p.add_argument("--adx-exit-pct", type=float, default=d.adx_exit_pct)
    p.add_argument("--adx-exit-range", type=int, default=d.adx_exit_range,
                   help="Velas durante las que una reversión ADX mantiene activada la salida (default 4).")
    p.add_argument("--ema-weakening-exit", action=argparse.BooleanOptionalAction, default=d.ema_weakening_exit_enabled)
    p.add_argument("--ema-weakening-timeframe", default=d.ema_weakening_timeframe,
                   help="Timeframe(s) EMA weakening de salida separados por coma; se combinan con OR (p. ej. 1D,30min).")
    p.add_argument("--ema-weakening-gap-pct", type=float, default=d.ema_weakening_gap_pct)
    p.add_argument("--ema-weakening-fast", type=int, default=d.ema_weakening_fast,
                   help="Longitud de la EMA rápida de la salida weakening (0 = usar --ema-fast).")
    p.add_argument("--ema-weakening-slow", type=int, default=d.ema_weakening_slow,
                   help="Longitud de la EMA lenta de la salida weakening (0 = usar --ema-slow).")
    p.add_argument("--ema-weakening-exit-pct", type=float, default=d.ema_weakening_exit_pct)
    p.add_argument("--rsi-divergence-exit", action=argparse.BooleanOptionalAction,
                   default=d.rsi_divergence_exit_enabled,
                   help="Cierra el 100%% de la posición ante una divergencia RSI en su contra "
                        "(bajista contra LONG, alcista contra SHORT).")
    p.add_argument("--rsi-divergence-timeframe", default=d.rsi_divergence_timeframe,
                   help="Timeframe(s) de divergencia RSI separados por coma; se combinan con OR (p. ej. 1D,30min).")
    p.add_argument("--rsi-divergence-len", type=int, default=d.rsi_divergence_len,
                   help="Periodo del RSI (Wilder, default 14).")
    p.add_argument("--rsi-divergence-pivot", type=int, default=d.rsi_divergence_pivot,
                   help="Velas a cada lado para confirmar un pivote de precio (default 5).")
    p.add_argument("--rsi-divergence-lookback", type=int, default=d.rsi_divergence_lookback,
                   help="Velas hacia atrás para emparejar el pivote con el anterior (default 60).")
    p.add_argument("--candle-pattern-exit", action=argparse.BooleanOptionalAction,
                   default=d.candle_pattern_exit_enabled,
                   help="Usa patrones TA-Lib contrarios y dojis para salidas tempranas.")
    p.add_argument("--candle-pattern-exit-pct", type=float, default=d.candle_pattern_exit_pct,
                   help="Porcentaje de la posición a cerrar por patrón TA-Lib (default 100).")
    p.add_argument("--candle-pattern-exit-confirm-bars", type=int,
                   default=d.candle_pattern_exit_confirm_bars,
                   help="Velas para confirmar la ruptura del extremo del patrón (default 4).")
    p.add_argument("--candle-pattern-exit-loss-pct", type=float,
                   default=d.candle_pattern_exit_loss_pct,
                   help="Override común de pérdida para LONG/SHORT (por defecto usa umbrales por lado).")
    p.add_argument("--candle-pattern-exit-long-loss-pct", type=float,
                   default=d.candle_pattern_exit_long_loss_pct,
                   help="Pérdida abierta mínima para salida LONG (default 0.65%%).")
    p.add_argument("--candle-pattern-exit-short-loss-pct", type=float,
                   default=d.candle_pattern_exit_short_loss_pct,
                   help="Pérdida abierta mínima para salida SHORT (default 0.6%%).")
    p.add_argument("--candle-pattern-exit-signed-loss-pct", type=float,
                   default=d.candle_pattern_exit_signed_loss_pct,
                   help="Pérdida mínima para patrones bidireccionales (default 0.8%%).")
    p.add_argument("--ema55-touch-exit", action=argparse.BooleanOptionalAction,
                   default=d.ema55_touch_exit_enabled,
                   help="Salida LONG en 30m por toques repetidos a EMA55 sin ruptura.")
    p.add_argument("--ema55-touch-tolerance-pct", type=float,
                   default=d.ema55_touch_tolerance_pct,
                   help="Banda de proximidad para contar un toque a EMA55 (default 0.1%%).")
    p.add_argument("--ema55-touch-lookback-bars", type=int,
                   default=d.ema55_touch_lookback_bars,
                   help="Ventana de conteo de toques (default 5 velas).")
    p.add_argument("--ema55-touch-min-touches", type=int,
                   default=d.ema55_touch_min_touches,
                   help="Toques repetidos requeridos dentro de la ventana (default 3).")
    p.add_argument("--ema55-touch-consecutive", type=int,
                   default=d.ema55_touch_consecutive,
                   help="Toques consecutivos suficientes para salir (default 2).")
    p.add_argument("--ema55-touch-breakout-buffer-pct", type=float,
                   default=d.ema55_touch_breakout_buffer_pct,
                   help="Cierre por encima de EMA55 que invalida la debilidad (default 0.1%%).")
    p.add_argument("--ema55-touch-exit-loss-pct", type=float,
                   default=d.ema55_touch_exit_loss_pct,
                   help="Pérdida abierta mínima para confirmar debilidad (default 0.7%%).")
    p.add_argument("--ema55-touch-short-exit-loss-pct", type=float,
                   default=d.ema55_touch_short_exit_loss_pct,
                   help="Pérdida mínima para la salida SHORT (default 0.9%%).")
    p.add_argument("--partial-tp", action=argparse.BooleanOptionalAction, default=d.partial_tp_enabled)
    p.add_argument("--partial-tp-r", type=float, default=d.partial_tp_r)
    p.add_argument("--partial-tp-pct", type=float, default=d.partial_tp_pct)
    p.add_argument("--partial-tp2-r", type=float, default=d.partial_tp2_r)
    p.add_argument("--partial-tp2-pct", type=float, default=d.partial_tp2_pct)
    p.add_argument("--breakeven-stop", action=argparse.BooleanOptionalAction, default=d.move_stop_to_breakeven)
    # Entrada parcial + confirmación
    p.add_argument("--entry-partial", action=argparse.BooleanOptionalAction, default=d.entry_partial_enabled,
                   help="Activa/desactiva entrada parcial 50%%/50%% (scale-in) con lógica de confirmación (por defecto activado).")
    p.add_argument("--entry-partial-pct", type=float, default=d.entry_partial_pct,
                   help="%% de la posición a entrar en la primera fase (default 50).")
    p.add_argument("--entry-confirm-mode", choices=("bars", "close_above_ema", "pullback_retest"),
                   default=d.entry_confirm_mode,
                   help="Modo de confirmación para añadir el resto (default 'pullback_retest').")
    p.add_argument("--entry-confirm-bars", type=int, default=d.entry_confirm_bars,
                   help="Velas tras entrada parcial antes de confirmar (modo 'bars', default 2).")
    p.add_argument("--entry-confirm-close-in-dir", action=argparse.BooleanOptionalAction,
                   default=d.entry_confirm_close_in_dir,
                   help="Exige cierre en dirección para confirmar (modo 'bars').")
    p.add_argument("--entry-confirm-retest-tol-pct", type=float, default=d.entry_confirm_retest_tol_pct,
                   help="Tolerancia %% para pullback_retest (default 0.10%%).")
    p.add_argument("--entry-confirm-max-move-pct", type=float, default=d.entry_confirm_max_move_pct,
                   help="Bloquea añadir el resto si el precio se ha movido en contra más de X%% desde la entrada parcial (0 = desactivado).")
    p.add_argument("--entry-confirm-move-exit", action=argparse.BooleanOptionalAction,
                   default=getattr(d, "entry_confirm_move_exit_enabled", getattr(d, "confirm_entry_adverse_exit_enabled", True)),
                   help="Salida parcial si el precio se mueve en contra de la entrada de confirmación.")
    p.add_argument("--entry-confirm-move-exit-pct", type=float, default=getattr(d, "entry_confirm_move_exit_pct", getattr(d, "confirm_entry_adverse_move_pct", 0.5)),
                   help="%% de movimiento adverso desde la entrada de confirmación para disparar la salida (default 0.5).")
    p.add_argument("--entry-confirm-move-exit-portion-pct", type=float,
                   default=getattr(d, "entry_confirm_move_exit_portion_pct", getattr(d, "confirm_entry_adverse_exit_pct", 50.0)),
                   help="%% de la posición restante a cerrar en esa salida (default 50).")
    p.add_argument("--confirm-adverse-exit", action=argparse.BooleanOptionalAction,
                   default=getattr(d, "confirm_entry_adverse_exit_enabled", getattr(d, "entry_confirm_move_exit_enabled", True)),
                   help="Salida parcial después de la entrada por confirmación si el precio va en contra.")
    p.add_argument("--confirm-adverse-move-pct", type=float, default=getattr(d, "confirm_entry_adverse_move_pct", 0.5),
                   help="%% adverso desde la entrada de confirmación para disparar la salida (default 0.5).")
    p.add_argument("--confirm-adverse-exit-pct", type=float, default=getattr(d, "confirm_entry_adverse_exit_pct", 50.0),
                   help="%% de la posición a cerrar (default 50).")
    p.add_argument("--close-on-ema-slow-touch", action=argparse.BooleanOptionalAction, default=d.close_on_ema_slow_touch)
    p.add_argument("--close-on-ema-slow-touch-pct", type=float, default=d.close_on_ema_slow_touch_pct,
                   help="Tolerancia en %% alrededor de la EMA slow (0.10 = 0.10%%).")
    p.add_argument("--close-on-ema-slow-touch-after", action=argparse.BooleanOptionalAction,
                   default=d.close_on_ema_slow_touch_after,
                   help="Aplica la tolerancia después del toque a la EMA slow (por defecto, antes).")
    p.add_argument("--close-on-ema-slow-touch-exit-pct", type=float, default=d.close_on_ema_slow_touch_exit_pct,
                   help="%% de la posición a cerrar al tocar la EMA slow (100 = cierre total, < 100 = parcial).")
    p.add_argument("--no-long", action="store_true")
    p.add_argument("--no-short", action="store_true")
    p.add_argument("--trend-filter", choices=("off", "cross", "slow"), default=d.trend_filter,
                   help="Filtro de régimen: entradas sólo en la dirección de la tendencia "
                        "del timeframe superior ('cross' = cruce EMA, 'slow' = EMA slow).")
    p.add_argument("--trend-timeframe", default=d.trend_timeframe,
                   help="Timeframe del filtro de régimen (default 1D).")
    p.add_argument("--chop-filter", action=argparse.BooleanOptionalAction, default=d.chop_filter,
                   help="Sólo entra si el ADX de --chop-timeframe supera --chop-adx-min.")
    p.add_argument("--chop-timeframe", default=d.chop_timeframe)
    p.add_argument("--chop-adx-min", type=float, default=d.chop_adx_min)
    p.add_argument("--monthly-loss-limit-pct", type=float, default=d.monthly_loss_limit_pct,
                   help="%% máxima de pérdida mensual antes de dejar de operar el resto del mes "
                        "(0 = desactivado).")
    p.add_argument("--monthly-loss-limit-close", action=argparse.BooleanOptionalAction,
                   default=d.monthly_loss_limit_close,
                   help="Cierra la posición abierta al alcanzar el límite mensual.")
    p.add_argument("--monthly-loss-limit-days", type=int, default=d.monthly_loss_limit_days,
                   help="Días sin entradas tras el límite mensual (0 = hasta el próximo mes).")
    p.add_argument("--monthly-trend-gate-pct", type=float, default=d.monthly_trend_gate_pct,
                   help="%% de pérdida mensual acumulada a partir de la cual las entradas sólo se abren "
                        "a favor de la tendencia (0 = desactivado).")
    p.add_argument("--monthly-gate-mode", choices=("trend", "all"), default=d.monthly_gate_mode,
                   help="'trend' exige alineación con la tendencia; 'all' bloquea toda entrada "
                        "mientras el mes está en rojo.")
    p.add_argument("--monthly-risk-scaling", action=argparse.BooleanOptionalAction,
                   default=d.monthly_risk_scaling,
                   help="Escala el tamaño de las posiciones con el drawdown del mes, hasta "
                        "--monthly-risk-scaling-min al llegar al límite mensual.")
    p.add_argument("--monthly-risk-scaling-min", type=float, default=d.monthly_risk_scaling_min,
                   help="Tamaño mínimo (fracción de risk_pct) al fondo del drawdown mensual (default 0.35).")
    p.add_argument("--consecutive-loss-pause", type=int, default=d.consecutive_loss_pause,
                   help="Pausa las entradas tras N posiciones consecutivas en pérdida (0 = desactivado).")
    p.add_argument("--consecutive-loss-pause-bars", type=int, default=d.consecutive_loss_pause_bars,
                   help="Velas sin entradas tras la racha de pérdidas (default 96 = 48h en 30min).")
    p.add_argument("--consecutive-loss-skip-signals", type=int, default=d.consecutive_loss_skip_signals,
                   help="Señales de entrada a ignorar tras cada pérdida (0 = desactivado).")
    p.add_argument("--streak-trend-only", action=argparse.BooleanOptionalAction, default=d.streak_trend_only,
                   help="Tras una pérdida, sólo entrar si la señal coincide con la tendencia mayor.")
    p.add_argument("--adaptive-risk-window", type=int, default=d.adaptive_risk_window,
                   help="Nº de últimas posiciones para escalar el riesgo (0 = tamaño fijo).")
    p.add_argument("--adaptive-risk-k", type=float, default=d.adaptive_risk_k)
    p.add_argument("--adaptive-risk-min", type=float, default=d.adaptive_risk_min)
    p.add_argument("--adaptive-risk-max", type=float, default=d.adaptive_risk_max)
    p.add_argument("--no-force-close", action="store_true")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    p.add_argument("--no-output", action="store_true")
    p.add_argument("--equity-csv", action="store_true")
    p.add_argument("--no-plot", action="store_true")
    p.add_argument("--plot", default=None)
    p.add_argument("--plot-dpi", type=int, default=150)
    return p.parse_args()


def config_from_args(a) -> Config:
    return Config(ema_fast=a.ema_fast, ema_slow=a.ema_slow, timeframe=a.timeframe,
        allow_long=not a.no_long, allow_short=not a.no_short, initial_capital=a.capital,
        commission_bps=a.commission_bps, slippage_bps=a.slippage_bps, risk_pct=a.risk_pct,
        max_leverage=a.max_leverage, stop_loss_pct=a.stop_loss_pct, confirm_bars=a.confirm_bars,
        entry_adx_enabled=a.entry_adx_filter, entry_adx_len=a.entry_adx_len, entry_adx_min=a.entry_adx_min,
        entry_dmi_risk_scaling_enabled=a.entry_dmi_risk_scaling,
        entry_dmi_risk_base=a.entry_dmi_risk_base,
        entry_dmi_risk_spread_scale=a.entry_dmi_risk_spread_scale,
        entry_dmi_risk_min=a.entry_dmi_risk_min,
        entry_dmi_risk_max=a.entry_dmi_risk_max,
        entry_mfi_risk_scaling_enabled=a.entry_mfi_risk_scaling,
        entry_mfi_len=a.entry_mfi_len,
        entry_mfi_risk_base=a.entry_mfi_risk_base,
        entry_mfi_risk_spread_scale=a.entry_mfi_risk_spread_scale,
        entry_mfi_risk_min=a.entry_mfi_risk_min,
        entry_mfi_risk_max=a.entry_mfi_risk_max,
        entry_er_risk_scaling_enabled=a.entry_er_risk_scaling,
        entry_er_len=a.entry_er_len,
        entry_er_risk_base=a.entry_er_risk_base,
        entry_er_risk_slope=a.entry_er_risk_slope,
        entry_er_risk_min=a.entry_er_risk_min,
        entry_er_risk_max=a.entry_er_risk_max,
        adx_exit_enabled=a.adx_exit, adx_timeframe=a.adx_timeframe, adx_dilen=a.adx_dilen,
        adx_adxlen=a.adx_adxlen, adx_key_level=a.adx_key_level, adx_exit_pct=a.adx_exit_pct,
        adx_exit_range=a.adx_exit_range,
        ema_weakening_exit_enabled=a.ema_weakening_exit, ema_weakening_timeframe=a.ema_weakening_timeframe,
        ema_weakening_fast=a.ema_weakening_fast, ema_weakening_slow=a.ema_weakening_slow,
        ema_weakening_gap_pct=a.ema_weakening_gap_pct, ema_weakening_exit_pct=a.ema_weakening_exit_pct,
        rsi_divergence_exit_enabled=a.rsi_divergence_exit,
        rsi_divergence_timeframe=a.rsi_divergence_timeframe,
        rsi_divergence_len=a.rsi_divergence_len,
        rsi_divergence_pivot=a.rsi_divergence_pivot,
        rsi_divergence_lookback=a.rsi_divergence_lookback,
        candle_pattern_exit_enabled=a.candle_pattern_exit,
        candle_pattern_exit_pct=a.candle_pattern_exit_pct,
        candle_pattern_exit_confirm_bars=a.candle_pattern_exit_confirm_bars,
        candle_pattern_exit_loss_pct=a.candle_pattern_exit_loss_pct,
        candle_pattern_exit_long_loss_pct=a.candle_pattern_exit_long_loss_pct,
        candle_pattern_exit_short_loss_pct=a.candle_pattern_exit_short_loss_pct,
        candle_pattern_exit_signed_loss_pct=a.candle_pattern_exit_signed_loss_pct,
        ema55_touch_exit_enabled=a.ema55_touch_exit,
        ema55_touch_tolerance_pct=a.ema55_touch_tolerance_pct,
        ema55_touch_lookback_bars=a.ema55_touch_lookback_bars,
        ema55_touch_min_touches=a.ema55_touch_min_touches,
        ema55_touch_consecutive=a.ema55_touch_consecutive,
        ema55_touch_breakout_buffer_pct=a.ema55_touch_breakout_buffer_pct,
        ema55_touch_exit_loss_pct=a.ema55_touch_exit_loss_pct,
        ema55_touch_short_exit_loss_pct=a.ema55_touch_short_exit_loss_pct,
        partial_tp_enabled=a.partial_tp, partial_tp_r=a.partial_tp_r, partial_tp_pct=a.partial_tp_pct,
        partial_tp2_r=a.partial_tp2_r, partial_tp2_pct=a.partial_tp2_pct,
        move_stop_to_breakeven=a.breakeven_stop,
        entry_partial_enabled=a.entry_partial,
        entry_partial_pct=a.entry_partial_pct,
        entry_confirm_mode=a.entry_confirm_mode,
        entry_confirm_bars=a.entry_confirm_bars,
        entry_confirm_close_in_dir=a.entry_confirm_close_in_dir,
        entry_confirm_retest_tol_pct=a.entry_confirm_retest_tol_pct,
        entry_confirm_max_move_pct=a.entry_confirm_max_move_pct,
        entry_confirm_move_exit_enabled=getattr(a, "confirm_adverse_exit", getattr(a, "entry_confirm_move_exit", True)),
        entry_confirm_move_exit_pct=getattr(a, "confirm_adverse_move_pct", getattr(a, "entry_confirm_move_exit_pct", 0.5)),
        entry_confirm_move_exit_portion_pct=getattr(a, "confirm_adverse_exit_pct", getattr(a, "entry_confirm_move_exit_portion_pct", 50.0)),
        close_on_ema_slow_touch=a.close_on_ema_slow_touch,
        close_on_ema_slow_touch_pct=a.close_on_ema_slow_touch_pct,
        close_on_ema_slow_touch_after=a.close_on_ema_slow_touch_after,
        close_on_ema_slow_touch_exit_pct=a.close_on_ema_slow_touch_exit_pct,
        trend_filter=a.trend_filter, trend_timeframe=a.trend_timeframe,
        chop_filter=a.chop_filter, chop_timeframe=a.chop_timeframe, chop_adx_min=a.chop_adx_min,
        monthly_loss_limit_pct=a.monthly_loss_limit_pct,
        monthly_loss_limit_close=a.monthly_loss_limit_close,
        monthly_loss_limit_days=a.monthly_loss_limit_days,
        monthly_trend_gate_pct=a.monthly_trend_gate_pct,
        monthly_gate_mode=a.monthly_gate_mode,
        monthly_risk_scaling=a.monthly_risk_scaling,
        monthly_risk_scaling_min=a.monthly_risk_scaling_min,
        consecutive_loss_pause=a.consecutive_loss_pause,
        consecutive_loss_pause_bars=a.consecutive_loss_pause_bars,
        consecutive_loss_skip_signals=a.consecutive_loss_skip_signals,
        streak_trend_only=a.streak_trend_only,
        adaptive_risk_window=a.adaptive_risk_window,
        adaptive_risk_k=a.adaptive_risk_k,
        adaptive_risk_min=a.adaptive_risk_min,
        adaptive_risk_max=a.adaptive_risk_max,
        force_close_at_end=not a.no_force_close)


def main():
    args = parse_args()
    cfg = config_from_args(args)
    raw = load_parquet(args.parquet)
    bars = to_timeframe(raw, cfg)
    if len(bars) < cfg.ema_slow + 2:
        raise ValueError(f"Dataset insuficiente para EMA{cfg.ema_slow}: {len(bars)} velas.")
    strategy = EMACrossStrategy(cfg)
    signals = strategy.generate_signals(bars, raw)
    adx_exit, weak_exit, rsi_div_exit = strategy.prepare_exits(raw, bars)
    (candle_bull_exit, candle_bear_exit, signed_bull_exit,
     signed_bear_exit, doji_exit) = strategy.prepare_candle_exits(bars)
    candle_exit = None
    if candle_bull_exit is not None:
        candle_exit = (
            (candle_bull_exit | signed_bull_exit).astype(np.int8)
            - (candle_bear_exit | signed_bear_exit).astype(np.int8)
        )
    if cfg.adx_exit_enabled:
        print(f"[ADX] {'disponible' if adx_exit is not None else 'sin datos suficientes/timeframe incompatible'}", flush=True)
    if cfg.ema_weakening_exit_enabled:
        print(f"[WEAKENING] {'disponible' if weak_exit is not None else 'sin datos suficientes'}", flush=True)
    if cfg.rsi_divergence_exit_enabled:
        print(f"[RSI DIV] {'disponible' if rsi_div_exit is not None else 'sin datos suficientes/timeframe incompatible'}", flush=True)
    if cfg.candle_pattern_exit_enabled:
        print(f"[CANDLE PATTERN] {'disponible' if candle_exit is not None else 'sin datos suficientes'}", flush=True)
    engine = Backtester(
        bars, cfg, signals, adx_exit, weak_exit, rsi_div_exit, verbose=args.verbose,
        candle_bull_exit=candle_bull_exit, candle_bear_exit=candle_bear_exit,
        signed_bull_exit=signed_bull_exit, signed_bear_exit=signed_bear_exit,
        doji_exit=doji_exit,
    )
    trades, equity = engine.run()
    generate_report(
        trades, equity, cfg, bars, args.parquet,
        output_dir=args.output_dir,
        write_outputs_enabled=not args.no_output,
        write_equity_csv=args.equity_csv,
        plot_enabled=not args.no_plot,
        plot_path=args.plot,
        plot_dpi=args.plot_dpi,
        raw=raw,
        signals=signals,
        adx_exit=adx_exit,
        weak_exit=weak_exit,
        rsi_div_exit=rsi_div_exit,
        candle_exit=candle_exit,
        doji_exit=doji_exit,
        opens=engine.opened,
        adds=engine.adds,
        add_skipped=getattr(engine, "add_skipped", None),
    )


if __name__ == "__main__":
    main()
