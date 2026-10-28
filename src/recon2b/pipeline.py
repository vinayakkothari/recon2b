"""One call that loads the files, reconciles them and writes the report."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from pathlib import Path

from .decisions import DecisionStore
from .loaders import load_books, load_gstr2b
from .matcher import MatchConfig, ReconResult, reconcile
from .report import write_excel, write_flat


def run(
    books: str | Path,
    gstr2b: Sequence[str | Path],
    out: str | Path,
    config: MatchConfig | None = None,
    decisions: str | Path | None = None,
    column_map: str | Path | dict | None = None,
    books_sheet: str | None = None,
) -> tuple[ReconResult, Path]:
    timings: dict[str, float] = {}
    clock = time.perf_counter()
    mapping = _read_map(column_map)
    b = load_books(books, sheet=books_sheet, column_map=mapping)
    t = load_gstr2b(gstr2b)
    timings["load"] = time.perf_counter() - clock

    store = DecisionStore(decisions)
    result = reconcile(b, t, config, store.to_matcher())
    timings["match"] = sum(result.timings.values())

    clock = time.perf_counter()
    out = Path(out)
    if out.suffix.lower() in (".csv", ".parquet"):
        write_flat(result, out)
    else:
        meta = {
            "Purchase register": Path(books).name,
            "GSTR-2B": ", ".join(Path(p).name for p in gstr2b),
            "Saved decisions used": str(len(store)),
        }
        write_excel(result, out, meta)
    timings["write"] = time.perf_counter() - clock
    result.timings.update({f"total_{k}": v for k, v in timings.items()})
    return result, out


def _read_map(column_map: str | Path | dict | None) -> dict[str, str] | None:
    if column_map is None or isinstance(column_map, dict):
        return column_map
    return json.loads(Path(column_map).read_text())
