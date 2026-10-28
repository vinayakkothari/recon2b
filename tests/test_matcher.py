import polars as pl

from conftest import SUP_A, SUP_B, doc, frame
from recon2b.matcher import (
    AMOUNT_MISMATCH,
    MATCHED,
    MISSING_IN_2B,
    MISSING_IN_BOOKS,
    TAX_HEAD_MISMATCH,
    Decisions,
    MatchConfig,
    reconcile,
)


def run(books, portal, **kw):
    return reconcile(frame(books), frame(portal), MatchConfig(**kw)).rows


def pair_of(rows, books_no):
    hit = rows.filter(pl.col("books_inv_no") == books_no)
    assert hit.height == 1, hit
    return hit.row(0, named=True)


def test_exact_match():
    rows = run([doc("INV/26-27/0012", 1000)], [doc("INV/26-27/0012", 1000)])
    r = pair_of(rows, "INV/26-27/0012")
    assert r["status"] == MATCHED and r["match_pass"] == "exact" and r["confidence"] == 1.0
    assert r["itc_at_risk"] == 0


def test_written_differently_is_normalised():
    rows = run([doc("inv 12", 1000)], [doc("INV/26-27/0012", 1000)])
    r = pair_of(rows, "inv 12")
    assert r["status"] == MATCHED and r["match_pass"] == "normalised"
    assert "written differently" in r["remarks"]


def test_number_only_needs_amounts_to_agree():
    rows = run([doc("12", 1000)], [doc("INV/26-27/0012", 1000)])
    assert pair_of(rows, "12")["match_pass"] == "number"
    # with a different amount and nothing else to go on, it stays a low-confidence pair
    rows = run([doc("12", 1000)], [doc("INV/26-27/0012", 5000)])
    r = pair_of(rows, "12")
    assert r["match_pass"] == "number_amount_off" and r["status"] == AMOUNT_MISMATCH


def test_typo_found_by_fuzzy_pass():
    rows = run([doc("SI-004517", 1000)], [doc("SI-004571", 1000), doc("SI-009999", 1000, day=28)])
    r = pair_of(rows, "SI-004517")
    assert r["gstr2b_inv_no"] == "SI-004571"
    assert r["match_pass"] in ("fuzzy", "amount_date")


def test_swapped_typo_does_not_lock_in_wrong_pairs():
    # books typed 304 as 340 and 340 as 304. The numbers point the wrong way,
    # the amounts point the right way.
    books = [doc("340", 1000, day=3), doc("304", 2000, day=24)]
    portal = [doc("304", 1000, day=3), doc("340", 2000, day=24)]
    rows = run(books, portal)
    assert pair_of(rows, "340")["gstr2b_inv_no"] == "304"
    assert pair_of(rows, "304")["gstr2b_inv_no"] == "340"


def test_amount_mismatch_on_exact_number():
    rows = run([doc("A1", 1000)], [doc("A1", 1200)])
    r = pair_of(rows, "A1")
    assert r["status"] == AMOUNT_MISMATCH and r["match_pass"] == "exact_amount_off"
    assert r["taxable_diff"] == -200
    assert r["itc_at_risk"] == 0  # the books claim less than 2B allows


def test_amount_mismatch_puts_shortfall_at_risk():
    rows = run([doc("A1", 1200)], [doc("A1", 1000)])
    assert pair_of(rows, "A1")["itc_at_risk"] == 36.0  # 18% of the extra 200


def test_tax_head_mismatch():
    rows = run([doc("A1", 1000, inter=False)], [doc("A1", 1000, inter=True)])
    r = pair_of(rows, "A1")
    assert r["status"] == TAX_HEAD_MISMATCH
    assert "place of supply" in r["remarks"]


def test_rounding_within_tolerance():
    b = doc("A1", 1000.40)
    rows = run([b], [doc("A1", 1000)])
    assert pair_of(rows, "A1")["status"] == MATCHED
    rows = run([b], [doc("A1", 1000)], tolerance=0.1)
    assert pair_of(rows, "A1")["status"] == AMOUNT_MISMATCH


