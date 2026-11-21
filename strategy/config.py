from __future__ import annotations
from dataclasses import dataclass, fields
from decimal import Decimal
import math
from numbers import Real
import pandas as pd


def timeframe_tuple(raw: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Normalize one or several timeframes into a tuple.

    Accepts a comma-separated ``str`` (``"1D,30min"``) or a sequence
    (``["1D", "30min"]``) and returns a tuple with empty entries removed.
    """
    if isinstance(raw, (list, tuple)):
        parts = [str(item) for item in raw]
    else:
        parts = str(raw).split(",")
    return tuple(part.strip() for part in parts if part.strip())


@dataclass
class Config:
    """Configuration for the EMA/MACD crossover strategy and its exits.

    All timeframe strings follow pandas offset aliases (e.g. "30min", "1h", "1D").
    Validation of ranges and consistency is performed in ``__post_init__``.
    """
    #
    # Fast EMA period used for the entry crossover.
    #
    ema_fast: int = 55
    #
    # Slow EMA period used for the entry crossover.
    #
    ema_slow: int = 200
    #
    # MACD periods for entry signals: the signal line is compared against zero.
    #
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    #
    # None = use the strategy's main timeframe.
    #
    macd_timeframe: str | None = "30min"
    #
    # Main strategy timeframe.
    #
    timeframe: str = "30min"
    #
    # Whether long positions are allowed.
    #
    allow_long: bool = True
    #
    # Whether short positions are allowed.
    #
    allow_short: bool = True
    #
    # Initial account equity.
    #
    initial_capital: float = 10_000.0
    #
    # Commission cost in basis points per trade.
    #
    commission_bps: float = 0.0
    #
    # Slippage cost in basis points per trade.
    #
    slippage_bps: float = 0.0
    #
    # Fraction of equity risked per trade.
    #
    risk_pct: float = 0.10
    #
    # Stop-loss distance as a fraction of entry price.
    #
    stop_loss_pct: float = 0.01
    #
    # Bars required to confirm an EMA crossover before acting.
    #
    confirm_bars: int = 4
    #
    # Block EMA crossovers when the entry ADX is below the minimum.
    #
    entry_adx_enabled: bool = True
    entry_adx_len: int = 14
    entry_adx_min: float = 20.0
    #
    # Block entries if ADX made a local peak above entry_adx_min within the
    # previous N bars of the strategy timeframe, on any entry ADX timeframe.
    # Set to 0 to disable this additional filter.
    #
    entry_adx_reversal_lookback: int = 1
    #
    # All listed timeframes must clear the threshold to allow an entry.
    #
    entry_adx_timeframe: str | list[str] | tuple[str, ...] = ("30min")
    #
    # Block entries when ATR(14) is abnormally low/high versus its mean over the
    # previous candles. Does not affect exits of open positions.
    #
    entry_atr_enabled: bool = True
    entry_atr_len: int = 14
    entry_atr_lookback: int = 50
    entry_atr_min_ratio: float = 0.5
    entry_atr_max_ratio: float = 2.5
    #
    # Block entries when, at crossover confirmation, price sits on the wrong side
    # of both EMAs of the crossover: a LONG is not opened if the close is below
    # both fast and slow EMA; a SHORT is not opened if the close is above both.
    # Uses `ema_fast`/`ema_slow` of the strategy timeframe (does not affect exits).
    #
    entry_price_ema_filter_enabled: bool = True
    #
    # Require the Gaussian Filter Trend line to be rising for LONG entries and
    # falling for SHORT entries. Parameters match the indicator's Default preset.
    #
    entry_gaussian_trend_enabled: bool = True
    entry_gaussian_source: str = "close"
    entry_gaussian_length: int = 14
    entry_gaussian_poles: int = 4
    entry_gaussian_atr_length: int = 14
    entry_gaussian_fixed_multiplier: float = 1.5
    entry_gaussian_adaptive_width: bool = True
    entry_gaussian_efficiency_length: int = 10
    entry_gaussian_trend_multiplier: float = 0.8
    entry_gaussian_chop_multiplier: float = 2.5
    entry_gaussian_efficiency_smooth: int = 5
    #
    # Partially close positions when the Gaussian trend changes direction
    # against them. The transition is evaluated once on the closed signal bar.
    #
    gaussian_trend_reversal_exit_enabled: bool = True
    gaussian_trend_reversal_exit_pct: float = 5.0
    # One or several timeframes (combined with OR); None = main timeframe.
    gaussian_trend_reversal_exit_timeframe: (
        str | list[str] | tuple[str, ...] | None
    ) = ("30min")
    #
    # --- RSI divergence ------------------------------------------------------
    # Entry filter: block LONG after bearish divergence and SHORT after bullish
    # divergence while its age is <= `entry_rsi_divergence_lookback` strategy bars.
    #
    entry_rsi_divergence_enabled: bool = True
    entry_rsi_divergence_timeframe: str | list[str] | tuple[str, ...] = ("1D")
    entry_rsi_divergence_lookback: int = 48
    #
    # Exit: close the configured percentage when a divergence appears against
    # the open position. Entry and exit can use independent timeframes.
    #
    exit_rsi_divergence_enabled: bool = True
    exit_rsi_divergence_pct: float = 100.0
    exit_rsi_divergence_timeframe: str | list[str] | tuple[str, ...] = ("1D")
    #
    # Shared RSI divergence calculation parameters for entry and exit.
    #
    rsi_divergence_len: int = 14       # RSI period (Wilder)
    rsi_divergence_pivot: int = 5      # Bars on each side to confirm a pivot
    rsi_divergence_lookback: int = 60  # Previous-pivot search window
    #
    #
    # ADX-based exit (reversal) configuration.
    #
    adx_exit_enabled: bool = True
    #
    # One or several timeframes (combined with OR). Accepts "1D", "1D,30min" or
    # ["1D", "30min"]; the ADX exit triggers if a reversal occurs in any of them.
    #
    adx_timeframe: str | list[str] | tuple[str, ...] = ("1D")
    #
    # Window (in strategy-timeframe bars) during which an ADX reversal keeps the
    # exit active.
    #
    adx_exit_range: int = 9
    adx_dilen: int = 14
    adx_adxlen: int = 14
    adx_key_level: float = 23.0
    adx_exit_pct: float = 100.0
    # Exit management aimed at reducing whipsaw loss streaks: when price reaches
    # `partial_tp_r` times the initial risk, profit is locked by closing
    # `partial_tp_pct` of the position and, optionally, moving the stop of the
    # remainder to breakeven. Raises the real win rate (breaks consecutive
    # stop-loss streaks) at the cost of trimming part of the return.
    #
    partial_tp_enabled: bool = True
    partial_tp_r: float = 0.5
    partial_tp_pct: float = 5.0
    #
    # Second partial take-profit (optional). If enabled (>0), it triggers at
    # partial_tp2_r (R-multiple of initial risk) closing partial_tp2_pct of the
    # position. Partial 2 is intended to be at a larger distance than TP1.
    #
    partial_tp2_r: float = 1.0
    partial_tp2_pct: float = 5.0
    #
    # Third partial take-profit, at a larger distance than TP2.
    #
    partial_tp3_r: float = 1.5
    partial_tp3_pct: float = 5.0
    #
    # Fourth partial take-profit, at a larger distance than TP3.
    #
    partial_tp4_r: float = 2.0
    partial_tp4_pct: float = 5.0
    #
    # Fifth partial take-profit, at a larger distance than TP4.
    #
    partial_tp5_r: float = 3.0
    partial_tp5_pct: float = 5.0
    #
    # Sixth partial take-profit, at a larger distance than TP5.
    #
    partial_tp6_r: float = 4.0
    partial_tp6_pct: float = 5.0
    #
    # Seventh partial take-profit, at a larger distance than TP6.
    #
    partial_tp7_r: float = 5.0
    partial_tp7_pct: float = 5.0
    #
    # Eighth partial take-profit, at a larger distance than TP7.
    #
    partial_tp8_r: float = 6.0
    partial_tp8_pct: float = 5.0
    #
    # Ninth partial take-profit, at a larger distance than TP8.
    #
    partial_tp9_r: float = 7.0
    partial_tp9_pct: float = 5.0
    #
    # Tenth partial take-profit, at a larger distance than TP9.
    #
    partial_tp10_r: float = 8.0
    partial_tp10_pct: float = 5.0
    #
    # After TP3, the remaining SHORT exits if price exceeds the slow EMA by
    # this %. 0 = touching it is enough.
    #
    tp3_ema_slow_exit_enabled: bool = True
    tp3_ema_slow_penetration_pct: float = 1.75
    #
    # Whether to move the stop to breakeven after a partial take-profit.
    #
    move_stop_to_breakeven: bool = True
    #
    # Higher-timeframe trend bias, used only after a loss.
    #
    trend_timeframe: str = "1D"
    #
    # EMA lengths used to calculate the higher-timeframe trend bias. A value of
    # 0 reuses the corresponding entry EMA length (`ema_fast` / `ema_slow`).
    #
    trend_ema_fast: int = 100
    trend_ema_slow: int = 200
    #
    # --- Monthly protection -----------------------------------------------
    # If equity falls more than `monthly_loss_limit_pct` % below the equity at
    # the start of the calendar month, new positions stop being opened (and, with
    # monthly_loss_limit_close, the open one is closed) until the next month or,
    # if `monthly_loss_limit_days` > 0, for that many days. Safety net for the
    # worst month. With defaults it barely triggers (1 time in 82 months), so it
    # starts loose at 5.0. Lowering the limit (e.g. 2) improves the worst month
    # but trims cumulative negative months. 0 = disabled.
    #
    monthly_loss_limit_pct: float = 5.0
    monthly_loss_limit_close: bool = True
    monthly_loss_limit_days: int = 0
    #
    # After a position closed at a loss, the next entry only opens if it aligns
    # with the trend bias of `trend_timeframe` (already-closed higher bar, no
    # lookahead); a win resets the requirement. Targets loss streaks: in
    # whipsaw the crossover signal often runs against the larger trend. Only
    # demands the bias after a loss, not always, to avoid trimming returns of
    # crossovers in trend. On BTCUSDT 2020-2026 reduces the max streak from 7
    # to 5 (train and test), raises PF from 1.76 to 2.07 and trims the worst
    # month from -4.75% to -2.65%, with total return above baseline
    # (328% -> 363%). False = disabled.
    #
    streak_trend_only: bool = True
    #
    force_close_at_end: bool = True

    @property
    def adx_timeframes(self) -> tuple[str, ...]:
        """ADX timeframes normalized (one or several); exit uses OR across them."""
        return timeframe_tuple(self.adx_timeframe)

    @property
    def entry_adx_timeframes(self) -> tuple[str, ...]:
        """Entry ADX timeframes normalized; all of them must pass the threshold."""
        return timeframe_tuple(self.entry_adx_timeframe)

    @property
    def trend_ema_lengths(self) -> tuple[int, int]:
        """Lengths (fast, slow) used for the trend-timeframe bias.

        Uses `trend_ema_fast`/`trend_ema_slow` when defined (> 0);
        otherwise falls back to the entry crossover EMAs.
        """
        fast = self.trend_ema_fast if self.trend_ema_fast > 0 else self.ema_fast
        slow = self.trend_ema_slow if self.trend_ema_slow > 0 else self.ema_slow
        return fast, slow

    @property
    def entry_rsi_divergence_timeframes(self) -> tuple[str, ...]:
        """Entry divergence timeframes normalized."""
        return timeframe_tuple(self.entry_rsi_divergence_timeframe)

    @property
    def exit_rsi_divergence_timeframes(self) -> tuple[str, ...]:
        """Exit divergence timeframes normalized."""
        return timeframe_tuple(self.exit_rsi_divergence_timeframe)

    @property
    def gaussian_trend_reversal_exit_timeframes(self) -> tuple[str, ...]:
        """Gaussian reversal exit timeframes normalized; exit uses OR."""
        if self.gaussian_trend_reversal_exit_timeframe is None:
            return (self.timeframe,)
        return timeframe_tuple(self.gaussian_trend_reversal_exit_timeframe)

    def __post_init__(self):
        """Validate ranges and internal consistency of the configuration."""
        for config_field in fields(self):
            value = getattr(self, config_field.name)
            if isinstance(value, (Real, Decimal)) and not math.isfinite(float(value)):
                raise ValueError(f"{config_field.name} must be finite.")

        if min(self.ema_fast, self.ema_slow) <= 0 or self.ema_fast >= self.ema_slow:
            raise ValueError("Requires 0 < ema_fast < ema_slow.")
        if min(self.macd_fast, self.macd_slow, self.macd_signal) <= 0 or self.macd_fast >= self.macd_slow:
            raise ValueError("Requires 0 < macd_fast < macd_slow and macd_signal > 0.")
        if self.macd_timeframe is not None and pd.Timedelta(self.macd_timeframe) <= pd.Timedelta(0):
            raise ValueError("macd_timeframe must be positive or None to use timeframe.")
        if pd.Timedelta(self.timeframe) <= pd.Timedelta(0):
            raise ValueError("timeframe must be positive.")
        for timeframe in self.gaussian_trend_reversal_exit_timeframes:
            if pd.Timedelta(timeframe) <= pd.Timedelta(0):
                raise ValueError(
                    f"invalid gaussian_trend_reversal_exit_timeframe: {timeframe!r}."
                )
        if (
            self.gaussian_trend_reversal_exit_enabled
            and not self.gaussian_trend_reversal_exit_timeframes
        ):
            raise ValueError("gaussian_trend_reversal_exit_timeframe cannot be empty.")
        for timeframe in self.adx_timeframes:
            if pd.Timedelta(timeframe) <= pd.Timedelta(0):
                raise ValueError(f"invalid adx_timeframe: {timeframe!r}.")
        if self.adx_exit_enabled and not self.adx_timeframes:
            raise ValueError("adx_timeframe cannot be empty.")
        for timeframe in self.entry_adx_timeframes:
            if pd.Timedelta(timeframe) <= pd.Timedelta(0):
                raise ValueError(f"invalid entry_adx_timeframe: {timeframe!r}.")
        if self.entry_adx_enabled and not self.entry_adx_timeframes:
            raise ValueError("entry_adx_timeframe cannot be empty.")
        if self.entry_rsi_divergence_enabled \
                and not self.entry_rsi_divergence_timeframes:
            raise ValueError("entry_rsi_divergence_timeframe cannot be empty.")
        if self.exit_rsi_divergence_enabled \
                and not self.exit_rsi_divergence_timeframes:
            raise ValueError("exit_rsi_divergence_timeframe cannot be empty.")
        for timeframe in (
            *self.entry_rsi_divergence_timeframes,
            *self.exit_rsi_divergence_timeframes,
        ):
            if pd.Timedelta(timeframe) <= pd.Timedelta(0):
                raise ValueError(f"invalid RSI divergence timeframe: {timeframe!r}.")
        if self.initial_capital <= 0:
            raise ValueError("Capital must be positive.")
        if not 0 < self.risk_pct <= 0.2:
            raise ValueError("risk_pct must be in (0, 0.2].")
        if min(self.commission_bps, self.slippage_bps, self.stop_loss_pct) < 0:
            raise ValueError("Commissions, slippage and stop cannot be negative.")
        if self.allow_long and self.stop_loss_pct >= 1:
            raise ValueError(
                "stop_loss_pct must be < 1 when LONG positions are enabled."
            )
        if self.confirm_bars < 0:
            raise ValueError("confirm_bars cannot be negative.")
        if self.entry_adx_enabled and (self.entry_adx_len <= 0 or self.entry_adx_min < 0):
            raise ValueError("Invalid entry ADX configuration.")
        if self.entry_adx_enabled and self.entry_adx_reversal_lookback < 0:
            raise ValueError("entry_adx_reversal_lookback must be >= 0.")
        if self.entry_atr_enabled and (
            self.entry_atr_len <= 0 or self.entry_atr_lookback <= 0
            or self.entry_atr_min_ratio < 0
            or self.entry_atr_max_ratio <= self.entry_atr_min_ratio
        ):
            raise ValueError("Invalid entry ATR filter configuration.")
        if (
            self.entry_gaussian_trend_enabled
            or self.gaussian_trend_reversal_exit_enabled
        ) and (
            self.entry_gaussian_source not in {"close", "hl2", "hlc3"}
            or self.entry_gaussian_length < 2
            or not 1 <= self.entry_gaussian_poles <= 4
            or self.entry_gaussian_atr_length <= 0
            or self.entry_gaussian_fixed_multiplier <= 0
            or self.entry_gaussian_efficiency_length < 2
            or self.entry_gaussian_trend_multiplier <= 0
            or self.entry_gaussian_chop_multiplier <= 0
            or self.entry_gaussian_efficiency_smooth <= 0
        ):
            raise ValueError("Invalid Gaussian Filter Trend entry configuration.")
        if self.entry_rsi_divergence_enabled and self.entry_rsi_divergence_lookback < 1:
            raise ValueError("entry_rsi_divergence_lookback must be >= 1.")
        if self.adx_exit_enabled:
            if min(self.adx_dilen, self.adx_adxlen) <= 0 or self.adx_key_level < 0:
                raise ValueError("Invalid exit ADX configuration.")
            if not 0 < self.adx_exit_pct <= 100:
                raise ValueError("adx_exit_pct must be in (0, 100].")
            if self.adx_exit_range < 1:
                raise ValueError("adx_exit_range must be >= 1.")
        if self.exit_rsi_divergence_enabled or self.entry_rsi_divergence_enabled:
            if self.rsi_divergence_len <= 0:
                raise ValueError("rsi_divergence_len must be positive.")
            if self.rsi_divergence_pivot < 1:
                raise ValueError("rsi_divergence_pivot must be >= 1.")
            if self.rsi_divergence_lookback < 1:
                raise ValueError("rsi_divergence_lookback must be >= 1.")
        if self.exit_rsi_divergence_enabled and not 0 < self.exit_rsi_divergence_pct <= 100:
            raise ValueError("exit_rsi_divergence_pct must be in (0, 100].")
        if self.gaussian_trend_reversal_exit_enabled and not (
            0 < self.gaussian_trend_reversal_exit_pct <= 100
        ):
            raise ValueError("gaussian_trend_reversal_exit_pct must be in (0, 100].")
        if self.tp3_ema_slow_penetration_pct < 0:
            raise ValueError("tp3_ema_slow_penetration_pct cannot be negative.")
        if self.partial_tp_enabled:
            if self.partial_tp_r <= 0 or not 0 < self.partial_tp_pct <= 100:
                raise ValueError("Invalid partial take-profit configuration.")
        if self.partial_tp2_r > 0 or self.partial_tp2_pct > 0:
            if self.partial_tp2_r <= 0 or not 0 < self.partial_tp2_pct <= 100:
                raise ValueError("Invalid partial take-profit 2 configuration.")
            if self.partial_tp2_r <= self.partial_tp_r:
                raise ValueError("partial_tp2_r must be greater than partial_tp_r.")
        if self.partial_tp3_r > 0 or self.partial_tp3_pct > 0:
            if self.partial_tp3_r <= 0 or not 0 < self.partial_tp3_pct <= 100:
                raise ValueError("Invalid partial take-profit 3 configuration.")
            if self.partial_tp3_r <= self.partial_tp2_r:
                raise ValueError("partial_tp3_r must be greater than TP2.")
        if self.partial_tp4_r > 0 or self.partial_tp4_pct > 0:
            if self.partial_tp4_r <= 0 or not 0 < self.partial_tp4_pct <= 100:
                raise ValueError("Invalid partial take-profit 4 configuration.")
            if self.partial_tp4_r <= self.partial_tp3_r:
                raise ValueError("partial_tp4_r must be greater than TP3.")
        if self.partial_tp5_r > 0 or self.partial_tp5_pct > 0:
            if self.partial_tp5_r <= 0 or not 0 < self.partial_tp5_pct <= 100:
                raise ValueError("Invalid partial take-profit 5 configuration.")
            if self.partial_tp5_r <= self.partial_tp4_r:
                raise ValueError("partial_tp5_r must be greater than TP4.")
        if self.partial_tp6_r > 0 or self.partial_tp6_pct > 0:
            if self.partial_tp6_r <= 0 or not 0 < self.partial_tp6_pct <= 100:
                raise ValueError("Invalid partial take-profit 6 configuration.")
            if self.partial_tp6_r <= self.partial_tp5_r:
                raise ValueError("partial_tp6_r must be greater than TP5.")
        if self.partial_tp7_r > 0 or self.partial_tp7_pct > 0:
            if self.partial_tp7_r <= 0 or not 0 < self.partial_tp7_pct <= 100:
                raise ValueError("Invalid partial take-profit 7 configuration.")
            if self.partial_tp7_r <= self.partial_tp6_r:
                raise ValueError("partial_tp7_r must be greater than TP6.")
        if self.partial_tp8_r > 0 or self.partial_tp8_pct > 0:
            if self.partial_tp8_r <= 0 or not 0 < self.partial_tp8_pct <= 100:
                raise ValueError("Invalid partial take-profit 8 configuration.")
            if self.partial_tp8_r <= self.partial_tp7_r:
                raise ValueError("partial_tp8_r must be greater than TP7.")
        if self.partial_tp9_r > 0 or self.partial_tp9_pct > 0:
            if self.partial_tp9_r <= 0 or not 0 < self.partial_tp9_pct <= 100:
                raise ValueError("Invalid partial take-profit 9 configuration.")
            if self.partial_tp9_r <= self.partial_tp8_r:
                raise ValueError("partial_tp9_r must be greater than TP8.")
        if self.partial_tp10_r > 0 or self.partial_tp10_pct > 0:
            if self.partial_tp10_r <= 0 or not 0 < self.partial_tp10_pct <= 100:
                raise ValueError("Invalid partial take-profit 10 configuration.")
            if self.partial_tp10_r <= self.partial_tp9_r:
                raise ValueError("partial_tp10_r must be greater than TP9.")
        if not (self.allow_long or self.allow_short):
            raise ValueError("LONG, SHORT or both must be enabled.")
        if self.streak_trend_only and pd.Timedelta(self.trend_timeframe) <= pd.Timedelta(0):
            raise ValueError("trend_timeframe must be positive.")
        if self.streak_trend_only:
            if min(self.trend_ema_fast, self.trend_ema_slow) < 0:
                raise ValueError("trend_ema_fast/trend_ema_slow cannot be negative.")
            trend_fast, trend_slow = self.trend_ema_lengths
            if trend_fast >= trend_slow:
                raise ValueError(
                    "Requires trend_ema_fast < trend_ema_slow "
                    "(or 0 to use the corresponding entry EMA)."
                )
        if self.monthly_loss_limit_pct < 0:
            raise ValueError("monthly_loss_limit_pct cannot be negative.")
        if self.monthly_loss_limit_days < 0:
            raise ValueError("monthly_loss_limit_days cannot be negative.")
