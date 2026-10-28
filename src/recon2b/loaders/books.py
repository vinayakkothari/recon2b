"""Purchase register exported from the books (Tally, Zoho, Busy or a hand-made sheet)."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from .tabular import read_raw, to_canonical


def load_books(
    path: str | Path,
    sheet: str | None = None,
    column_map: dict[str, str] | None = None,
) -> pl.DataFrame:
    raw = read_raw(path, sheet)
    return to_canonical(raw, column_map, source_label=f"purchase register {Path(path).name}")
