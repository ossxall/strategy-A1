from __future__ import annotations
from dataclasses import dataclass
import pandas as pd

@dataclass
class Position:
    position_id: int
    side: int
    quantity: float
    initial_quantity: float
    entry_price: float
    entry_time: pd.Timestamp
    entry_fee: float
    stop_pct: float
    risk_amount: float
    breakeven: bool = False
    partial_done: bool = False
    partial2_done: bool = False
    touch_done: bool = False
    # --- Entrada parcial + confirmación (scale-in) --------------------------
    # target_quantity = 0 -> modo clásico (entrada completa)
    target_quantity: float = 0.0        # Cantidad total objetivo planeada
    partial_entry_done: bool = False    # True si el scale-in está completo
    entry_partial_pct: float = 100.0    # % del objetivo cubierto en 1ª entrada
    entry_bar_index: int = -1           # Índice de barra donde se hizo la 1ª entrada
    last_add_price: float = 0.0         # Precio del último add
    add_count: int = 0                  # Número de adds realizados
    confirm_exit_done: bool = False     # Salida parcial por movimiento adverso desde la confirmación
    candle_exit_done: bool = False      # Una salida TA-Lib por posición (total o parcial)


@dataclass
class Trade:
    trade_id: int
    position_id: int
    side: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    quantity: float
    pnl_gross: float
    fees: float
    pnl_net: float
    return_pct: float
    reason: str
    position_risk: float


TRADE_COLUMNS = list(Trade.__dataclass_fields__)
CROSS_REASON = "EMA_CROSS_EXIT"
#: Motivo de salida al superar el límite de pérdida mensual.
MONTHLY_LIMIT_REASON = "MONTHLY_LOSS_LIMIT"
#: Salida total por divergencia RSI en contra de la posición: la bajista cierra
#: un LONG (`RSI_DIVERGENCE_BEAR`) y la alcista cierra un SHORT.
RSI_DIVERGENCE_BULL_REASON = "RSI_DIVERGENCE_BULL"
RSI_DIVERGENCE_BEAR_REASON = "RSI_DIVERGENCE_BEAR"
DEFAULT_OUTPUT_DIR = "backtest_out"


def empty_trades() -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in TRADE_COLUMNS})
