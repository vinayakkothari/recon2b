"""Read CSV and Excel exports whose column names nobody agreed on.

Tally, Zoho, Busy and hand-made sheets all call the same column something
different. We normalise each header to lowercase letters and digits and look it
up in an alias list. Excel sheets often start with a title block, so we also scan
the first rows to find where the real header is.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Iterable
from pathlib import Path

import polars as pl
from openpyxl import load_workbook

from ..normalise import parse_amount, parse_dates, parse_flag
from ..schema import CREDIT_NOTE, DEBIT_NOTE, INVOICE, conform

# Order matters: the first alias found in the file wins.
ALIASES: dict[str, list[str]] = {
    "gstin": [
        "gstinofsupplier",
        "suppliergstin",
        "gstinuinofsupplier",
        "partygstin",
        "vendorgstin",
        "gstinuin",
        "gstin",
        "ctin",
        "gstno",
        "gstnumber",
    ],
    "supplier_name": [
        "tradelegalname",
        "suppliername",
        "partyname",
        "vendorname",
        "tradename",
        "legalname",
        "particulars",
        "ledgername",
        "trdnm",
        "name",
    ],
    "inv_no": [
        "supplierinvoiceno",
        "supplierinvoicenumber",
        "invoicenumber",
        "invoiceno",
        "invoicenum",
        "billno",
        "billnumber",
        "invno",
        "notenumber",
        "noteno",
        "documentnumber",
        "docno",
        "voucherref",
        "referenceno",
        "refno",
        "inum",
        "ntnum",
    ],
    "inv_date": [
        "supplierinvoicedate",
        "invoicedate",
        "billdate",
        "notedate",
        "documentdate",
        "docdate",
        "invdate",
        "dt",
        "date",
    ],
    "taxable": [
        "taxablevalue",
        "taxableamount",
        "taxableval",
        "assessablevalue",
        "txval",
        "basicamount",
    ],
    "igst": ["integratedtax", "igstamount", "igstamt", "igst"],
    "cgst": ["centraltax", "cgstamount", "cgstamt", "cgst"],
    "sgst": ["stateuttax", "statetax", "sgstutgst", "sgstamount", "sgstamt", "sgst", "utgst"],
    "cess": ["cessamount", "cessamt", "cess"],
    "inv_value": [
        "invoicevalue",
        "totalinvoicevalue",
        "notevalue",
        "invoiceamount",
        "grosstotal",
        "total",
        "val",
    ],
    "doc_type": ["documenttype", "doctype", "notetype", "vouchertype", "type"],
    "itc_available": ["itcavailability", "itcavailable", "itcavl"],
    "reverse_charge": ["supplyattractreversecharge", "reversecharge", "rcm", "rev"],
}

_NEEDED = ("gstin", "inv_no", "taxable")


def header_key(name: object) -> str:
    return re.sub(r"[^0-9a-z]", "", str(name or "").lower())


def map_columns(headers: Iterable[str], overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Return {canonical: source header}. ``overrides`` wins over the alias list."""
    headers = list(headers)
    by_key: dict[str, str] = {}
    for h in headers:
        by_key.setdefault(header_key(h), h)
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for canon, source in (overrides or {}).items():
        if source not in headers:
            raise ValueError(f"column '{source}' given for '{canon}' is not in the file")
        mapping[canon] = source
        used.add(source)
    for canon, aliases in ALIASES.items():
        if canon in mapping:
            continue
        for alias in aliases:
            source = by_key.get(alias)
            if source is not None and source not in used:
                mapping[canon] = source
                used.add(source)
                break
    return mapping


def _score_row(values: list[object]) -> int:
    keys = {header_key(v) for v in values if v not in (None, "")}
    return sum(1 for aliases in ALIASES.values() if keys & set(aliases))


def _sheet_rows(path: Path, sheet: str | None) -> tuple[list[str], list[list[object]]]:
    """Find the header inside a sheet and return (headers, data rows)."""
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet else wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        preview: list[list[object]] = []
        for _ in range(15):
            try:
                preview.append(list(next(rows)))
            except StopIteration:
                break
        scores = [_score_row(r) for r in preview]
        if not scores or max(scores) < 2:
            raise ValueError(f"could not find a header row in sheet '{ws.title}'")
        best = scores.index(max(scores))
        # Portal sheets use two header rows: a group label ("Invoice details", "Tax
        # Amount") over the real names, and some names only on the upper row. Take
        # every adjacent row that also looks like a header and let the lower row win.
        first, last = best, best
        if first > 0 and scores[first - 1] >= 2:
            first -= 1
        if last + 1 < len(scores) and scores[last + 1] >= 2:
            last += 1
        width = max(len(preview[i]) for i in range(first, last + 1))
        headers = [""] * width
        for i in range(first, last + 1):
            for j, v in enumerate(preview[i]):
                if v not in (None, ""):
                    headers[j] = str(v).strip()
        start = last + 1
        headers = _dedupe([hd or f"column_{i}" for i, hd in enumerate(headers)])
        data = preview[start:] + [list(r) for r in rows]
        width = len(headers)
        data = [
            (r + [None] * (width - len(r)))[:width]
            for r in data
            if any(v not in (None, "") for v in r)
        ]
        return headers, data
    finally:
        wb.close()