def test_missing_on_each_side():
    rows = run([doc("B1", 1000)], [doc("C9", 5000, day=28)])
    statuses = sorted(rows["status"].cast(pl.Utf8).to_list())
    assert statuses == [MISSING_IN_2B, MISSING_IN_BOOKS]
    missing = rows.filter(pl.col("status") == MISSING_IN_2B).row(0, named=True)
    assert missing["itc_at_risk"] == 180.0


def test_never_pairs_across_suppliers_or_doc_types():
    books = [doc("A1", 1000, gstin_=SUP_A), doc("CN1", 500, doc_type="CRN")]
    portal = [doc("A1", 1000, gstin_=SUP_B), doc("CN1", 500, doc_type="INV")]
    rows = run(books, portal)
    assert rows.filter(pl.col("status") == MATCHED).height == 0


def test_last_years_invoice_with_same_number_is_not_paired():
    books = [doc("INV/25-26/0005", 1000, year=2025, month=10)]
    portal = [doc("INV/26-27/0005", 4000, year=2026, month=10)]
    rows = run(books, portal)
    assert set(rows["status"].cast(pl.Utf8)) == {MISSING_IN_2B, MISSING_IN_BOOKS}


def test_duplicate_in_books_flagged():
    books = [doc("A1", 1000), doc("A1", 1000)]
    rows = run(books, [doc("A1", 1000)])
    assert rows.filter(pl.col("status") == MATCHED).height == 1
    extra = rows.filter(pl.col("status") == MISSING_IN_2B).row(0, named=True)
    assert "duplicate" in extra["remarks"]


def test_itc_not_available_is_at_risk_even_when_matched():
    rows = run([doc("A1", 1000)], [doc("A1", 1000, itc=False)])
    r = pair_of(rows, "A1")
    assert r["status"] == MATCHED and r["itc_at_risk"] == 180.0
    assert "ITC not available" in r["remarks"]


def test_credit_notes_count_negative():
    rows = run([doc("CN1", 1000, doc_type="CRN")], [doc("CN1", 1000, doc_type="CRN")])
    r = pair_of(rows, "CN1")
    assert r["itc_books"] == -180.0 and r["itc_2b"] == -180.0


def test_saved_decisions_are_applied():
    books = [doc("XYZ", 1000, day=1)]
    portal = [doc("Q-77", 1000, day=20)]  # nothing in common but the amount
    plain = reconcile(frame(books), frame(portal)).rows
    assert MATCHED not in plain["status"].cast(pl.Utf8).to_list()

    def decisions(column):
        d = pl.DataFrame(
            {"gstin": [SUP_A], "doc_type": ["INV"], "b_norm": ["XYZ"], "t_norm": ["Q77"]}
        )
        return Decisions(**{column: d})

    accepted = reconcile(frame(books), frame(portal), decisions=decisions("accepted")).rows
    r = pair_of(accepted, "XYZ")
    assert r["match_pass"] == "manual" and r["status"] == MATCHED

    near = [doc("XYZ", 1000, day=20)]
    rejected = reconcile(frame(near), frame(portal), decisions=decisions("rejected")).rows
    assert rejected.filter(pl.col("status") == MATCHED).height == 0


def test_low_confidence_pairs_need_review():
    rows = run([doc("AAA", 1000, day=10)], [doc("ZZZ", 1000, day=12)])
    r = pair_of(rows, "AAA")
    assert r["match_pass"] == "amount_date" and r["needs_review"]


def test_gstin_typo_is_reported_with_suggestion(make_frame):
    typo = SUP_A[:5] + ("X" if SUP_A[5] != "X" else "Y") + SUP_A[6:]
    res = reconcile(make_frame([doc("A1", 1000, gstin_=typo)]), make_frame([doc("A1", 1000)]))
    issue = res.gstin_issues.row(0, named=True)
    assert issue["gstin"] == typo and not issue["valid"]
    assert issue["did_you_mean"] == SUP_A and issue["edits"] == 1


def test_empty_inputs(make_frame):
    res = reconcile(make_frame([doc("A1", 1000)]), make_frame([doc("A1", 1)]).clear())
    assert res.rows["status"].cast(pl.Utf8).to_list() == [MISSING_IN_2B]
    assert res.summary()["documents"].sum() == 1
