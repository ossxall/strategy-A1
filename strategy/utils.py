from __future__ import annotations
from pathlib import Path
from typing import Optional
from strategy.config import Config
import time
import numpy as np
import pandas as pd


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normaliza aliases OHLCV y devuelve un DataFrame indexado por timestamp UTC."""
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
    missing = [c for c in ("timestamp", "open", "high", "low", "close") if c not in rename.values()]
    if missing:
        raise ValueError(f"Faltan columnas {missing}; disponibles: {list(df.columns)}")
    out = df.rename(columns=rename).copy()
    if "volume" not in out:
        out["volume"] = 0.0

    ts = out["timestamp"]
    if pd.api.types.is_numeric_dtype(ts):
        nums = pd.to_numeric(ts, errors="coerce")
        finite = nums.dropna()
        magnitude = float(finite.abs().median()) if len(finite) else 0
        unit = "ns" if magnitude >= 1e17 else "us" if magnitude >= 1e14 else "ms" if magnitude >= 1e11 else "s" if magnitude >= 1e9 else None
        ts = pd.to_datetime(nums, unit=unit, utc=True, errors="coerce") if unit else pd.to_datetime(ts, utc=True, errors="coerce")
    else:
        ts = pd.to_datetime(ts, utc=True, errors="coerce")
    if ts.isna().any():
        raise ValueError("Hay timestamps inválidos.")
    out["timestamp"] = ts
    for c in ("open", "high", "low", "close", "volume"):
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out = out.dropna(subset=["timestamp", "open", "high", "low", "close"])
    out = out.sort_values("timestamp").drop_duplicates("timestamp", keep="last").set_index("timestamp")
    return out[["open", "high", "low", "close", "volume"]]


def load_parquet(path: str | Path) -> pd.DataFrame:
    start = time.perf_counter()
    print(f"[LOAD] {path}", flush=True)
    data = normalize_columns(pd.read_parquet(path))
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
