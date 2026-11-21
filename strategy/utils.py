from __future__ import annotations
from pathlib import Path
from typing import Optional
from strategy.config import Config
import time
import numpy as np
import pandas as pd

from strategy.ohlcv import OHLCV_COLUMNS, normalize_ohlcv_values

TIMESTAMP_UNITS = {"s", "ms", "us", "ns"}


def _parse_timestamp(values: pd.Series, unit: str | None) -> pd.Series:
    """Convierte timestamps a UTC sin adivinar unidades de epochs numéricos."""
    if unit is not None and unit not in TIMESTAMP_UNITS:
        raise ValueError(
            f"timestamp_unit debe ser uno de {sorted(TIMESTAMP_UNITS)}."
        )

    if pd.api.types.is_datetime64_any_dtype(values.dtype):
        if unit is not None:
            raise ValueError(
                "timestamp_unit no se aplica a una columna datetime."
            )
        parsed = pd.to_datetime(values, utc=True, errors="coerce")
    else:
        numeric = pd.to_numeric(values, errors="coerce")
        supplied = values.notna()
        numeric_rows = numeric.notna()
        has_numeric = bool((supplied & numeric_rows).any())
        has_non_numeric = bool((supplied & ~numeric_rows).any())

        if has_numeric and has_non_numeric:
            raise ValueError(
                "La columna timestamp mezcla epochs numéricos y fechas de texto."
            )
        if has_numeric:
            if unit is None:
                raise ValueError(
                    "timestamp_unit es obligatorio para timestamps numéricos "
                    "(s, ms, us o ns)."
                )
            parsed = pd.to_datetime(
                numeric, unit=unit, utc=True, errors="coerce"
            )
        else:
            if unit is not None:
                raise ValueError(
                    "timestamp_unit solo se permite para timestamps numéricos."
                )
            parsed = pd.to_datetime(values, utc=True, errors="coerce")

    if parsed.isna().any():
        raise ValueError("Hay timestamps inválidos.")
    return parsed


def normalize_ohlcv(
    frame: pd.DataFrame,
    *,
    timestamp_unit: str | None = None,
) -> pd.DataFrame:
    """Devuelve el contrato OHLCV común: índice UTC por apertura y float64.

    Los epochs enteros o numéricos en texto requieren unidad explícita. Las
    fechas ISO y las columnas datetime se convierten directamente a UTC.
    """
    required = ("timestamp", *OHLCV_COLUMNS)
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(
            f"Faltan columnas canónicas {missing}; disponibles: {list(frame.columns)}"
        )
    if frame.columns.duplicated().any():
        raise ValueError("El DataFrame contiene nombres de columna duplicados.")

    timestamps = _parse_timestamp(frame["timestamp"], timestamp_unit)
    numeric_columns: list[np.ndarray] = []
    for column in OHLCV_COLUMNS:
        values = pd.to_numeric(frame[column], errors="coerce")
        try:
            array = values.to_numpy(dtype=np.float64, na_value=np.nan)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{column} contiene valores no numéricos.") from exc
        numeric_columns.append(array)
    normalized_values = normalize_ohlcv_values(*numeric_columns)
    numeric = dict(zip(OHLCV_COLUMNS, normalized_values))

    try:
        # Binance open times have millisecond precision. Fixing the index unit
        # also makes Parquet datetimes and SQLite epochs byte-for-byte
        # comparable at the pandas dtype level.
        index = pd.DatetimeIndex(timestamps).as_unit("ms", round_ok=False)
    except (OverflowError, ValueError) as exc:
        raise ValueError(
            "timestamp debe tener precisión de milisegundos y poder "
            "representarse en ese formato."
        ) from exc
    out = pd.DataFrame(numeric, index=index)
    out.index.name = "timestamp"
    out = out.sort_index(kind="stable")
    # Coincide con la política histórica y con FeedReader: en timestamps
    # repetidos, prevalece la última fila recibida.
    out = out.loc[~out.index.duplicated(keep="last")]
    return out.loc[:, OHLCV_COLUMNS].astype("float64")


