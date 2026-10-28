from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from recon2b.normalise import gstin_check_char
from recon2b.schema import conform


def gstin(state: str = "29", pan: str = "ABCDE1234F") -> str:
    first14 = f"{state}{pan}1Z"
    return first14 + gstin_check_char(first14)


SUP_A = gstin("29", "AAACA1111A")
SUP_B = gstin("27", "BBBCB2222B")


def doc(
    inv_no: str,
    taxable: float,
    *,
    gstin_: str = SUP_A,
    day: int = 10,
    month: int = 10,
    year: int = 2026,
    rate: float = 0.18,
    inter: bool = False,
    doc_type: str = "INV",
    itc: bool = True,
) -> dict:
    tax = round(taxable * rate, 2)
    half = round(tax / 2, 2)
    return {
        "gstin": gstin_,
        "supplier_name": "Test Supplier",
        "doc_type": doc_type,
        "inv_no": inv_no,
        "inv_date": dt.date(year, month, day),
        "taxable": taxable,
        "igst": tax if inter else 0.0,
        "cgst": 0.0 if inter else half,
        "sgst": 0.0 if inter else round(tax - half, 2),
        "cess": 0.0,
        "inv_value": taxable + tax,
        "itc_available": itc,
    }


def frame(docs: list[dict]) -> pl.DataFrame:
    return conform(pl.DataFrame(docs))


@pytest.fixture
def make_frame():
    return frame
