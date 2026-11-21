"""Contrato común y validación de valores OHLCV."""

from __future__ import annotations

import numpy as np

OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


def normalize_ohlcv_values(
    open_values,
    high_values,
    low_values,
    close_values,
    volume_values,
) -> tuple:
    """Convierte y valida OHLCV escalar o vectorizado en float64.

    Los cinco campos deben tener la misma forma. Devuelve escalares Python
    para entradas escalares y arrays NumPy float64 para entradas vectorizadas.
    """
    values = []
    for name, value in zip(
        OHLCV_COLUMNS,
        (open_values, high_values, low_values, close_values, volume_values),
    ):
        try:
            array = np.asarray(value, dtype=np.float64)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"{name} contiene valores no numéricos.") from exc
        values.append(array)

    shape = values[0].shape
    if any(array.shape != shape for array in values[1:]):
        raise ValueError("Los cinco campos OHLCV deben tener la misma forma.")
    if any(not np.isfinite(array).all() for array in values):
        raise ValueError("OHLCV contiene valores nulos o no finitos.")

    op, hi, lo, cl, vol = values
    if ((op <= 0) | (hi <= 0) | (lo <= 0) | (cl <= 0)).any():
        raise ValueError("OHLC contiene precios no positivos.")
    invalid_ranges = (
        (hi < op)
        | (hi < cl)
        | (hi < lo)
        | (lo > op)
        | (lo > cl)
    )
    if invalid_ranges.any():
        raise ValueError("Rango OHLC inválido: high/low no contiene open/close.")
    if (vol < 0).any():
        raise ValueError("volume no puede ser negativo.")

    if shape == ():
        return tuple(float(array) for array in values)
    return tuple(array.astype(np.float64, copy=False) for array in values)
