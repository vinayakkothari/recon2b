"""Cleaning rules shared by every loader and by the matcher.

Most of the real work in GST reconciliation is here. The same invoice shows up as
`INV/24-25/0012` on the portal and `inv 12` in the books, so we derive two keys:

* ``inv_norm``  uppercase, financial year token removed, separators removed,
  leading zeros removed from each number.  INV/24-25/0012 -> INV12
* ``inv_core``  the last run of digits in ``inv_norm``.               INV12 -> 12

Everything here is written as Polars expressions so it runs vectorised over
millions of rows. The only Python loops are over unique GSTINs, which are few.
"""

from __future__ import annotations

import re

import polars as pl

# ---------------------------------------------------------------- invoice numbers

# A financial year token like 24-25, 2024-25, 2024/2025, split into what comes before
# it, the two years and what comes after. The years let us confirm they are
# consecutive so an invoice number like 12-40 is left alone.
_YEARS = r"(?:20)?(\d{2})\s*[-/]\s*(?:20)?(\d{2})((?:[^0-9].*)?)$"
_FIRST_PAIR = r"^(|.*?[^0-9])" + _YEARS  # leftmost two-number pair
_LAST_PAIR = r"^(|.*[^0-9])" + _YEARS  # rightmost two-number pair


def _drop_fy(raw: pl.Expr, pattern: str) -> tuple[pl.Expr, pl.Expr]:
    before = raw.str.extract(pattern, 1)
    y1 = raw.str.extract(pattern, 2).cast(pl.Int32, strict=False)
    y2 = raw.str.extract(pattern, 3).cast(pl.Int32, strict=False)
    after = raw.str.extract(pattern, 4)
    is_fy = y1.is_not_null() & (y2 == (y1 + 1) % 100)
    return is_fy, before + "/" + after


def fiscal_year_starts(dates: pl.Series) -> list[int]:
    """Financial years (April to March) touched by these dates, plus the one before."""
    d = dates.drop_nulls()
    if d.len() == 0:
        return []
    starts = (d.dt.year() - (d.dt.month() < 4).cast(pl.Int32)).unique().to_list()
    return sorted({y for s in starts for y in (s - 1, s)})


def invoice_keys(col: str = "inv_no", fiscal_years: list[int] | None = None) -> list[pl.Expr]:
    """Expressions adding ``inv_exact``, ``inv_norm`` and ``inv_core`` from ``col``.

    ``fiscal_years`` lists the April-March years in play (2026 means 2026-27). When
    given, a year glued into the number with no separator, like INV26270535 or
    GST202627792, is removed as well. That cannot be done for every year blindly
    because 2627 might just be part of a serial number.
    """
    raw = pl.col(col).cast(pl.Utf8).fill_null("").str.strip_chars().str.to_uppercase()
    # The year can sit anywhere, so try the leftmost and rightmost number pairs.
    # A12/24-25/5 has 12/24 first, which is not a financial year, and 24-25 last.
    first_is_fy, first_dropped = _drop_fy(raw, _FIRST_PAIR)
    last_is_fy, last_dropped = _drop_fy(raw, _LAST_PAIR)
    without_fy = (
        pl.when(first_is_fy).then(first_dropped).when(last_is_fy).then(last_dropped).otherwise(raw)
    )
    norm = (
        # strip leading zeros of every number while separators still mark where
        # numbers start: INV/0012 -> INV/12, INV0012 -> INV12, 12/0007 -> 12/7
        without_fy.str.replace_all(r"(^|[^0-9])0+([0-9])", "${1}${2}").str.replace_all(
            r"[^0-9A-Z]", ""
        )
    )
    for start in fiscal_years or []:
        a, b = start % 100, (start + 1) % 100
        # INV26270535 -> INV0535 -> INV535, only when a serial number follows
        glued = rf"(^|[A-Z])(?:20)?{a:02d}{b:02d}0*([1-9][0-9]*)$"
        norm = norm.str.replace(glued, "${1}${2}")
    # guard against an invoice number that was only a financial year or punctuation
    norm = pl.when(norm == "").then(raw.str.replace_all(r"[^0-9A-Z]", "")).otherwise(norm)
    core = norm.str.extract(r"([0-9]+)[^0-9]*$", 1)
    return [
        raw.alias("inv_exact"),
        norm.alias("inv_norm"),
        core.alias("inv_core"),
    ]


# ------------------------------------------------------------------------- GSTIN

_GSTIN_SHAPE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_check_char(first14: str) -> str:
    """The check character GSTN computes for the first 14 characters."""
    total = 0
    for i, ch in enumerate(first14):
        product = _CHARSET.index(ch) * (2 if i % 2 else 1)
        total += product // 36 + product % 36
    return _CHARSET[(36 - total % 36) % 36]


def is_valid_gstin(gstin: str | None) -> bool:
    if not gstin or not _GSTIN_SHAPE.match(gstin):
        return False
    return gstin_check_char(gstin[:14]) == gstin[14]


# ------------------------------------------------------------------- dates, money

_DATE_FORMATS = [
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%d.%m.%Y",
    "%d-%b-%Y",
    "%d %b %Y",
    "%d-%m-%y",
    "%d/%m/%y",
    "%d-%b-%y",
]


def parse_dates(s: pl.Series) -> pl.Series:
    """Parse whatever an accountant's export hands us into ``pl.Date``."""
    if s.dtype == pl.Date:
        return s
    if s.dtype == pl.Datetime or isinstance(s.dtype, pl.Datetime):
        return s.dt.date()
    text = (
        s.cast(pl.Utf8)
        .str.strip_chars()
        .str.replace(r"[T\s]+\d{1,2}:\d{2}(:\d{2})?.*$", "")
        .str.to_titlecase()  # OCT -> Oct for %b
    )
    frame = pl.DataFrame({"t": text})
    attempts = [
        pl.col("t").str.strptime(pl.Date, fmt, strict=False, exact=True) for fmt in _DATE_FORMATS
    ]
    parsed = frame.select(pl.coalesce(attempts).alias("d"))["d"]
    # two digit years parsed by %Y land in year 26 AD; move them to this century
    return (
        pl.DataFrame({"d": parsed})
        .select(
            pl.when(pl.col("d").dt.year() < 100)
            .then(pl.col("d").dt.offset_by("2000y"))
            .otherwise(pl.col("d"))
        )
        .to_series()
        .alias(s.name)
    )


def parse_amount(col: str) -> pl.Expr:
    """Money columns arrive as numbers, '1,23,456.00', '₹ 500' or '(120.50)'."""
    text = pl.col(col).cast(pl.Utf8).str.strip_chars()
    negative = text.str.starts_with("(") & text.str.ends_with(")")
    digits = text.str.replace_all(r"[^0-9.\-]", "")
    value = pl.when(digits.is_in(["", "-", "."])).then(None).otherwise(digits)
    number = value.cast(pl.Float64, strict=False)
    return pl.when(negative).then(-number).otherwise(number).alias(col)


def parse_flag(col: str, true_values: tuple[str, ...] = ("Y", "YES", "TRUE", "1")) -> pl.Expr:
    text = pl.col(col).cast(pl.Utf8).str.strip_chars().str.to_uppercase()
    return pl.when(text.is_null()).then(None).otherwise(text.is_in(true_values)).alias(col)
