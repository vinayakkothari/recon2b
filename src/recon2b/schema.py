"""The one table shape every loader produces.

Books and GSTR-2B arrive in very different formats. Each loader turns its input
into this canonical frame so the matcher never needs to know where a row came from.
"""

from __future__ import annotations

import polars as pl

# Document types. Amounts are always stored positive; the type carries the sign.
INVOICE = "INV"
CREDIT_NOTE = "CRN"
DEBIT_NOTE = "DBN"

CANONICAL_SCHEMA: dict[str, pl.DataType] = {
    "row": pl.Int64,  # position in the source file, used to trace a result back
    "gstin": pl.Utf8,
    "supplier_name": pl.Utf8,
    "doc_type": pl.Utf8,
    "inv_no": pl.Utf8,
    "inv_date": pl.Date,
    "taxable": pl.Float64,
    "igst": pl.Float64,
    "cgst": pl.Float64,
    "sgst": pl.Float64,
    "cess": pl.Float64,
    "inv_value": pl.Float64,
    "itc_available": pl.Boolean,
    "reverse_charge": pl.Boolean,
}

AMOUNT_COLUMNS = ["taxable", "igst", "cgst", "sgst", "cess", "inv_value"]


def empty_frame() -> pl.DataFrame:
    return pl.DataFrame(schema=CANONICAL_SCHEMA)


def conform(df: pl.DataFrame) -> pl.DataFrame:
    """Add any missing canonical column with a sensible default and fix dtypes."""
    defaults = {
        "supplier_name": pl.lit(None, pl.Utf8),
        "doc_type": pl.lit(INVOICE),
        "igst": pl.lit(0.0),
        "cgst": pl.lit(0.0),
        "sgst": pl.lit(0.0),
        "cess": pl.lit(0.0),
        "inv_value": pl.lit(None, pl.Float64),
        "itc_available": pl.lit(True),
        "reverse_charge": pl.lit(False),
    }
    required = [c for c in ("gstin", "inv_no", "taxable") if c not in df.columns]
    if required:
        raise ValueError(f"missing required columns: {', '.join(required)}")
    if "row" not in df.columns:
        df = df.with_row_index("row")
    if "inv_date" not in df.columns:
        df = df.with_columns(pl.lit(None, pl.Date).alias("inv_date"))
    missing = [defaults[c].alias(c) for c in defaults if c not in df.columns]
    if missing:
        df = df.with_columns(missing)
    df = df.select([pl.col(c).cast(t, strict=False) for c, t in CANONICAL_SCHEMA.items()])
    return df.with_columns(
        [pl.col(c).fill_null(0.0) for c in ["igst", "cgst", "sgst", "cess"]]
        + [
            # " 29abcde1234f1z5 " and "29ABCDE1234F1Z5" are the same supplier
            pl.col("gstin").str.to_uppercase().str.replace_all(r"[^0-9A-Z]", ""),
            pl.col("taxable").fill_null(0.0),
            pl.col("doc_type").fill_null(INVOICE),
            pl.col("itc_available").fill_null(True),
            pl.col("reverse_charge").fill_null(False),
        ]
    )
