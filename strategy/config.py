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
    # Periodos del MACD de entrada: la señal usa la línea principal respecto a cero.
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    # None = usar el timeframe principal de la estrategia.
    macd_timeframe: str | None = "1h"
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
    # Bloquea cruces EMA si el ADX de entrada no supera el mínimo.
    entry_adx_enabled: bool = True
    entry_adx_len: int = 14
    entry_adx_min: float = 20.0
    # Todos los timeframes deben superar el umbral para permitir una entrada.
    entry_adx_timeframe: str | list[str] | tuple[str, ...] = ("30min")
    # Bloquea entradas cuando ATR(14) es anormalmente bajo/alto frente a su
    # media de las velas anteriores. No afecta las salidas de posiciones abiertas.
    entry_atr_enabled: bool = True
    entry_atr_len: int = 14
    entry_atr_lookback: int = 50
    entry_atr_min_ratio: float = 0.5
    entry_atr_max_ratio: float = 2.5
    # Bloquea entradas LONG tras divergencia alcista y SHORT tras bajista,
    # dentro de las últimas N velas del timeframe de estrategia.
    entry_rsi_divergence_enabled: bool = True
    entry_rsi_divergence_lookback: int = 20
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
    rsi_divergence_exit_pct: float = 100.0  # % de la posición que se cierra
    # Uno o varios timeframes (combinados con OR), como en las otras salidas.
    rsi_divergence_timeframe: str | list[str] | tuple[str, ...] = "1D"
    rsi_divergence_len: int = 14       # Periodo del RSI (Wilder)
    rsi_divergence_pivot: int = 5      # Velas a cada lado para confirmar un pivote
    rsi_divergence_lookback: int = 60  # Ventana hacia atrás para el pivote previo

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

    # --- Entrada parcial -----------------------------------------------------
    # Abre `entry_partial_pct` % en la apertura de la vela posterior a la señal
    # y añade el resto al cierre de esa misma vela. Activado por defecto (50/50).
    entry_partial_enabled: bool = True
    entry_partial_pct: float = 50.0        # % inicial (0 < pct < 100)
    # Sesgo de tendencia de timeframe superior, usado sólo después de una pérdida.
    trend_timeframe: str = "1D"

    # --- Protección mensual ------------------------------------------------
    # Si la equity cae más de `monthly_loss_limit_pct` % por debajo de la
    # equity al inicio del mes calendario, se dejan de abrir posiciones (y, con
    # monthly_loss_limit_close, se cierra la abierta) hasta el siguiente mes
    # o, si `monthly_loss_limit_days` > 0, durante ese número de días.
    # Red de seguridad para el peor mes. Con los defaults apenas llega a
    # dispararse (1 vez en 82 meses), por eso arranca holgado en 5.0. El tamaño
    # adaptativo también recorta meses rojos; bajar el límite (p. ej. 2) mejora
    # el peor mes pero recorta el acumulado de meses negativos. 0 = desactivado.
    monthly_loss_limit_pct: float = 5.0
    monthly_loss_limit_close: bool = True
    monthly_loss_limit_days: int = 0

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
    def entry_adx_timeframes(self) -> tuple[str, ...]:
        """Timeframes ADX de entrada normalizados; todos deben pasar el umbral."""
        return timeframe_tuple(self.entry_adx_timeframe)

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

    def __post_init__(self):
        if min(self.ema_fast, self.ema_slow) <= 0 or self.ema_fast >= self.ema_slow:
            raise ValueError("Se requiere 0 < ema_fast < ema_slow.")
        if min(self.macd_fast, self.macd_slow, self.macd_signal) <= 0 or self.macd_fast >= self.macd_slow:
            raise ValueError("Se requiere 0 < macd_fast < macd_slow y macd_signal > 0.")
        if self.macd_timeframe is not None and pd.Timedelta(self.macd_timeframe) <= pd.Timedelta(0):
            raise ValueError("macd_timeframe debe ser positivo o None para usar timeframe.")
        if pd.Timedelta(self.timeframe) <= pd.Timedelta(0):
            raise ValueError("timeframe debe ser positivo.")
        for timeframe in self.adx_timeframes:
            if pd.Timedelta(timeframe) <= pd.Timedelta(0):
                raise ValueError(f"adx_timeframe inválido: {timeframe!r}.")
        if self.adx_exit_enabled and not self.adx_timeframes:
            raise ValueError("adx_timeframe no puede estar vacío.")
        for timeframe in self.entry_adx_timeframes:
            if pd.Timedelta(timeframe) <= pd.Timedelta(0):
                raise ValueError(f"entry_adx_timeframe inválido: {timeframe!r}.")
        if self.entry_adx_enabled and not self.entry_adx_timeframes:
            raise ValueError("entry_adx_timeframe no puede estar vacío.")
        if self.ema_weakening_exit_enabled and not self.ema_weakening_timeframes:
            raise ValueError("ema_weakening_timeframe no puede estar vacío.")
        for timeframe in self.ema_weakening_timeframes:
            if pd.Timedelta(timeframe) <= pd.Timedelta(0):
                raise ValueError(f"ema_weakening_timeframe inválido: {timeframe!r}.")
        if (self.rsi_divergence_exit_enabled or self.entry_rsi_divergence_enabled) \
                and not self.rsi_divergence_timeframes:
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
        if self.entry_atr_enabled and (
            self.entry_atr_len <= 0 or self.entry_atr_lookback <= 0
            or self.entry_atr_min_ratio < 0
            or self.entry_atr_max_ratio <= self.entry_atr_min_ratio
        ):
            raise ValueError("Configuración del filtro ATR de entrada inválida.")
        if self.entry_rsi_divergence_enabled and self.entry_rsi_divergence_lookback < 1:
            raise ValueError("entry_rsi_divergence_lookback debe ser >= 1.")
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
        if self.rsi_divergence_exit_enabled or self.entry_rsi_divergence_enabled:
            if self.rsi_divergence_len <= 0:
                raise ValueError("rsi_divergence_len debe ser positivo.")
            if self.rsi_divergence_pivot < 1:
                raise ValueError("rsi_divergence_pivot debe ser >= 1.")
            if self.rsi_divergence_lookback < 1:
                raise ValueError("rsi_divergence_lookback debe ser >= 1.")
        if self.rsi_divergence_exit_enabled and not 0 < self.rsi_divergence_exit_pct <= 100:
            raise ValueError("rsi_divergence_exit_pct debe estar en (0, 100].")
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
        if self.entry_partial_enabled:
            if not (0 < self.entry_partial_pct < 100):
                raise ValueError("entry_partial_pct debe estar en (0, 100).")
        if not (self.allow_long or self.allow_short):
            raise ValueError("Debe habilitarse LONG, SHORT o ambos.")
        if self.streak_trend_only and pd.Timedelta(self.trend_timeframe) <= pd.Timedelta(0):
            raise ValueError("trend_timeframe debe ser positivo.")
        if self.monthly_loss_limit_pct < 0:
            raise ValueError("monthly_loss_limit_pct no puede ser negativo.")
        if self.monthly_loss_limit_days < 0:
            raise ValueError("monthly_loss_limit_days no puede ser negativo.")
        if self.adaptive_risk_window < 0:
            raise ValueError("adaptive_risk_window no puede ser negativo.")
        if self.adaptive_risk_window and not (0 < self.adaptive_risk_min <= self.adaptive_risk_max):
            raise ValueError("Se requiere 0 < adaptive_risk_min <= adaptive_risk_max.")
