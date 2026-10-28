"""Write the reconciliation as an Excel workbook an accountant can hand to a client.

Sheets
    Summary         status counts and ITC at each status, with what to do next
    Reconciliation  every document side by side, filterable
    Review          pairs the tool was unsure about, with an Accept or Reject column
    Suppliers       per supplier totals, worst first, for follow-up calls
    GSTIN issues    GSTINs in the books that never appear in 2B, with a suggestion

Amounts use Indian digit grouping (12,34,567.00) and dates are DD-MM-YYYY.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import polars as pl
import xlsxwriter

from .matcher import (
    AMOUNT_MISMATCH,
    MATCHED,
    MISSING_IN_2B,
    MISSING_IN_BOOKS,
    TAX_HEAD_MISMATCH,
    ReconResult,
)

EXCEL_MAX_ROWS = 1_000_000
INR = "[>=10000000]##\\,##\\,##\\,##0.00;[>=100000]##\\,##\\,##0.00;##,##0.00;-##,##0.00"

LABELS = {
    MATCHED: "Matched",
    TAX_HEAD_MISMATCH: "Tax head differs",
    AMOUNT_MISMATCH: "Amount differs",
    MISSING_IN_2B: "In books, not in 2B",
    MISSING_IN_BOOKS: "In 2B, not in books",
}
ACTIONS = {
    MATCHED: "Claim the credit, except rows 2B marks as ITC not available (see Remarks).",
    TAX_HEAD_MISMATCH: "Supplier used IGST where CGST and SGST were due, or the reverse. "
    "Ask them to amend.",
    AMOUNT_MISMATCH: "Compare with the invoice copy. Ask the supplier to amend, "
    "or correct the books.",
    MISSING_IN_2B: "Supplier has not filed it yet. Follow up and hold this credit until it shows.",
    MISSING_IN_BOOKS: "Record the purchase, or confirm with the supplier that it is not yours.",
}
STATUS_COLOURS = {
    MATCHED: "#E6F4EA",
    TAX_HEAD_MISMATCH: "#FFF4E5",
    AMOUNT_MISMATCH: "#FFF4E5",
    MISSING_IN_2B: "#FDECEA",
    MISSING_IN_BOOKS: "#E8F0FE",
}


@dataclass
class Col:
    field: str
    header: str
    width: float
    kind: str = "text"  # text, int, money, date, pct, bool


RECON_COLUMNS = [
    Col("status_label", "Status", 20),
    Col("gstin", "Supplier GSTIN", 18),
    Col("supplier_name", "Supplier", 28),
    Col("doc_type", "Doc", 6),
    Col("books_inv_no", "Books invoice no", 18),
    Col("gstr2b_inv_no", "2B invoice no", 18),
    Col("books_inv_date", "Books date", 11, "date"),
    Col("gstr2b_inv_date", "2B date", 11, "date"),
    Col("books_taxable", "Books taxable", 14, "money"),
    Col("gstr2b_taxable", "2B taxable", 14, "money"),
    Col("taxable_diff", "Taxable diff", 12, "money"),
    Col("books_igst", "Books IGST", 12, "money"),
    Col("books_cgst", "Books CGST", 12, "money"),
    Col("books_sgst", "Books SGST", 12, "money"),
    Col("gstr2b_igst", "2B IGST", 12, "money"),
    Col("gstr2b_cgst", "2B CGST", 12, "money"),
    Col("gstr2b_sgst", "2B SGST", 12, "money"),
    Col("tax_diff", "Tax diff", 12, "money"),
    Col("itc_at_risk", "ITC at risk", 13, "money"),
    Col("match_pass", "Matched by", 14),
    Col("confidence", "Confidence", 10, "pct"),
    Col("remarks", "Remarks", 60),
    Col("books_row", "Books row", 9, "int"),
    Col("gstr2b_row", "2B row", 9, "int"),
]

REVIEW_COLUMNS = [
    Col("decision", "Decision", 11),
    Col("status_label", "Status", 18),
    Col("gstin", "Supplier GSTIN", 18),
    Col("supplier_name", "Supplier", 26),
    Col("books_inv_no", "Books invoice no", 18),
    Col("gstr2b_inv_no", "2B invoice no", 18),
    Col("books_inv_date", "Books date", 11, "date"),
    Col("gstr2b_inv_date", "2B date", 11, "date"),
    Col("books_taxable", "Books taxable", 14, "money"),
    Col("gstr2b_taxable", "2B taxable", 14, "money"),
    Col("match_pass", "Matched by", 14),
    Col("confidence", "Confidence", 10, "pct"),
    Col("remarks", "Remarks", 50),
    # read back by `recon2b learn`; hidden so nobody edits them by accident
    Col("gstin", "gstin", 4),
    Col("doc_type", "doc_type", 4),
    Col("books_inv_norm", "books_inv_norm", 4),
    Col("gstr2b_inv_norm", "gstr2b_inv_norm", 4),
]

SUPPLIER_COLUMNS = [
    Col("gstin", "Supplier GSTIN", 18),
    Col("supplier_name", "Supplier", 30),
    Col("documents", "Documents", 11, "int"),
    Col("matched", "Matched", 10, "int"),
    Col("issues", "Issues", 9, "int"),
    Col("itc_in_books", "ITC in books", 15, "money"),
    Col("itc_in_2b", "ITC in 2B", 15, "money"),
    Col("itc_at_risk", "ITC at risk", 15, "money"),
]

GSTIN_COLUMNS = [
    Col("gstin", "GSTIN in books", 18),
    Col("supplier_name", "Supplier", 28),
    Col("documents", "Documents", 11, "int"),
    Col("tax_in_books", "Tax in books", 15, "money"),
    Col("problem", "Problem", 34),
    Col("did_you_mean", "Did you mean", 18),
    Col("edits", "Characters different", 12, "int"),
]


def _labelled(rows: pl.DataFrame) -> pl.DataFrame:
    return rows.with_columns(
        pl.col("status").cast(pl.Utf8).replace_strict(LABELS, default="").alias("status_label")
    )


class _Writer:
    def __init__(self, path: Path):
        self.wb = xlsxwriter.Workbook(
            str(path), {"constant_memory": True, "default_date_format": "dd-mm-yyyy"}
        )
        f = self.wb.add_format
        self.fmt = {
            "head": f(
                {
                    "bold": True,
                    "bg_color": "#1F3A5F",
                    "font_color": "#FFFFFF",
                    "text_wrap": True,
                    "valign": "vcenter",
                    "border": 1,
                    "border_color": "#C9D3E0",
                }
            ),
            "money": f({"num_format": INR}),
            "int": f({"num_format": "0"}),
            "pct": f({"num_format": "0%"}),
            "date": f({"num_format": "dd-mm-yyyy"}),
            "title": f({"bold": True, "font_size": 16, "font_color": "#1F3A5F"}),
            "muted": f({"font_color": "#5F6B7A"}),
            "muted_wrap": f({"font_color": "#5F6B7A", "text_wrap": True, "valign": "top"}),
            "bold": f({"bold": True}),
            "bold_money": f({"bold": True, "num_format": INR, "top": 1}),
            "bold_int": f({"bold": True, "num_format": "0", "top": 1}),
            "wrap": f({"text_wrap": True, "valign": "top"}),
            "decision": f({"bg_color": "#FFF9DB", "border": 1, "border_color": "#E0C96B"}),
        }
        self.status_fmt = {s: f({"bg_color": c}) for s, c in STATUS_COLOURS.items()}

    def table(
        self,
        name: str,
        df: pl.DataFrame,
        cols: Sequence[Col],
        status_col: bool = False,
        hide_from: int | None = None,
        decision: bool = False,
    ) -> None:
        chunks = max(1, -(-df.height // EXCEL_MAX_ROWS))
        for c in range(chunks):
            part = df.slice(c * EXCEL_MAX_ROWS, EXCEL_MAX_ROWS)
            ws = self.wb.add_worksheet(name if c == 0 else f"{name} ({c + 1})")
            for i, col in enumerate(cols):
                fmt = self.fmt.get(col.kind) if col.kind != "text" else None
                hidden = hide_from is not None and i >= hide_from
                ws.set_column(i, i, col.width, fmt, {"hidden": hidden})
            ws.set_row(0, 30)
            ws.set_landscape()
            ws.fit_to_pages(1, 0)
            ws.repeat_rows(0)
            ws.write_row(0, 0, [c.header for c in cols], self.fmt["head"])
            ws.freeze_panes(1, 1 if not decision else 2)
            data = part.select(
                [
                    (pl.col(c.field) if c.field in part.columns else pl.lit(None)).alias(f"_{i}")
                    for i, c in enumerate(cols)
                ]
            )
            for r, values in enumerate(data.iter_rows(), start=1):
                ws.write_row(r, 0, [_cell(v) for v in values])
            last = max(part.height, 1)
            ws.autofilter(0, 0, last, len(cols) - 1)
            if status_col:
                idx = next(i for i, c in enumerate(cols) if c.field == "status_label")
                for status, fmt in self.status_fmt.items():
                    ws.conditional_format(
                        1,
                        idx,
                        last,
                        idx,
                        {
                            "type": "cell",
                            "criteria": "==",
                            "value": f'"{LABELS[status]}"',
                            "format": fmt,
                        },
                    )
            if decision and part.height:
                ws.data_validation(
                    1,
                    0,
                    part.height,
                    0,
                    {
                        "validate": "list",
                        "source": ["Accept", "Reject"],
                        "input_title": "Is this the same document?",
                        "input_message": "Accept to pair it every month, Reject to never pair it.",
                    },
                )
                ws.conditional_format(
                    1,
                    0,
                    part.height,
                    0,
                    {
                        "type": "no_errors",
                        "format": self.fmt["decision"],
                    },
                )

    def summary(self, result: ReconResult, meta: dict[str, str]) -> None:
        ws = self.wb.add_worksheet("Summary")
        ws.set_column(0, 0, 26)
        ws.set_column(1, 1, 12)
        ws.set_column(2, 4, 17)
        ws.set_column(5, 5, 96)
        ws.write(0, 0, "GST reconciliation, books vs GSTR-2B", self.fmt["title"])
        r = 2
        for k, v in meta.items():
            ws.write(r, 0, k, self.fmt["muted"])
            ws.write(r, 1, v)
            r += 1
        r += 1
        heads = ["Status", "Documents", "ITC in books", "ITC in 2B", "ITC at risk", "What to do"]
        ws.write_row(r, 0, heads, self.fmt["head"])
        first = r + 1
        summary = result.summary()
        for rec in summary.iter_rows(named=True):
            r += 1
            s = str(rec["status"])
            ws.write(r, 0, LABELS[s], self.status_fmt[s])
            ws.write_number(r, 1, rec["documents"], self.fmt["int"])
            ws.write_number(r, 2, rec["itc_in_books"], self.fmt["money"])
            ws.write_number(r, 3, rec["itc_in_2b"], self.fmt["money"])
            ws.write_number(r, 4, rec["itc_at_risk"], self.fmt["money"])
            ws.write(r, 5, ACTIONS[s], self.fmt["wrap"])
        r += 1
        ws.write(r, 0, "Total", self.fmt["bold"])
        ws.write_formula(
            r, 1, f"=SUM(B{first + 1}:B{r})", self.fmt["bold_int"], int(summary["documents"].sum())
        )
        for c, colname in ((2, "itc_in_books"), (3, "itc_in_2b"), (4, "itc_at_risk")):
            letter = "ABCDE"[c]
            ws.write_formula(
                r,
                c,
                f"=SUM({letter}{first + 1}:{letter}{r})",
                self.fmt["bold_money"],
                float(summary[colname].sum()),
            )
        review = int(result.rows["needs_review"].sum())
        r += 2
        ws.write(r, 0, "Pairs to review", self.fmt["bold"])
        ws.write_number(r, 1, review, self.fmt["int"])
        ws.write(
            r,
            5,
            "Open the Review sheet, mark Accept or Reject, then run recon2b learn.",
            self.fmt["wrap"],
        )
        issues = result.gstin_issues.height
        if issues:
            r += 1
            ws.write(r, 0, "GSTINs not in 2B", self.fmt["bold"])
            ws.write_number(r, 1, issues, self.fmt["int"])
            ws.write(
                r,
                5,
                "See the GSTIN issues sheet. A typo here hides every invoice from that supplier.",
                self.fmt["wrap"],
            )
        r += 2
        ws.set_row(r, 32)
        ws.merge_range(
            r,
            0,
            r,
            5,
            "ITC at risk is credit the books claim that 2B does not "
            "support yet. It counts documents missing from 2B, the shortfall where "
            "amounts differ, and documents 2B marks as ITC not available. Credit "
            "notes count negative.",
            self.fmt["muted_wrap"],
        )
        ws.set_landscape()
        ws.fit_to_pages(1, 0)
        ws.activate()

    def close(self) -> None:
        self.wb.close()


def _cell(v: object) -> object:
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, float) and v != v:  # NaN
        return None
    if isinstance(v, dt.datetime):
        return v.date()
    return v


def write_excel(result: ReconResult, path: str | Path, meta: dict[str, str] | None = None) -> Path:
    path = Path(path)
    rows = _labelled(result.rows)
    w = _Writer(path)
    try:
        info = {
            "Run on": dt.date.today().strftime("%d-%m-%Y"),
            "Tolerance": f"Rs {result.config.tolerance:g} on taxable value and tax",
        }
        info.update(meta or {})
        w.summary(result, info)
        w.table("Reconciliation", rows, RECON_COLUMNS, status_col=True)
        review = (
            rows.filter(pl.col("needs_review"))
            .with_columns(pl.lit(None, pl.Utf8).alias("decision"))
            .sort("confidence")
        )
        w.table("Review", review, REVIEW_COLUMNS, hide_from=len(REVIEW_COLUMNS) - 4, decision=True)
        w.table("Suppliers", result.suppliers(), SUPPLIER_COLUMNS)
        if result.gstin_issues.height:
            w.table("GSTIN issues", result.gstin_issues, GSTIN_COLUMNS)
    finally:
        w.close()
    return path


def write_flat(result: ReconResult, path: str | Path) -> Path:
    """CSV or Parquet, for runs too big for Excel or for loading elsewhere."""
    path = Path(path)
    rows = _labelled(result.rows).with_columns(pl.col("status").cast(pl.Utf8))
    if path.suffix.lower() == ".parquet":
        rows.write_parquet(path)
    else:
        rows.write_csv(path)
    return path
