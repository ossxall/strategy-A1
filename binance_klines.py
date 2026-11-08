#!/usr/bin/env python3
"""Descarga masiva de klines (OHLCV) de Binance Vision.

Mercados: spot, um (USDⓈ-M / USDT), cm (COIN-M / inversos).
Endpoints: https://data.binance.vision/?prefix=data/futures/um/monthly/klines/<SYM>/<interval>/

MÉTODO RECOMENDADO: los archivos mensuales (un .zip por mes, ~2 MB para 1m de BTCUSDT).
  - Son inmutables: se pueden volver a bajar cuando quieras sin perder nada.
  - Descarga reanudable por HTTP Range e idempotente: si el archivo ya existe, se salta.
  - SHA-256 verificado contra el .CHECKSUM oficial antes de darlo por bueno.
  - Un rango de meses completos nunca deja huecos (no depende de paginación ni de red estable).

--iterations N  -> N archivos mensuales = los últimos N meses disponibles.
--years N       -> atajo a --iterations N*12 (los últimos N años de datos completos).
--from/--to     -> rango explícito de meses (YYYY-MM o YYYY-MM-DD).
0 o negativo    -> todo el histórico disponible.

Ejemplos:
  python3 binance_klines.py --years 5
  python3 binance_klines.py --iterations 60
  python3 binance_klines.py --from 2020-01 --to 2021-12 --merge-format parquet
  python3 binance_klines.py --symbol ETHUSDT --interval 1h --market spot --years 3
"""

from __future__ import annotations

import argparse
import calendar
import concurrent.futures as futures
import hashlib
import os
import re
import shutil
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

try:
    import requests
except ImportError:
    sys.exit("Falta 'requests'. Instálalo con: pip install requests")

CDN = "https://data.binance.vision/"
S3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
CHUNK = 1 << 20

COLUMNS = [
    "open_time", "open", "high", "low", "close", "volume", "close_time",
    "quote_volume", "count", "taker_buy_volume", "taker_buy_quote_volume", "ignore",
]
FLOAT_COLS = {"open", "high", "low", "close", "volume", "quote_volume",
              "taker_buy_volume", "taker_buy_quote_volume"}
INT_COLS = {"open_time", "close_time", "count", "ignore"}

MARKETS = {"um": "data/futures/um", "cm": "data/futures/cm", "spot": "data/spot"}
INTERVALS = {"1s", "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M"}
MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")

_tls = threading.local()
_print_lock = threading.Lock()


def session() -> requests.Session:
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.headers["User-Agent"] = "binance-klines/1.0 (+https://data.binance.vision)"
        _tls.s = s
    return s


