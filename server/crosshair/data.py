"""Resolution of `$ref` data references inside Plotly specs.

An agent may replace any data array in a spec with:

    {"$ref": {"file": "results/run3.parquet", "column": "loss"}}

The server loads the file (parquet/csv/json/feather), caches the frame, and
rewrites the ref into `{"$data": "<ref_id>"}`. The browser fetches the array
from `/data/{ref_id}` and splices it in before handing the spec to Plotly.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from . import persist

# ref_id -> materialized list of values
_ARRAYS: dict[str, list] = {}
# (resolved path, mtime) -> DataFrame
_FRAMES: dict[tuple[str, float], pd.DataFrame] = {}

MAX_FRAME_CACHE = 16
SUFFIX_LOADERS = {
    ".parquet": pd.read_parquet,
    ".pq": pd.read_parquet,
    ".csv": pd.read_csv,
    ".tsv": lambda p: pd.read_csv(p, sep="\t"),
    ".json": pd.read_json,
    ".feather": pd.read_feather,
    ".arrow": pd.read_feather,
}


class DataRefError(ValueError):
    """Raised when a $ref cannot be resolved. Message is written for the agent."""


def _load_frame(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    loader = SUFFIX_LOADERS.get(suffix)
    if loader is None:
        raise DataRefError(
            f"Unsupported data file type {suffix!r} for {path}. "
            f"Supported: {', '.join(sorted(SUFFIX_LOADERS))}"
        )
    key = (str(path), path.stat().st_mtime)
    frame = _FRAMES.get(key)
    if frame is None:
        try:
            frame = loader(path)
        except Exception as exc:  # noqa: BLE001 - surfaced to the agent verbatim
            raise DataRefError(f"Failed to read {path}: {exc}") from exc
        if len(_FRAMES) >= MAX_FRAME_CACHE:
            _FRAMES.pop(next(iter(_FRAMES)))
        _FRAMES[key] = frame
    return frame


def _jsonable(values: list) -> list:
    out: list[Any] = []
    for v in values:
        item: Any = v
        if isinstance(item, float) and not math.isfinite(item):
            out.append(None)
            continue
        to_iso = getattr(item, "isoformat", None)
        if callable(to_iso):
            out.append(to_iso())
            continue
        to_scalar = getattr(item, "item", None)
        if callable(to_scalar):
            out.append(to_scalar())
            continue
        out.append(item)
    return out


def _resolve_one(ref: dict, base_dir: Path) -> str:
    """Materialize one $ref into the array cache; return its ref_id."""
    if not isinstance(ref, dict):
        raise DataRefError(f"$ref must be an object, got {type(ref).__name__}")
    file = ref.get("file")
    column = ref.get("column")
    if not file or not column:
        raise DataRefError('$ref requires both "file" and "column", e.g. {"file": "a.parquet", "column": "loss"}')

    path = Path(file)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    if not path.exists():
        raise DataRefError(f"Data file not found: {path}")

    frame = _load_frame(path)
    if column not in frame.columns:
        raise DataRefError(
            f"Column {column!r} not in {path.name}. Available columns: {', '.join(map(str, frame.columns))}"
        )

    series = frame[column]
    if "query" in ref and ref["query"]:
        try:
            series = frame.query(ref["query"])[column]
        except Exception as exc:  # noqa: BLE001
            raise DataRefError(f"Invalid query {ref['query']!r}: {exc}") from exc

    stride = int(ref.get("stride", 1) or 1)
    if stride > 1:
        series = series.iloc[::stride]

    values = _jsonable(series.tolist())
    ref_id = hashlib.sha1(
        json.dumps([str(path), column, ref.get("query"), stride], sort_keys=True).encode()
    ).hexdigest()[:16]
    _ARRAYS[ref_id] = values
    # Keep a copy on disk so a panel restored after a restart still renders,
    # even if the source file has since moved or changed.
    persist.save_array(ref_id, values)
    return ref_id


def resolve_refs(spec: Any, base_dir: Path) -> Any:
    """Walk a spec, replacing every {"$ref": {...}} with {"$data": "<ref_id>"}."""
    if isinstance(spec, dict):
        if "$ref" in spec and len(spec) == 1:
            return {"$data": _resolve_one(spec["$ref"], base_dir)}
        return {k: resolve_refs(v, base_dir) for k, v in spec.items()}
    if isinstance(spec, list):
        return [resolve_refs(v, base_dir) for v in spec]
    return spec


def get_array(ref_id: str) -> list | None:
    values = _ARRAYS.get(ref_id)
    if values is None:
        # A panel restored from disk references arrays this process never
        # resolved; read them back rather than serving the browser a 404.
        values = persist.load_array(ref_id)
        if values is not None:
            _ARRAYS[ref_id] = values
    return values


def describe_file(file: str, base_dir: Path) -> dict:
    """Column names, dtypes, and row count — so an agent can plot without guessing."""
    path = Path(file)
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    if not path.exists():
        raise DataRefError(f"Data file not found: {path}")
    frame = _load_frame(path)
    return {
        "path": str(path),
        "rows": int(len(frame)),
        "columns": [{"name": str(c), "dtype": str(frame[c].dtype)} for c in frame.columns],
        "head": json.loads(frame.head(5).to_json(orient="records", date_format="iso") or "[]"),
    }