def normalize_columns(
    df: pd.DataFrame,
    *,
    timestamp_unit: str | None = None,
) -> pd.DataFrame:
    """Normaliza aliases OHLCV al contrato UTC común.

    Si el timestamp es un epoch numérico, ``timestamp_unit`` es obligatorio.
    """
    df = df.rename(columns={c: str(c).strip().lower() for c in df.columns})
    aliases = {
        "timestamp": ("timestamp", "open_time", "datetime", "date", "time", "ts"),
        "open": ("open", "o"), "high": ("high", "h"), "low": ("low", "l"),
        "close": ("close", "c"), "volume": ("volume", "vol", "v"),
    }
    rename = {}
    for canonical, choices in aliases.items():
        found = next((c for c in choices if c in df.columns), None)
        if found is not None:
            rename[found] = canonical
    missing = [c for c in ("timestamp", *OHLCV_COLUMNS) if c not in rename.values()]
    if missing:
        raise ValueError(f"Faltan columnas {missing}; disponibles: {list(df.columns)}")
    out = df.rename(columns=rename).copy()
    return normalize_ohlcv(out, timestamp_unit=timestamp_unit)


def load_parquet(
    path: str | Path,
    *,
    timestamp_unit: str | None = None,
) -> pd.DataFrame:
    start = time.perf_counter()
    print(f"[LOAD] {path}", flush=True)
    data = normalize_columns(
        pd.read_parquet(path),
        timestamp_unit=timestamp_unit,
    )
    print(f"[LOAD] {len(data):,} velas en {time.perf_counter()-start:.2f}s | {data.index.min()} -> {data.index.max()}", flush=True)
    return data


def source_minutes(data: pd.DataFrame) -> float:
    delta = data.index.to_series().diff().dropna().dt.total_seconds() / 60
    value = float(delta.median()) if len(delta) else 1.0
    return value if np.isfinite(value) and value > 0 else 1.0


def validate_raw_1m(data: pd.DataFrame, source: str = "raw dataset") -> None:
    """Reject raw strategy inputs whose inferred candle interval is not 1m."""
    if data.empty:
        return
    if not isinstance(data.index, pd.DatetimeIndex):
        raise ValueError(f"{source} debe tener un DatetimeIndex de velas de 1m.")
    if not data.index.is_monotonic_increasing:
        raise ValueError(
            f"{source} debe tener timestamps en orden cronológico ascendente."
        )
    if data.index.has_duplicates:
        raise ValueError(f"{source} no debe contener timestamps duplicados.")
    interval = pd.Timedelta(minutes=source_minutes(data))
    if interval != pd.Timedelta("1min"):
        raise ValueError(
            f"{source} debe tener resolución de 1m; "
            f"resolución detectada: {interval}."
        )


def resample_ohlcv(data: pd.DataFrame, rule: str, source_interval_minutes: Optional[float] = None) -> pd.DataFrame:
    """Resamplea OHLCV y descarta buckets con menos barras que las esperadas."""
    if data.empty:
        return data.copy()
    src = source_interval_minutes or source_minutes(data)
    td = pd.Timedelta(rule)
    target_minutes = td.total_seconds() / 60
    if target_minutes < src:
        raise ValueError(f"No se puede remuestrear de {src:g}m a un timeframe menor ({rule}).")
    ratio = target_minutes / src
    expected = int(round(ratio))
    if expected < 1 or not np.isclose(ratio, expected, rtol=1e-9, atol=1e-9):
        raise ValueError(
            f"El timeframe {rule} debe ser múltiplo entero de la resolución "
            f"de origen ({src:g}m); proporción actual: {ratio:g}."
        )

    # Source candles are timestamped by their open. Reject any candle whose
    # duration would cross the target bucket boundary: otherwise its OHLC could
    # include prices from after that target candle closed (lookahead).
    source_ns = int(round(src * 60_000_000_000))
    index_ns = data.index.as_unit("ns").asi8
    day_start_ns = data.index.normalize().as_unit("ns").asi8
    bucket_offset_ns = (index_ns - day_start_ns) % td.value
    if np.any(bucket_offset_ns + source_ns > td.value):
        raise ValueError(
            f"Las velas de origen de {src:g}m no están alineadas con los "
            f"buckets de {rule}; alguna vela cruza el cierre del bucket."
        )

    grouped = data.resample(rule, label="left", closed="left", origin="start_day")
    out = grouped.agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    counts = grouped["close"].count()
    out = out[counts.reindex(out.index, fill_value=0) >= expected]
    return out.dropna(subset=["open", "high", "low", "close"])


def to_timeframe(data: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    validate_raw_1m(data)
    if data.empty:
        return data.copy()
    if pd.Timedelta(cfg.timeframe) < pd.Timedelta("1min"):
        raise ValueError("El timeframe de estrategia no puede ser menor que 1m.")
    if pd.Timedelta(cfg.timeframe) == pd.Timedelta("1min"):
        return data.copy()
    out = resample_ohlcv(data, cfg.timeframe)
    print(f"[RESAMPLE] {source_minutes(data):g}m -> {cfg.timeframe}: {len(out):,} velas", flush=True)
    return out
