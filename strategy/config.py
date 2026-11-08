from __future__ import annotations
from dataclasses import dataclass
import pandas as pd


def timeframe_tuple(raw: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Normaliza uno o varios timeframes.

    Acepta un ``str`` separado por comas (``"1D,30min"``) o una secuencia
    (``["1D", "30min"]``) y devuelve una tupla sin elementos vacíos.
    """
    if isinstance(raw, (list, tuple)):
        parts = [str(item) for item in raw]
    else:
        parts = str(raw).split(",")
    return tuple(part.strip() for part in parts if part.strip())


@dataclass
class Config:
    ema_fast: int = 55
    ema_slow: int = 200
    timeframe: str = "30min"
    allow_long: bool = True
    allow_short: bool = True

    initial_capital: float = 10_000.0
    commission_bps: float = 0.0
    slippage_bps: float = 0.0
    risk_pct: float = 0.01
    max_leverage: float = 1.0
    stop_loss_pct: float = 0.01

    confirm_bars: int = 4
    entry_adx_enabled: bool = True
    entry_adx_len: int = 14
    entry_adx_min: float = 20.0
    # Escala el riesgo (no bloquea la señal) según DI direccional en la vela
    # confirmada: factor = clamp(base + (DI a favor - DI en contra) / escala).
    entry_dmi_risk_scaling_enabled: bool = True
    entry_dmi_risk_base: float = 0.9
    entry_dmi_risk_spread_scale: float = 100.0
    entry_dmi_risk_min: float = 0.5
    entry_dmi_risk_max: float = 1.25
    # MFI modula adicionalmente el tamaño según flujo de dinero a favor.
    entry_mfi_risk_scaling_enabled: bool = True
    entry_mfi_len: int = 14
    entry_mfi_risk_base: float = 1.0
    entry_mfi_risk_spread_scale: float = 100.0
    entry_mfi_risk_min: float = 0.5
    entry_mfi_risk_max: float = 1.25
    # Efficiency Ratio suaviza el tamaño entre regímenes laterales y direccionales.
    entry_er_risk_scaling_enabled: bool = True
    entry_er_len: int = 20
    entry_er_risk_base: float = 0.75
    entry_er_risk_slope: float = 1.0
    entry_er_risk_min: float = 0.5
    entry_er_risk_max: float = 1.25

    adx_exit_enabled: bool = True
    # Uno o varios timeframes (combinados con OR). Acepta "1D", "1D,30min" o
    # ["1D", "30min"]; la salida ADX se dispara si hay reversión en cualquiera.
    adx_timeframe: str | list[str] | tuple[str, ...] = ("1D")
    # Ventana (en velas del timeframe de la estrategia) durante la cual una
    # reversión ADX mantiene activada la salida.
    adx_exit_range: int = 9
    adx_dilen: int = 14
    adx_adxlen: int = 14
    adx_key_level: float = 23.0
    adx_exit_pct: float = 100.0

    ema_weakening_exit_enabled: bool = True
    # Uno o varios timeframes (combinados con OR). Acepta "1D", "1D,30min" o
    # ["1D", "30min"]; la salida weakening se dispara si hay debilitamiento en
    # cualquiera de ellos.
    ema_weakening_timeframe: str | list[str] | tuple[str, ...] = ("4h", "1D")
    # Longitudes de las EMAs propias de la salida weakening. Si son > 0 se usan
    # en lugar de `ema_fast`/`ema_slow` (permiten elegir el periodo sin tocar el
    # cruce de entrada); con 0 se reutilizan los del cruce.
    ema_weakening_fast: int = 21
    ema_weakening_slow: int = 55
    ema_weakening_gap_pct: float = 0.0001
    ema_weakening_exit_pct: float = 100.0

    # --- Salida por divergencia RSI ----------------------------------------
    # Cierra el 100% de la posición cuando aparece una divergencia RSI en su
    # contra: la **bajista** (precio con máximos más altos, RSI con máximos más
    # bajos) cierra un LONG y la **alcista** (precio con mínimos más bajos, RSI
    # con mínimos más altos) cierra un SHORT. Un pivote de precio se confirma
    # tras `rsi_divergence_pivot` velas a cada lado y se compara con el pivote
    # anterior del mismo tipo dentro de `rsi_divergence_lookback` velas; la
    # señal se emite al confirmarse el pivote, sin lookahead.
    rsi_divergence_exit_enabled: bool = True
    # Uno o varios timeframes (combinados con OR), como en las otras salidas.
    rsi_divergence_timeframe: str | list[str] | tuple[str, ...] = "1D"
    rsi_divergence_len: int = 14       # Periodo del RSI (Wilder)
    rsi_divergence_pivot: int = 5      # Velas a cada lado para confirmar un pivote
    rsi_divergence_lookback: int = 60  # Ventana hacia atrás para el pivote previo

    # --- Salida por patrones de velas TA-Lib -------------------------------
    # Un patrón bajista cierra/reduce LONG; uno alcista cierra/reduce SHORT.
    # Los dojis neutrales sólo confirman el patrón contrario, nunca salen solos.
    candle_pattern_exit_enabled: bool = True
    candle_pattern_exit_pct: float = 100.0
    candle_pattern_exit_confirm_bars: int = 4
    # Los umbrales direccionales permiten compensar la asimetría LONG/SHORT.
    # El umbral común es un override retrocompatible; None usa los dos valores.
    candle_pattern_exit_loss_pct: float | None = None
    candle_pattern_exit_long_loss_pct: float = 0.65
    candle_pattern_exit_short_loss_pct: float = 0.6
    # Bidireccionales: umbral más estricto para compensar el mayor ruido.
    candle_pattern_exit_signed_loss_pct: float = 0.8

    # DI direccional modula el tamaño de entrada; no bloquea señales.
    entry_dmi_risk_scaling_enabled: bool = True
    entry_dmi_risk_base: float = 0.9
    entry_dmi_risk_spread_scale: float = 100.0
    entry_dmi_risk_min: float = 0.5
    entry_dmi_risk_max: float = 1.25

    # --- Debilidad por toques repetidos a EMA55 (30m únicamente) ------------
    # Un toque es el rango de la vela intersectando EMA55 ± tolerancia. Se sale
    # tras 2 toques seguidos o 3 dentro de 5 velas, sin ruptura y con pérdida abierta.
    ema55_touch_exit_enabled: bool = True
    ema55_touch_tolerance_pct: float = 0.1
    ema55_touch_lookback_bars: int = 5
    ema55_touch_min_touches: int = 3
    ema55_touch_consecutive: int = 2
    ema55_touch_breakout_buffer_pct: float = 0.1
    ema55_touch_exit_loss_pct: float = 0.7        # LONG
    ema55_touch_short_exit_loss_pct: float = 0.9  # SHORT, umbral más estricto

    # Gestión de salida orientada a reducir la racha de pérdidas en whipsaw:
    # al alcanzar `partial_tp_r` veces el riesgo inicial se asegura beneficio
    # cerrando `partial_tp_pct` de la posición y, opcionalmente, se mueve el
    # stop del resto a breakeven. Sube el win rate real (rompe rachas de
    # stop-loss consecutivos) a cambio de recortar parte del retorno.
    partial_tp_enabled: bool = True
    partial_tp_r: float = 1.0
    partial_tp_pct: float = 50.0
    # Second partial take-profit (optional). If enabled (>0), it triggers at
    # partial_tp2_r (R-multiple of initial risk) closing partial_tp2_pct of
    # the position. Partial 2 is intended to be at larger distance than TP1.
    partial_tp2_r: float = 1.75
    partial_tp2_pct: float = 25.0
    move_stop_to_breakeven: bool = True

    # Salida si el precio toca la EMA slow después de una entrada por cross EMA.
    # `..._pct` es la tolerancia en % alrededor de la EMA (0.10 = 0.10%).
    # `..._after` decide el sentido de la tolerancia: False (default) la aplica
    # ANTES del toque (LONG dispara en EMA*(1+tol), SHORT en EMA*(1-tol)); True
    # la aplica DESPUÉS del toque (LONG en EMA*(1-tol), SHORT en EMA*(1+tol)).
    # `..._exit_pct` es el % de la posición que se cierra al tocar la EMA
    # (100 = cierre total; < 100 = salida parcial, una sola vez por posición).
    close_on_ema_slow_touch: bool = False
    close_on_ema_slow_touch_pct: float = 0.01  # Tolerancia en % (0.10 = 0.10%)
    close_on_ema_slow_touch_after: bool = False  # False = antes del toque; True = después
    close_on_ema_slow_touch_exit_pct: float = 25.0  # % de la posición a cerrar en el toque

    # --- Entrada parcial + confirmación (scale-in) --------------------------
    # Abre `entry_partial_pct` % de la posición en la señal y añade el resto
    # cuando se cumpla `entry_confirm_mode`. Activado por defecto (50%/50%);
    # `pullback_retest` confirma con una vela direccional que cierra más allá
    # de EMA slow; patrones TA-Lib también pueden confirmar. En la implementación
    # actual no siempre exige un toque real a la EMA.
    entry_partial_enabled: bool = True
    entry_partial_pct: float = 50.0        # % inicial (0 < pct < 100)
    entry_confirm_mode: str = "pullback_retest"  # "bars" | "close_above_ema" | "pullback_retest"
    entry_confirm_bars: int = 2            # Nº de velas tras entrada parcial
    entry_confirm_close_in_dir: bool = True  # Exige cierre a favor para barras y patrones
    entry_confirm_retest_tol_pct: float = 0.10  # Tolerancia % para pullback_retest
    entry_confirm_max_move_pct: float = 0.3  # Si > 0, no añade el resto si el precio se ha movido > X% desde la entrada parcial
    # Si la vela donde ocurre la confirmación para añadir el resto cierra en negativo
    # respecto al precio de entrada parcial, se cierra el 100% de la posición en lugar de hacer scale-in.
    entry_confirm_close_on_negative: bool = False

    # --- Salida parcial en contra de la entrada de confirmación -------------
    # Una vez completada la entrada por confirmación (scale-in), si el precio
    # se mueve `entry_confirm_move_exit_pct` % en contra del precio de esa
    # entrada de confirmación, se cierra `entry_confirm_move_exit_portion_pct` %
    # de la posición restante. Se aplica una sola vez por posición. Es una
    # señal de salida intrabar (stop-like) independiente del stop principal.
    entry_confirm_move_exit_enabled: bool = False
    entry_confirm_move_exit_pct: float = 0.6
    entry_confirm_move_exit_portion_pct: float = 25.0
    # Alias con nombres más descriptivos solicitados
    confirm_entry_adverse_exit_enabled: bool = False
    confirm_entry_adverse_move_pct: float = 0.6
    confirm_entry_adverse_exit_pct: float = 25.0


    # --- Filtro de régimen -------------------------------------------------
    # Sólo se admiten entradas alineadas con la tendencia del timeframe
    # superior `trend_timeframe`:
    #   "off"   -> sin filtro (comportamiento original).
    #   "cross" -> dirección del cruce EMA rápida/lenta en ese timeframe.
    #   "slow"  -> cierre por encima (long) o por debajo (short) de la EMA slow.
    # El sesgo se recalcula con la última vela superior ya cerrada, así que no
    # hay lookahead.
    trend_filter: str = "off"
    trend_timeframe: str = "1D"

    # --- Filtro anti-chop --------------------------------------------------
    # Exige que el ADX (Wilder, `adx_dilen`/`adx_adxlen`) de `chop_timeframe`
    # esté por encima de `chop_adx_min` para abrir posiciones: en mercados
    # laterales el cruce EMA genera muchas entradas pequeñas en pérdida.
    # Igual que el filtro de régimen, se alinea con la última vela ya cerrada.
    chop_filter: bool = False
    chop_timeframe: str = "1D"
    chop_adx_min: float = 25.0

    # --- Protección mensual ------------------------------------------------
    # Si la equity cae más de `monthly_loss_limit_pct` % por debajo de la
    # equity al inicio del mes calendario, se dejan de abrir posiciones (y, con
    # monthly_loss_limit_close, se cierra la abierta) hasta el siguiente mes
    # o, si `monthly_loss_limit_days` > 0, durante ese número de días.
    # Red de seguridad para el peor mes. Con los defaults apenas llega a
    # dispararse (1 vez en 82 meses), por eso arranca holgado en 5.0 y el
    # trabajo de recortar meses rojos lo hacen `monthly_trend_gate_pct` y
    # `adaptive_risk_window`; bajarlo (p. ej. 2) mejora el peor mes pero
    # recorta el acumulado de meses negativos. 0 = desactivado.
    monthly_loss_limit_pct: float = 5.0
    monthly_loss_limit_close: bool = True
    monthly_loss_limit_days: int = 0

    # --- Gate y escalado de riesgo por mes ---------------------------------
    # Palancas para recortar los meses en rojo sin apagar la estrategia:
    # actúan sobre el drawdown *acumulado del mes calendario* (equity mark-to-
    # market respecto a la equity del cierre del mes anterior), no sobre la
    # racha de posiciones.
    #
    # `monthly_trend_gate_pct`: cuando el mes acumula una pérdida ≥ X %, las
    # nuevas entradas sólo se abren a favor del sesgo de tendencia de
    # `trend_timeframe` (mismo requisito que `streak_trend_only`, pero activado
    # por el mes en rojo). En los meses en verde no toca nada, así que no
    # recorta los cruces ganadores. 0 = desactivado.
    monthly_trend_gate_pct: float = 0.0
    # `monthly_gate_mode`: "trend" exige sólo alineación con la tendencia y
    # "all" bloquea cualquier entrada mientras el mes esté en rojo.
    monthly_gate_mode: str = "trend"
    # `monthly_risk_scaling`: escala `risk_pct` con el drawdown del mes:
    # dd = 0 -> 1, dd >= `monthly_loss_limit_pct` -> `monthly_risk_scaling_min`
    # (y ahí ya corta el guard duro). Reduce el tamaño de las posiciones que se
    # abren en la segunda mitad de un mes rojo, donde salen las pérdidas que
    # profundizan el mes. Con el límite mensual a 0 se usa un 2 % de referencia.
    monthly_risk_scaling: bool = False
    monthly_risk_scaling_min: float = 0.35

    # --- Pausa tras rachas de pérdidas -------------------------------------
    # Tras `consecutive_loss_pause` posiciones consecutivas en pérdida no se
    # abren nuevas durante `consecutive_loss_pause_bars` velas (96 velas de
    # 30min = 48h). Evita re-entrar en pleno whipsaw. Medido: no baja la racha
    # máxima y cuesta rendimiento, así que arranca desactivada (0).
    consecutive_loss_pause: int = 0
    consecutive_loss_pause_bars: int = 96

    # Además de la pausa temporal, se ignoran las primeras
    # `consecutive_loss_skip_signals` señales de entrada válidas que aparezcan
    # tras una posición cerrada en pérdida (una victoria las reinicia). Ataca
    # directamente la racha máxima de pérdidas: en whipsaw la siguiente señal
    # suele ser otra trampa. 0 = desactivado.
    consecutive_loss_skip_signals: int = 0

    # Tras una posición cerrada en pérdida, la siguiente entrada sólo se abre
    # si va en la dirección del sesgo de tendencia de `trend_timeframe` (vela
    # superior ya cerrada, sin lookahead); una victoria reinicia el requisito.
    # Ataca la racha de pérdidas: en whipsaw la señal de cruce suele ir contra
    # la tendencia mayor. Sólo exige el sesgo tras perder, no siempre, para no
    # recortar el retorno de los cruces en tendencia. En BTCUSDT 2020-2026
    # baja la racha máxima de 7 a 5 (train y test), sube el PF de 1,76 a 2,07
    # y recorta el peor mes de -4,75% a -2,65%, con el rendimiento total
    # encima del baseline (328% -> 363%). False = desactivado.
    streak_trend_only: bool = True

    # --- Tamaño de posición adaptativo -------------------------------------
    # `risk_pct` se multiplica por `clamp(1 + k * (win_rate últimas n posiciones
    # - 0.5), min, max)`: en rachas de pérdidas se arriesga menos y en rachas
    # ganadoras (mercado en tendencia) se arriesga más. Los meses rojos suelen
    # ser rachas frías, así que recorta el riesgo justo cuando salen las
    # pérdidas: con `adaptive_risk_window=20` y `k=2` el peor mes pasa de
    # -3,15% a -2,49%, el acumulado de meses negativos de -28,6% a -26,1% y el
    # PF de 2,43 a 2,50, a cambio de ~38 puntos de rendimiento total. 0 =
    # desactivado (tamaño fijo).
    adaptive_risk_window: int = 20
    adaptive_risk_k: float = 2.0
    adaptive_risk_min: float = 0.5
    adaptive_risk_max: float = 1.5

    force_close_at_end: bool = True
    timestamp_is_close: bool = False

    @property
    def adx_timeframes(self) -> tuple[str, ...]:
        """Timeframes ADX normalizados (uno o varios); la salida usa OR entre ellos."""
        return timeframe_tuple(self.adx_timeframe)

    @property
    def ema_weakening_timeframes(self) -> tuple[str, ...]:
        """Timeframes de weakening normalizados (uno o varios); la salida usa OR entre ellos."""
        return timeframe_tuple(self.ema_weakening_timeframe)

    @property
    def ema_weakening_lengths(self) -> tuple[int, int]:
        """Longitudes (fast, slow) de las EMAs de la salida weakening.

        Usa `ema_weakening_fast`/`ema_weakening_slow` cuando están definidas
        (> 0); si no, cae a las EMAs del cruce (`ema_fast`/`ema_slow`).
        """
        fast = self.ema_weakening_fast if self.ema_weakening_fast > 0 else self.ema_fast
        slow = self.ema_weakening_slow if self.ema_weakening_slow > 0 else self.ema_slow
        return fast, slow

    @property
    def rsi_divergence_timeframes(self) -> tuple[str, ...]:
        """Timeframes de divergencia RSI normalizados; la salida usa OR entre ellos."""
        return timeframe_tuple(self.rsi_divergence_timeframe)

    def candle_pattern_loss_threshold(self, side: int) -> float:
        """Umbral de pérdida para patrones de reversión; el override común gana."""
        if self.candle_pattern_exit_loss_pct is not None:
            return self.candle_pattern_exit_loss_pct
        return self.candle_pattern_exit_long_loss_pct if side == 1 \
            else self.candle_pattern_exit_short_loss_pct

    def __post_init__(self):
        if min(self.ema_fast, self.ema_slow) <= 0 or self.ema_fast >= self.ema_slow:
            raise ValueError("Se requiere 0 < ema_fast < ema_slow.")
        if pd.Timedelta(self.timeframe) <= pd.Timedelta(0):
            raise ValueError("timeframe debe ser positivo.")
        for timeframe in self.adx_timeframes:
            if pd.Timedelta(timeframe) <= pd.Timedelta(0):
                raise ValueError(f"adx_timeframe inválido: {timeframe!r}.")
        if self.adx_exit_enabled and not self.adx_timeframes:
            raise ValueError("adx_timeframe no puede estar vacío.")
        if self.ema_weakening_exit_enabled and not self.ema_weakening_timeframes:
            raise ValueError("ema_weakening_timeframe no puede estar vacío.")
        for timeframe in self.ema_weakening_timeframes:
            if pd.Timedelta(timeframe) <= pd.Timedelta(0):
                raise ValueError(f"ema_weakening_timeframe inválido: {timeframe!r}.")
        if self.rsi_divergence_exit_enabled and not self.rsi_divergence_timeframes:
            raise ValueError("rsi_divergence_timeframe no puede estar vacío.")
        for timeframe in self.rsi_divergence_timeframes:
            if pd.Timedelta(timeframe) <= pd.Timedelta(0):
                raise ValueError(f"rsi_divergence_timeframe inválido: {timeframe!r}.")
        if self.initial_capital <= 0 or self.max_leverage <= 0:
            raise ValueError("Capital y leverage deben ser positivos.")
        if not 0 < self.risk_pct <= 0.2:
            raise ValueError("risk_pct debe estar en (0, 0.2].")
        if min(self.commission_bps, self.slippage_bps, self.stop_loss_pct) < 0:
            raise ValueError("Comisiones, slippage y stop no pueden ser negativos.")
        if self.confirm_bars < 0:
            raise ValueError("confirm_bars no puede ser negativo.")
        if self.entry_adx_enabled and (self.entry_adx_len <= 0 or self.entry_adx_min < 0):
            raise ValueError("Configuración de ADX de entrada inválida.")
        if self.entry_dmi_risk_scaling_enabled and (
            self.entry_adx_len <= 0 or self.entry_dmi_risk_spread_scale <= 0
            or self.entry_dmi_risk_min <= 0
            or self.entry_dmi_risk_max < self.entry_dmi_risk_min
        ):
            raise ValueError("Configuración de escalado DMI de riesgo inválida.")
        if self.entry_mfi_risk_scaling_enabled and (
            self.entry_mfi_len <= 0 or self.entry_mfi_risk_spread_scale <= 0
            or self.entry_mfi_risk_min <= 0
            or self.entry_mfi_risk_max < self.entry_mfi_risk_min
        ):
            raise ValueError("Configuración de escalado MFI de riesgo inválida.")
        if self.entry_er_risk_scaling_enabled and (
            self.entry_er_len <= 0 or self.entry_er_risk_slope < 0
            or self.entry_er_risk_min <= 0
            or self.entry_er_risk_max < self.entry_er_risk_min
        ):
            raise ValueError("Configuración de escalado Efficiency Ratio inválida.")
        if self.entry_dmi_risk_scaling_enabled and (
            self.entry_adx_len <= 0 or self.entry_dmi_risk_spread_scale <= 0
            or self.entry_dmi_risk_min <= 0
            or self.entry_dmi_risk_max < self.entry_dmi_risk_min
        ):
            raise ValueError("Configuración de escalado DMI de riesgo inválida.")
        if self.adx_exit_enabled:
            if min(self.adx_dilen, self.adx_adxlen) <= 0 or self.adx_key_level < 0:
                raise ValueError("Configuración de ADX de salida inválida.")
            if not 0 < self.adx_exit_pct <= 100:
                raise ValueError("adx_exit_pct debe estar en (0, 100].")
            if self.adx_exit_range < 1:
                raise ValueError("adx_exit_range debe ser >= 1.")
        if self.ema_weakening_exit_enabled:
            if self.ema_weakening_gap_pct <= 0 or not 0 < self.ema_weakening_exit_pct <= 100:
                raise ValueError("Configuración de weakening inválida.")
            if min(self.ema_weakening_fast, self.ema_weakening_slow) < 0:
                raise ValueError("ema_weakening_fast/ema_weakening_slow no pueden ser negativas.")
            if self.ema_weakening_slow > 0 and self.ema_weakening_fast > 0 \
                    and self.ema_weakening_fast >= self.ema_weakening_slow:
                raise ValueError("Se requiere ema_weakening_fast < ema_weakening_slow (o 0 para usar las del cruce).")
        if self.rsi_divergence_exit_enabled:
            if self.rsi_divergence_len <= 0:
                raise ValueError("rsi_divergence_len debe ser positivo.")
            if self.rsi_divergence_pivot < 1:
                raise ValueError("rsi_divergence_pivot debe ser >= 1.")
            if self.rsi_divergence_lookback < 1:
                raise ValueError("rsi_divergence_lookback debe ser >= 1.")
        if not 0 < self.candle_pattern_exit_pct <= 100:
            raise ValueError("candle_pattern_exit_pct debe estar en (0, 100].")
        if self.candle_pattern_exit_confirm_bars < 1:
            raise ValueError("candle_pattern_exit_confirm_bars debe ser >= 1.")
        reversal_loss_thresholds = (
            self.candle_pattern_exit_long_loss_pct,
            self.candle_pattern_exit_short_loss_pct,
            self.candle_pattern_exit_signed_loss_pct,
        )
        if min(reversal_loss_thresholds) < 0 or (
            self.candle_pattern_exit_loss_pct is not None
            and self.candle_pattern_exit_loss_pct < 0
        ):
            raise ValueError("Los umbrales de pérdida de salidas TA-Lib no pueden ser negativos.")
        if (self.ema55_touch_tolerance_pct < 0 or self.ema55_touch_breakout_buffer_pct < 0
                or self.ema55_touch_exit_loss_pct < 0
                or self.ema55_touch_short_exit_loss_pct < 0):
            raise ValueError("Tolerancias y pérdida mínima de EMA55 no pueden ser negativas.")
        if min(self.ema55_touch_lookback_bars, self.ema55_touch_min_touches,
               self.ema55_touch_consecutive) < 1:
            raise ValueError("La ventana y los conteos de toques EMA55 deben ser >= 1.")
        if self.partial_tp_enabled:
            if self.partial_tp_r <= 0 or not 0 < self.partial_tp_pct <= 100:
                raise ValueError("Configuración de take-profit parcial inválida.")
        if self.partial_tp2_r > 0 or self.partial_tp2_pct > 0:
            if self.partial_tp2_r <= 0 or not 0 < self.partial_tp2_pct <= 100:
                raise ValueError("Configuración de take-profit parcial 2 inválida.")
            if self.partial_tp2_r <= self.partial_tp_r:
                raise ValueError("partial_tp2_r debe ser mayor que partial_tp_r.")
        if self.close_on_ema_slow_touch:
            if self.close_on_ema_slow_touch_pct < 0:
                raise ValueError("close_on_ema_slow_touch_pct debe ser >= 0.")
            if not 0 < self.close_on_ema_slow_touch_exit_pct <= 100:
                raise ValueError("close_on_ema_slow_touch_exit_pct debe estar en (0, 100].")
        if self.entry_partial_enabled:
            if not (0 < self.entry_partial_pct < 100):
                raise ValueError("entry_partial_pct debe estar en (0, 100).")
            if self.entry_confirm_mode not in ("bars", "close_above_ema", "pullback_retest"):
                raise ValueError("entry_confirm_mode debe ser 'bars', 'close_above_ema' o 'pullback_retest'.")
            if self.entry_confirm_bars < 0:
                raise ValueError("entry_confirm_bars no puede ser negativo.")
            if self.entry_confirm_retest_tol_pct < 0:
                raise ValueError("entry_confirm_retest_tol_pct no puede ser negativo.")
            if self.entry_confirm_max_move_pct < 0:
                raise ValueError("entry_confirm_max_move_pct no puede ser negativo.")
        if self.entry_confirm_move_exit_enabled:
            if self.entry_confirm_move_exit_pct <= 0:
                raise ValueError("entry_confirm_move_exit_pct debe ser positivo.")
            if not 0 < self.entry_confirm_move_exit_portion_pct <= 100:
                raise ValueError("entry_confirm_move_exit_portion_pct debe estar en (0, 100].")
        if not (self.allow_long or self.allow_short):
            raise ValueError("Debe habilitarse LONG, SHORT o ambos.")
        if self.trend_filter not in ("off", "cross", "slow"):
            raise ValueError("trend_filter debe ser 'off', 'cross' o 'slow'.")
        if (self.trend_filter != "off" or self.streak_trend_only) and pd.Timedelta(self.trend_timeframe) <= pd.Timedelta(0):
            raise ValueError("trend_timeframe debe ser positivo.")
        if self.chop_filter:
            if pd.Timedelta(self.chop_timeframe) <= pd.Timedelta(0):
                raise ValueError("chop_timeframe debe ser positivo.")
            if self.chop_adx_min < 0:
                raise ValueError("chop_adx_min no puede ser negativo.")
        if self.monthly_loss_limit_pct < 0:
            raise ValueError("monthly_loss_limit_pct no puede ser negativo.")
        if self.monthly_loss_limit_days < 0:
            raise ValueError("monthly_loss_limit_days no puede ser negativo.")
        if self.monthly_trend_gate_pct < 0:
            raise ValueError("monthly_trend_gate_pct no puede ser negativo.")
        if self.monthly_trend_gate_pct > 0 and self.trend_filter == "off" and not self.streak_trend_only:
            raise ValueError("monthly_trend_gate_pct requiere trend_filter != 'off' o streak_trend_only "
                             "(sin sesgo de tendencia no hay nada que exigir a la entrada).")
        if self.monthly_gate_mode not in ("trend", "all"):
            raise ValueError("monthly_gate_mode debe ser 'trend' o 'all'.")
        if not 0.0 <= self.monthly_risk_scaling_min <= 1.0:
            raise ValueError("monthly_risk_scaling_min debe estar en [0, 1].")
        if self.consecutive_loss_pause < 0 or self.consecutive_loss_pause_bars < 1:
            raise ValueError("consecutive_loss_pause debe ser >= 0 y ..._bars >= 1.")
        if self.consecutive_loss_skip_signals < 0:
            raise ValueError("consecutive_loss_skip_signals no puede ser negativo.")
        if self.adaptive_risk_window < 0:
            raise ValueError("adaptive_risk_window no puede ser negativo.")
        if self.adaptive_risk_window and not (0 < self.adaptive_risk_min <= self.adaptive_risk_max):
            raise ValueError("Se requiere 0 < adaptive_risk_min <= adaptive_risk_max.")