def say(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


# --------------------------------------------------------------------------- listing

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(node: ET.Element, name: str) -> str | None:
    for el in node.iter():
        if _local(el.tag) == name:
            return (el.text or "").strip()
    return None


@dataclass
class Obj:
    key: str
    size: int


def list_prefix(prefix: str) -> Iterator[Obj]:
    token: str | None = None
    while True:
        params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        r = session().get(S3, params=params, timeout=(10, 60))
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for node in root.iter():
            if _local(node.tag) != "Contents":
                continue
            key = _child(node, "Key")
            size = _child(node, "Size")
            if key and size:
                yield Obj(key, int(size))
        if _child(root, "IsTruncated") != "true":
            return
        token = _child(root, "NextContinuationToken")
        if not token:
            return


def available_months(prefix: str, symbol: str, interval: str) -> list[Obj]:
    pat = re.compile(rf"^{re.escape(symbol)}-{re.escape(interval)}-(\d{{4}})-(\d{{2}})\.zip$")
    found: dict[tuple[int, int], Obj] = {}
    for obj in list_prefix(prefix):
        name = obj.key.rsplit("/", 1)[-1]
        m = pat.match(name)
        if m:
            found[(int(m.group(1)), int(m.group(2)))] = obj
    return [found[k] for k in sorted(found)]


# --------------------------------------------------------------------------- selección

def parse_month(value: str) -> tuple[int, int]:
    m = re.match(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?$", value.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"Fecha inválida '{value}' (usa YYYY-MM o YYYY-MM-DD)")
    year, month = int(m.group(1)), int(m.group(2) or 1)
    if not 1 <= month <= 12:
        raise argparse.ArgumentTypeError(f"Mes inválido en '{value}'")
    return year, month


def label(ym: tuple[int, int]) -> str:
    return f"{ym[0]:04d}-{ym[1]:02d}"


def ym_of(obj: Obj, symbol: str, interval: str) -> tuple[int, int]:
    stem = obj.key.rsplit("/", 1)[-1]
    m = MONTH_RE.match(stem[len(symbol) + len(interval) + 2:-4])
    return int(m.group(1)), int(m.group(2))


def select_months(objects: Sequence[Obj], args) -> list[Obj]:
    if not objects:
        return []
    if args.frm or args.to:
        lo = parse_month(args.frm) if args.frm else None
        hi = parse_month(args.to) if args.to else None
        picked = [o for o in objects
                  if (lo is None or ym_of(o, args.symbol, args.interval) >= lo)
                  and (hi is None or ym_of(o, args.symbol, args.interval) <= hi)]
        if lo and lo < ym_of(objects[0], args.symbol, args.interval):
            say(f"Aviso: {args.frm} es anterior al primer mes disponible.")
        if hi and hi > ym_of(objects[-1], args.symbol, args.interval):
            say(f"Aviso: {args.to} es posterior al último mes disponible.")
        return picked

    if args.iterations is not None:
        count = args.iterations
    elif args.years is not None:
        count = int(args.years * 12)
    else:
        count = 0
    if count <= 0:
        return list(objects)
    if count < len(objects):
        say(f"Histórico completo: {len(objects)} meses. Descargando los últimos {count}.")
    return list(objects[-count:])


# --------------------------------------------------------------------------- descarga

def fetch_checksum(key: str, retries: int) -> str | None:
    url = CDN + key + ".CHECKSUM"
    for attempt in range(retries):
        try:
            r = session().get(url, timeout=(10, 30))
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.text.split()[0].strip().lower()
        except (requests.RequestException, IndexError):
            time.sleep(min(2 ** attempt, 8) * 0.5)
    return None


def download(url: str, dest: Path, size: int, retries: int) -> tuple[bool, str]:
    part = dest.with_name(dest.name + ".part")
    for attempt in range(retries + 1):
        have = part.stat().st_size if part.exists() else 0
        if size and have == size:
            break
        if size and have > size:
            part.unlink()
            have = 0
        try:
            headers = {"Range": f"bytes={have}-"} if have else {}
            with session().get(url, headers=headers, stream=True, timeout=(15, 120)) as r:
                if have and r.status_code == 200:
                    have = 0
                r.raise_for_status()
                with open(part, "ab" if have else "wb") as fh:
                    for chunk in r.iter_content(CHUNK):
                        fh.write(chunk)
            if not size or part.stat().st_size == size:
                break
        except (requests.RequestException, OSError) as exc:
            if attempt == retries:
                return False, str(exc)
            time.sleep(min(2 ** attempt, 30) + 0.3 * attempt)
    else:
        return False, "no se pudo completar la descarga"
    os.replace(part, dest)
    return True, ""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


# --------------------------------------------------------------------------- csv

def has_header(path: Path) -> bool:
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.readline().lstrip('"').startswith("open_time")


def extract(zip_path: Path, csv_dir: Path, force: bool = False) -> Path:
    target = csv_dir / f"{zip_path.stem}.csv"
    if target.exists() and target.stat().st_size > 0 and not force:
        return target
    csv_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not names:
            raise ValueError(f"{zip_path.name}: el zip no contiene CSV")
        tmp = target.with_name(target.name + ".part")
        with zf.open(names[0]) as src, open(tmp, "wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK)
        os.replace(tmp, target)
    return target


def merge_csv(csvs: Sequence[Path], out: Path, add_timestamps: bool) -> int:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    rows = 0
    with open(tmp, "w", newline="", encoding="utf-8") as dst:
        dst.write(("timestamp," if add_timestamps else "") + ",".join(COLUMNS) + "\n")
        for path in csvs:
            with open(path, "r", encoding="utf-8", errors="replace") as src:
                header = has_header(path)
                for j, line in enumerate(src):
                    if header and j == 0:
                        continue
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    if add_timestamps:
                        line = f"{ts_to_iso(int(line.split(',', 1)[0]))},{line}"
                    dst.write(line + "\n")
                    rows += 1
    os.replace(tmp, out)
    return rows


def merge_parquet(csvs: Sequence[Path], out: Path) -> int:
    try:
        import pyarrow as pa
        import pyarrow.csv as pacsv
        import pyarrow.parquet as pq
    except ImportError:
        raise SystemExit("Parquet requiere 'pyarrow'. Instálalo con: pip install pyarrow")

    fields: list = []
    for col in COLUMNS:
        if col in ("open_time", "close_time"):
            fields.append(pa.field(col, pa.timestamp("ms")))
        elif col in INT_COLS:
            fields.append(pa.field(col, pa.int64()))
        else:
            fields.append(pa.field(col, pa.float64()))
    schema = pa.schema(fields)
    read_types = {c: ("int64" if c in INT_COLS else "float64") for c in COLUMNS}
    conv = pacsv.ConvertOptions(column_types=read_types)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    rows = 0
    writer = pq.ParquetWriter(tmp, schema)
    try:
        for path in csvs:
            header = has_header(path)
            read = pacsv.ReadOptions(autogenerate_column_names=False,
                                     column_names=COLUMNS,
                                     skip_rows=1 if header else 0)
            table = pacsv.read_csv(path, read_options=read, convert_options=conv)
            table = table.rename_columns(COLUMNS).cast(schema)
            writer.write_table(table, row_group_size=250_000)
            rows += table.num_rows
    finally:
        writer.close()
    os.replace(tmp, out)
    return rows


def ts_to_iso(ms: int) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# --------------------------------------------------------------------------- integridad

def expected_rows(interval: str, year: int, month: int) -> int | None:
    per_day = {"1s": 86400, "1m": 1440, "3m": 480, "5m": 288, "15m": 96, "30m": 48,
               "1h": 24, "2h": 12, "4h": 6, "6h": 4, "8h": 3, "12h": 2}
    if interval not in per_day:
        return None
    return calendar.monthrange(year, month)[1] * per_day[interval]


def count_rows(path: Path) -> int:
    n = 0
    with open(path, "rb") as fh:
        for line in fh:
            n += 1
    return n - 1 if has_header(path) else n


def audit(base: Path, symbol: str, interval: str) -> list[str]:
    csv_dir = base / "csv"
    problems: list[str] = []
    for path in sorted(csv_dir.glob(f"{symbol}-{interval}-*.csv")):
        m = MONTH_RE.match(path.stem[len(symbol) + len(interval) + 2:])
        if not m:
            continue
        year, month = int(m.group(1)), int(m.group(2))
        want = expected_rows(interval, year, month)
        if want is None:
            continue
        got = count_rows(path)
        if got == want:
            continue
        zip_path = base / "monthly" / f"{path.stem}.zip"
        if zip_path.exists():
            try:
                extract(zip_path, csv_dir, force=True)
                again = count_rows(path)
            except (ValueError, zipfile.BadZipFile):
                again = got
            if again == want:
                problems.append(f"{year:04d}-{month:02d}: CSV regenerado desde el zip "
                                f"({got:,} -> {again:,} velas)")
                continue
            got = again
        problems.append(f"{year:04d}-{month:02d}: {got:,} velas, se esperaban {want:,} "
                        f"({'truncado' if got < want else 'sobran filas'})")
    return problems


# --------------------------------------------------------------------------- pipeline

def month_of(obj: Obj, symbol: str, interval: str) -> str:
    m = MONTH_RE.match(obj.key.rsplit("/", 1)[-1][len(symbol) + len(interval) + 2:-4])
    return label((int(m.group(1)), int(m.group(2))))


def _verified(args, zip_path: Path, obj: Obj) -> bool:
    if not args.verify:
        return True
    expected = fetch_checksum(obj.key, args.retries)
    if not expected:
        return True
    if sha256(zip_path) == expected:
        return True
    zip_path.unlink(missing_ok=True)
    return False


def process(obj: Obj, args, base: Path, index: int, total: int) -> tuple[bool, Path | None, str]:
    name = obj.key.rsplit("/", 1)[-1]
    month = month_of(obj, args.symbol, args.interval)
    zip_path = base / "monthly" / name
    t0 = time.time()

    if zip_path.exists() and (not obj.size or zip_path.stat().st_size == obj.size):
        if args.redownload:
            zip_path.unlink()
        elif not args.recheck or _verified(args, zip_path, obj):
            return _after(args, base, zip_path, month, index, total, t0, skipped=True)
        else:
            say(f"[{index:>4}/{total}] {month}  SHA-256 incorrecto, redescargando")
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    ok, err = download(CDN + obj.key, zip_path, obj.size, args.retries)
    if not ok:
        return False, None, f"{month}: {err}"
    if args.verify and not _verified(args, zip_path, obj):
        ok, err = download(CDN + obj.key, zip_path, obj.size, args.retries)
        if not ok:
            return False, None, f"{month}: {err}"
        if not _verified(args, zip_path, obj):
            zip_path.unlink(missing_ok=True)
            return False, None, f"{month}: SHA-256 no coincide tras reintento"
    return _after(args, base, zip_path, month, index, total, t0)


def _after(args, base: Path, zip_path: Path, month: str, index: int, total: int,
           t0: float, skipped: bool = False) -> tuple[bool, Path | None, str]:
    csv_path: Path | None = None
    if args.keep_csv:
        try:
            csv_path = extract(zip_path, base / "csv")
        except (ValueError, zipfile.BadZipFile) as exc:
            return False, None, f"{month}: {exc}"
    size = zip_path.stat().st_size
    status = "skip" if skipped else "ok"
    extra = f" -> {csv_path.name}" if csv_path else ""
    say(f"[{index:>4}/{total}] {month}  {status:<4} {human(size):>9}  {time.time() - t0:5.1f}s{extra}")
    return True, csv_path, ""


def main() -> int:
    p = argparse.ArgumentParser(
        prog="binance_klines.py",
        description="Descarga años de klines (OHLCV) de Binance Vision, mes a mes y con verificación SHA-256.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Recomendado: --years N (últimos N*12 meses completos).\n"
               "Alternativas: --iterations N (N meses) o --from/--to para un rango exacto.",
    )
    p.add_argument("--symbol", default="BTCUSDT", help="par (default: BTCUSDT)")
    p.add_argument("--interval", default="1m", help=f"intervalo, uno de: {', '.join(sorted(INTERVALS))} (default: 1m)")
    p.add_argument("--market", default="um", choices=sorted(MARKETS), help="um=USDT-M, cm=COIN-M, spot (default: um)")
    sel = p.add_argument_group("selección de período")
    sel.add_argument("--years", type=float, help="últimos N años de meses completos (N*12 archivos)")
    sel.add_argument("--iterations", type=int, help="últimos N archivos mensuales (= N meses)")
    sel.add_argument("--frm", "--from", dest="frm", help="mes inicial YYYY-MM (YYYY-MM-DD ignorado)")
    sel.add_argument("--to", help="mes final YYYY-MM")
    out = p.add_argument_group("salida")
    out.set_defaults(verify=True, keep_zip=True, keep_csv=True)
    out.add_argument("-o", "--out", default="data/binance", help="directorio raíz (default: data/binance)")
    out.add_argument("--workers", type=int, default=4, help="descargas simultáneas (default: 4)")
    out.add_argument("--retries", type=int, default=5, help="reintentos por archivo (default: 5)")
    out.add_argument("--merge-format", choices=["csv", "parquet"], help="además genera un archivo combinado")
    out.add_argument("--merged-timestamps", action="store_true", help="en el CSV combinado añade columna timestamp ISO UTC")
    out.add_argument("--no-verify", dest="verify", action="store_false", help="no comprobar SHA-256")
    out.add_argument("--no-keep-zip", dest="keep_zip", action="store_false", help="borra los .zip tras extraer el CSV")
    out.add_argument("--no-csv", dest="keep_csv", action="store_false", help="no extraer CSV por mes")
    out.add_argument("--redownload", action="store_true", help="ignora lo ya descargado y vuelve a bajarlo")
    out.add_argument("--recheck", action="store_true", help="revalida el SHA-256 de lo ya descargado")
    out.add_argument("--audit", action="store_true", help="comprueba que cada mes tenga todas sus velas (detecta meses truncados)")
    out.add_argument("--dry-run", action="store_true", help="solo lista qué se descargaría")
    args = p.parse_args()

    if args.interval not in INTERVALS:
        sys.exit(f"Intervalo inválido: {args.interval}. Opciones: {', '.join(sorted(INTERVALS))}")
    if args.years is None and args.iterations is None and not args.frm and not args.to:
        sys.exit("Indica --years, --iterations o --from/--to.")
    if args.years is not None and args.iterations is not None:
        sys.exit("Usa --years o --iterations, no ambos.")
    if args.frm and not args.to and args.years is None and args.iterations is None:
        sys.exit("--from necesita --to (o usa --iterations).")

    prefix = f"{MARKETS[args.market]}/monthly/klines/{args.symbol}/{args.interval}/"
    base = Path(args.out) / args.market / args.symbol / args.interval

    say(f"Listando {CDN}?prefix={prefix}")
    objects = available_months(prefix, args.symbol, args.interval)
    if not objects:
        sys.exit(f"No hay archivos para {args.symbol} {args.interval} en {args.market}.")
    first = month_of(objects[0], args.symbol, args.interval)
    last = month_of(objects[-1], args.symbol, args.interval)
    say(f"Disponible: {first} .. {last}  ({len(objects)} meses, {human(sum(o.size for o in objects))})")

    months = select_months(objects, args)
    if not months:
        sys.exit("El rango pedido no contiene ningún mes.")
    total_bytes = sum(o.size for o in months)
    say(f"A descargar: {len(months)} meses "
        f"({month_of(months[0], args.symbol, args.interval)} .. {month_of(months[-1], args.symbol, args.interval)}), "
        f"{human(total_bytes)} -> {base}")

    if args.dry_run:
        for obj in months:
            say(f"  {month_of(obj, args.symbol, args.interval)}  {human(obj.size):>9}  {obj.key.rsplit('/', 1)[-1]}")
        return 0

    t0 = time.time()
    failures: list[str] = []
    with futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        jobs = [pool.submit(process, obj, args, base, i + 1, len(months)) for i, obj in enumerate(months)]
        for job in futures.as_completed(jobs):
            ok, _, err = job.result()
            if not ok:
                failures.append(err)
                say(f"ERROR {err}")

    elapsed = time.time() - t0
    say(f"\n{len(months) - len(failures)}/{len(months)} meses en {elapsed:.1f}s "
        f"({human(total_bytes / max(elapsed, 1e-9))}/s)")

    if failures:
        say(f"Fallos ({len(failures)}):")
        for f in failures:
            say(f"  - {f}")
        say("Vuelve a ejecutar el mismo comando: las descargas son reanudables e idempotentes.")

    if args.merge_format and args.keep_csv:
        stem = f"{args.symbol}-{args.interval}"
        disk = sorted((base / "csv").glob(f"{stem}-*.csv"))
        if len(disk) < 2:
            say(f"Solo hay {len(disk)} mes(es) en disco: no se genera archivo combinado.")
        else:
            target = base / "merged" / f"{stem}.{args.merge_format}"
            if args.merge_format == "parquet":
                rows = merge_parquet(disk, target)
            else:
                rows = merge_csv(disk, target, args.merged_timestamps)
            say(f"Combinado: {target} ({rows:,} velas de {len(disk)} meses, {human(target.stat().st_size)})")

    if args.audit:
        problems = audit(base, args.symbol, args.interval)
        if problems:
            say(f"\nIncidencias de integridad ({len(problems)}):")
            for problem in problems:
                say(f"  - {problem}")
            say("Si algún mes sigue incompleto, es probable que Binance aún lo esté "
                "publicando: vuelve a ejecutarlo más tarde.")
        else:
            say("\nAuditoría OK: todos los meses tienen el número de velas esperado.")

    if not args.keep_zip:
        for obj in months:
            (base / "monthly" / obj.key.rsplit("/", 1)[-1]).unlink(missing_ok=True)
        say("Zips eliminados (--no-keep-zip).")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