def _dedupe(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        if n in seen:
            seen[n] += 1
            out.append(f"{n}_{seen[n]}")
        else:
            seen[n] = 0
            out.append(n)
    return out


def sheet_names(path: Path) -> list[str]:
    wb = load_workbook(path, read_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def read_raw(path: str | Path, sheet: str | None = None) -> pl.DataFrame:
    """Read a CSV or Excel file into a frame of strings, header located automatically."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".csv", ".txt", ".tsv"):
        sep = "\t" if suffix == ".tsv" else ","
        return pl.read_csv(path, separator=sep, infer_schema=False, encoding="utf8-lossy")
    if suffix in (".xlsx", ".xlsm"):
        headers, data = _sheet_rows(path, sheet)
        columns = list(zip(*data, strict=True)) if data else [() for _ in headers]
        return pl.DataFrame(
            [_to_series(h, list(col)) for h, col in zip(headers, columns, strict=True)]
        )
    raise ValueError(f"unsupported file type: {path.name} (use .csv or .xlsx)")


def _to_series(name: str, values: list[object]) -> pl.Series:
    """Excel cells mix numbers, dates and text. Keep dates as dates, the rest as text."""
    if values and all(isinstance(v, (_dt.date, type(None))) for v in values) and any(values):
        return pl.Series(name, [v.date() if isinstance(v, _dt.datetime) else v for v in values])
    return pl.Series(name, [None if v is None else _cell_text(v) for v in values], dtype=pl.Utf8)


def _cell_text(v: object) -> str:
    if isinstance(v, _dt.datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, _dt.date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    return str(v)


def doc_type_expr(col: str) -> pl.Expr:
    text = pl.col(col).cast(pl.Utf8).str.to_uppercase().str.replace_all(r"[^A-Z]", "")
    return (
        pl.when(text.str.contains("CREDIT") | text.is_in(["C", "CN", "CRN", "CDN"]))
        .then(pl.lit(CREDIT_NOTE))
        .when(text.str.contains("DEBIT") | text.is_in(["D", "DN", "DBN"]))
        .then(pl.lit(DEBIT_NOTE))
        .otherwise(pl.lit(INVOICE))
        .alias("doc_type")
    )


def to_canonical(
    raw: pl.DataFrame,
    overrides: dict[str, str] | None = None,
    source_label: str = "file",
    default_doc_type: str | None = None,
) -> pl.DataFrame:
    """Rename, clean and type a raw frame into the canonical schema."""
    mapping = map_columns(raw.columns, overrides)
    missing = [c for c in _NEEDED if c not in mapping]
    if missing:
        raise ValueError(
            f"{source_label}: could not find columns for {', '.join(missing)}. "
            f"Columns seen: {', '.join(raw.columns)}. Pass a column map to point at them."
        )
    df = raw.select([pl.col(src).alias(canon) for canon, src in mapping.items()])
    df = df.with_row_index("row").with_columns(pl.col("row").cast(pl.Int64))
    amounts = [c for c in ("taxable", "igst", "cgst", "sgst", "cess", "inv_value") if c in df]
    exprs = [parse_amount(c) for c in amounts]
    if "doc_type" in df.columns:
        exprs.append(doc_type_expr("doc_type"))
    else:
        exprs.append(pl.lit(default_doc_type or INVOICE).alias("doc_type"))
    for flag in ("itc_available", "reverse_charge"):
        if flag in df.columns:
            exprs.append(parse_flag(flag))
    df = df.with_columns(exprs)
    if "inv_date" in df.columns:
        df = df.with_columns(parse_dates(df["inv_date"]).alias("inv_date"))
    else:
        df = df.with_columns(pl.lit(None, pl.Date).alias("inv_date"))
    # Some exports keep credit notes negative. We store magnitudes and rely on doc_type.
    if "taxable" in df.columns:
        negative = pl.col("taxable") < 0
        df = df.with_columns(
            pl.when(negative & (pl.col("doc_type") == INVOICE))
            .then(pl.lit(CREDIT_NOTE))
            .otherwise(pl.col("doc_type"))
            .alias("doc_type")
        ).with_columns([pl.col(c).abs() for c in amounts])
    return conform(df)
