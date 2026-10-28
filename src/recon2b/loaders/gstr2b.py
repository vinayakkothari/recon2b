"""GSTR-2B as downloaded from the GST portal, in JSON or Excel.

The JSON groups documents by supplier:

    data.docdata.b2b[].ctin / trdnm / inv[].inum, dt, val, itcavl, rev, items[]
    data.docdata.cdnr[].ctin / trdnm / nt[].ntnum, typ (C or D), dt, val, items[]

Line items carry the amounts, so an invoice's taxable value and tax are the sum of
its items. Dates are DD-MM-YYYY. Amendment sections (b2ba, cdnra) are read too,
using the amended document number.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import polars as pl

from ..normalise import parse_dates
from ..schema import CREDIT_NOTE, DEBIT_NOTE, INVOICE, conform, empty_frame
from .tabular import header_key, read_raw, sheet_names, to_canonical

_AMOUNTS = ("txval", "igst", "cgst", "sgst", "cess")


def load_gstr2b(paths: str | Path | Iterable[str | Path]) -> pl.DataFrame:
    """Load one or more GSTR-2B files (several months, or JSON plus Excel)."""
    if isinstance(paths, (str, Path)):
        paths = [paths]
    frames = []
    for p in paths:
        p = Path(p)
        if p.suffix.lower() == ".json":
            frames.append(_from_json(p))
        elif p.suffix.lower() in (".xlsx", ".xlsm"):
            frames.append(_from_excel(p))
        else:
            raise ValueError(f"unsupported GSTR-2B file: {p.name} (use the portal .json or .xlsx)")
    if not frames:
        return empty_frame()
    df = pl.concat(frames, how="vertical_relaxed")
    # rows must stay unique across files so results can be traced back
    return df.with_columns(pl.int_range(pl.len(), dtype=pl.Int64).alias("row"))


# ------------------------------------------------------------------------- JSON


def _from_json(path: Path) -> pl.DataFrame:
    with path.open("rb") as fh:
        payload = json.load(fh)
    root = payload.get("data", payload)
    docdata = root.get("docdata", root)

    cols: dict[str, list] = {
        k: []
        for k in (
            "gstin",
            "supplier_name",
            "doc_type",
            "inv_no",
            "inv_date",
            "taxable",
            "igst",
            "cgst",
            "sgst",
            "cess",
            "inv_value",
            "itc_available",
            "reverse_charge",
        )
    }

    def add(sup: dict, doc: dict, number_key: str, doc_type: str) -> None:
        totals = _sum_items(doc)
        cols["gstin"].append(sup.get("ctin"))
        cols["supplier_name"].append(sup.get("trdnm"))
        cols["doc_type"].append(doc_type)
        cols["inv_no"].append(doc.get(number_key))
        cols["inv_date"].append(doc.get("dt"))
        cols["taxable"].append(totals[0])
        cols["igst"].append(totals[1])
        cols["cgst"].append(totals[2])
        cols["sgst"].append(totals[3])
        cols["cess"].append(totals[4])
        cols["inv_value"].append(doc.get("val"))
        cols["itc_available"].append(str(doc.get("itcavl", "Y")).upper() == "Y")
        cols["reverse_charge"].append(str(doc.get("rev", "N")).upper() == "Y")

    for section in ("b2b", "b2ba"):
        for sup in docdata.get(section) or []:
            for doc in sup.get("inv") or []:
                add(sup, doc, "inum", INVOICE)
    for section in ("cdnr", "cdnra"):
        for sup in docdata.get(section) or []:
            for doc in sup.get("nt") or []:
                kind = str(doc.get("typ", "C")).upper()
                add(sup, doc, "ntnum", DEBIT_NOTE if kind == "D" else CREDIT_NOTE)

    if not cols["gstin"]:
        return empty_frame()
    df = pl.DataFrame(cols, schema_overrides={"inv_date": pl.Utf8, "inv_value": pl.Float64})
    df = df.with_columns(parse_dates(df["inv_date"]).alias("inv_date"))
    return conform(df.with_row_index("row"))


def _sum_items(doc: dict) -> tuple[float, float, float, float, float]:
    # Older files carry totals on the document, newer ones only inside items.
    if not doc.get("items"):
        return tuple(float(doc.get(k) or 0) for k in _AMOUNTS)  # type: ignore[return-value]
    sums = [0.0] * 5
    for item in doc.get("items") or []:
        for i, k in enumerate(_AMOUNTS):
            sums[i] += float(item.get(k) or 0)
    return tuple(round(s, 2) for s in sums)  # type: ignore[return-value]


# ------------------------------------------------------------------------ Excel


def _from_excel(path: Path) -> pl.DataFrame:
    frames = []
    for name in sheet_names(path):
        key = header_key(name)
        if key in ("b2b", "b2ba"):
            default = INVOICE
        elif key.startswith("b2bcdnr"):
            default = CREDIT_NOTE
        else:
            continue  # summary, read-me, ISD, import sheets and so on
        raw = read_raw(path, name)
        if raw.height == 0:
            continue
        frames.append(
            to_canonical(raw, source_label=f"{path.name} sheet {name}", default_doc_type=default)
        )
    if not frames:
        raise ValueError(f"{path.name}: no B2B or B2B-CDNR sheet found")
    return pl.concat(frames, how="vertical_relaxed")
