import polars as pl
import pytest

from recon2b.normalise import (
    fiscal_year_starts,
    gstin_check_char,
    invoice_keys,
    is_valid_gstin,
    parse_amount,
    parse_dates,
)


def keys(values, years=None):
    df = pl.DataFrame({"inv_no": values}).with_columns(invoice_keys("inv_no", years))
    return list(zip(df["inv_norm"], df["inv_core"], strict=True))


@pytest.mark.parametrize(
    "raw, norm, core",
    [
        ("INV/24-25/0012", "INV12", "12"),
        ("inv-12", "INV12", "12"),
        ("12", "12", "12"),
        ("2024-25/001", "1", "1"),
        ("GST/2024-2025/77", "GST77", "77"),
        ("A12/24-25/5", "A125", "125"),  # the year is the second number pair
        ("26-27/INV/9", "INV9", "9"),
        ("SI-00340", "SI340", "340"),
        ("  tx 000123 ", "TX123", "123"),
        ("0007", "7", "7"),
    ],
)
def test_invoice_number_keys(raw, norm, core):
    assert keys([raw]) == [(norm, core)]


def test_non_consecutive_pair_is_not_a_year():
    # 12-40 is an invoice number, not a financial year
    assert keys(["12-40"]) == [("1240", "1240")]


def test_number_that_is_only_a_year_is_kept():
    assert keys(["24-25"]) == [("2425", "2425")]


def test_leading_zeros_stripped_per_number():
    assert keys(["12/0007"]) == keys(["12/7"])


def test_glued_year_removed_only_for_years_in_play():
    assert keys(["INV26270535"], [2026]) == [("INV535", "535")]
    assert keys(["GST202627792"], [2026]) == [("GST792", "792")]
    # without knowing the year, 2627 could be part of the serial
    assert keys(["INV26270535"]) == [("INV26270535", "26270535")]
    # a glued year needs a serial after it
    assert keys(["INV2627"], [2026]) == [("INV2627", "2627")]


def test_null_invoice_number():
    assert keys([None]) == [("", None)]


def test_fiscal_years_from_dates():
    # 31 March 2026 belongs to 2025-26 and 1 April 2026 to 2026-27; one year back is added
    s = pl.Series(["2026-03-31", "2026-04-01"]).str.to_date()
    assert fiscal_year_starts(s) == [2024, 2025, 2026]
    assert fiscal_year_starts(pl.Series([None], dtype=pl.Date)) == []


def test_gstin_checksum():
    first14 = "27AAPFU0939F1Z"
    assert gstin_check_char(first14) == "V"
    assert is_valid_gstin("27AAPFU0939F1ZV")
    assert not is_valid_gstin("27AAPFU0939F1ZX")  # wrong check character
    assert not is_valid_gstin("27AAPFU0939F1Z")  # too short
    assert not is_valid_gstin(None)


def test_dates_in_many_formats():
    s = pl.Series(
        "d",
        [
            "05-10-2026",
            "05/10/26",
            "2026-10-05",
            "5-Oct-2026",
            "05-OCT-26",
            "2026-10-05 00:00:00",
            "5.10.2026",
            None,
            "rubbish",
        ],
    )
    out = parse_dates(s).to_list()
    assert all(str(d) == "2026-10-05" for d in out[:7])
    assert out[7:] == [None, None]


def test_amounts():
    df = pl.DataFrame({"a": ["1,23,456.50", "₹ 500", "(120.50)", "-", "", None, "7"]})
    out = df.select(parse_amount("a"))["a"].to_list()
    assert out == [123456.5, 500.0, -120.5, None, None, None, 7.0]
