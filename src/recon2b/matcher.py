"""Pair purchase register rows with GSTR-2B rows and explain every difference.

The matcher runs passes from most certain to least certain. Each pass only sees
the rows earlier passes left unpaired, and every pair records which pass made it.

    pass                   pairs on                                 confidence
    manual                 a pair the accountant accepted before    1.00
    exact                  invoice no as typed, amounts agree       1.00
    normalised             normalised invoice no, amounts agree     0.95
    number                 last number in the invoice no            0.90
    fuzzy                  a similar invoice no                     0.60 to 0.90
    amount_date            the same amount within a few days        0.60
    exact_amount_off       invoice no as typed, amounts differ      0.90
    normalised_amount_off  normalised invoice no, amounts differ    0.85
    number_amount_off      last number and close date, amounts off  0.70

Pairs below 0.80 go to the Review sheet. Those are the ones where the invoice
number alone does not settle it.

Passes up to amount_date insist that taxable value and tax agree. Only after they
have run do we pair same-numbered documents whose amounts differ. Doing it in that
order matters: when a typo turns 304 into 340 and 340 also exists, pairing on the
number first would lock in two wrong pairs, while the amounts point to the right ones.

Every pass is a join on the supplier GSTIN first. That is the decision that keeps
the work close to linear: we never compare invoices from different suppliers.

Inside a pass a books row can have several candidates and the reverse. We keep a
pair only when each side is the other's cheapest candidate (mutual best), assign
those, and repeat on what is left. That gives a stable one-to-one result without
an expensive global assignment.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import polars as pl
from rapidfuzz import fuzz, process
from rapidfuzz.distance import Levenshtein

from .normalise import fiscal_year_starts, invoice_keys, is_valid_gstin
from .schema import CREDIT_NOTE

MATCHED = "MATCHED"
TAX_HEAD_MISMATCH = "TAX_HEAD_MISMATCH"
AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
MISSING_IN_2B = "MISSING_IN_2B"
MISSING_IN_BOOKS = "MISSING_IN_BOOKS"
STATUSES = [MATCHED, TAX_HEAD_MISMATCH, AMOUNT_MISMATCH, MISSING_IN_2B, MISSING_IN_BOOKS]
WORK_ORDER = [MISSING_IN_2B, AMOUNT_MISMATCH, TAX_HEAD_MISMATCH, MISSING_IN_BOOKS, MATCHED]


@dataclass
class MatchConfig:
    tolerance: float = 1.0  # rupees allowed on taxable value and on total tax
    key_date_window: int = 90  # days; stops a reused number from last year pairing
    date_window: int = 7  # days; for passes that rely on amounts instead of the number
    amount_date_window: int = 5  # days; for the amount-and-date pass
    fuzzy_threshold: float = 85.0  # rapidfuzz ratio, 0 to 100
    review_below: float = 0.8  # pairs below this confidence are sent for review
    max_fuzzy_pairs: int = 4_000_000  # per supplier; larger groups are blocked by amount


@dataclass
class Decisions:
    """Pairs the accountant confirmed or rejected in an earlier run."""

    accepted: pl.DataFrame = field(default_factory=lambda: _empty_decisions())
    rejected: pl.DataFrame = field(default_factory=lambda: _empty_decisions())


def _empty_decisions() -> pl.DataFrame:
    return pl.DataFrame(
        schema={"gstin": pl.Utf8, "doc_type": pl.Utf8, "b_norm": pl.Utf8, "t_norm": pl.Utf8}
    )


@dataclass
class ReconResult:
    rows: pl.DataFrame
    gstin_issues: pl.DataFrame
    timings: dict[str, float]
    config: MatchConfig

    def summary(self) -> pl.DataFrame:
        return status_summary(self.rows)

    def suppliers(self) -> pl.DataFrame:
        return supplier_summary(self.rows)


# ---------------------------------------------------------------------- prepare


def _prepare(df: pl.DataFrame, side: str, years: list[int]) -> pl.DataFrame:
    """Add match keys. ``side`` is 'b' for books or 't' for GSTR-2B."""
    id_col = "bid" if side == "b" else "tid"
    return df.with_columns(
        [pl.col("row").alias(id_col), *invoice_keys("inv_no", years)]
        + [(pl.col("igst") + pl.col("cgst") + pl.col("sgst") + pl.col("cess")).alias("tax")]
    )


_PAIR_COLS = ["inv_exact", "inv_norm", "inv_core", "inv_date", "taxable", "tax"]


def _side(df: pl.DataFrame, side: str) -> pl.DataFrame:
    id_col = "bid" if side == "b" else "tid"
    return df.select(
        [id_col, "gstin", "doc_type"] + [pl.col(c).alias(f"{side}_{c}") for c in _PAIR_COLS]
    )


# ------------------------------------------------------------------- assignment


def _cost() -> pl.Expr:
    taxable_gap = (pl.col("b_taxable") - pl.col("t_taxable")).abs() / pl.max_horizontal(
        pl.col("t_taxable").abs(), pl.lit(1.0)
    )
    tax_gap = (pl.col("b_tax") - pl.col("t_tax")).abs() / pl.max_horizontal(
        pl.col("t_tax").abs(), pl.lit(1.0)
    )
    return (taxable_gap + tax_gap + _date_gap().fill_null(30) / 30.0).alias("cost")


def _date_gap() -> pl.Expr:
    return (pl.col("b_inv_date") - pl.col("t_inv_date")).dt.total_days().abs()


def _amounts_agree(tol: float) -> pl.Expr:
    return ((pl.col("b_taxable") - pl.col("t_taxable")).abs() <= tol) & (
        (pl.col("b_tax") - pl.col("t_tax")).abs() <= tol
    )


def _within_days(days: int) -> pl.Expr:
    # an unknown date on either side does not block a match
    return _date_gap().is_null() | (_date_gap() <= days)


def _mutual_best(cands: pl.DataFrame, max_rounds: int = 50) -> pl.DataFrame:
    """One-to-one pairs where each side is the other's cheapest candidate."""
    out = []
    for _ in range(max_rounds):
        if cands.height == 0:
            break
        cands = cands.sort(["cost", "bid", "tid"])
        best_b = cands.unique("bid", keep="first", maintain_order=True)
        best_t = cands.unique("tid", keep="first", maintain_order=True).select("bid", "tid")
        mutual = best_b.join(best_t, on=["bid", "tid"], how="inner")
        if mutual.height == 0:
            break
        out.append(mutual)
        cands = cands.filter(
            ~pl.col("bid").is_in(mutual["bid"].implode())
            & ~pl.col("tid").is_in(mutual["tid"].implode())
        )
    if not out:
        return cands.clear()
    return pl.concat(out)


def _drop_rejected(cands: pl.DataFrame, rejected: pl.DataFrame) -> pl.DataFrame:
    if rejected.height == 0 or cands.height == 0:
        return cands
    return cands.join(
        rejected,
        left_on=["gstin", "doc_type", "b_inv_norm", "t_inv_norm"],
        right_on=["gstin", "doc_type", "b_norm", "t_norm"],
        how="anti",
    )


# ------------------------------------------------------------------------ passes


def _key_pass(b: pl.DataFrame, t: pl.DataFrame, key: str, filt: pl.Expr | None) -> pl.DataFrame:
    left = b.filter(pl.col(f"b_{key}").is_not_null() & (pl.col(f"b_{key}") != ""))
    right = t.filter(pl.col(f"t_{key}").is_not_null() & (pl.col(f"t_{key}") != ""))
    cands = left.join(
        right,
        left_on=["gstin", "doc_type", f"b_{key}"],
        right_on=["gstin", "doc_type", f"t_{key}"],
        how="inner",
        coalesce=False,
    ).drop("gstin_right", "doc_type_right")
    if filt is not None:
        cands = cands.filter(filt)
    return cands


def _manual_pass(b: pl.DataFrame, t: pl.DataFrame, accepted: pl.DataFrame) -> pl.DataFrame:
    if accepted.height == 0:
        return _key_pass(b.clear(), t.clear(), "inv_exact", None)
    left = b.join(
        accepted,
        left_on=["gstin", "doc_type", "b_inv_norm"],
        right_on=["gstin", "doc_type", "b_norm"],
        how="inner",
    )
    return left.join(
        t,
        left_on=["gstin", "doc_type", "t_norm"],
        right_on=["gstin", "doc_type", "t_inv_norm"],
        how="inner",
        coalesce=False,
    ).drop("gstin_right", "doc_type_right", "t_norm")


def _fuzzy_pass(b: pl.DataFrame, t: pl.DataFrame, cfg: MatchConfig) -> pl.DataFrame:
    """Compare invoice numbers by similarity, one supplier at a time."""
    groups_b = b.filter(pl.col("b_inv_norm") != "").partition_by(
        ["gstin", "doc_type"], as_dict=True
    )
    groups_t = t.filter(pl.col("t_inv_norm") != "").partition_by(
        ["gstin", "doc_type"], as_dict=True
    )
    pieces: list[pl.DataFrame] = []
    for key, gb in groups_b.items():
        gt = groups_t.get(key)
        if gt is None:
            continue
        if gb.height * gt.height > cfg.max_fuzzy_pairs:
            # Very large supplier: only compare invoices whose amounts are close.
            pieces.extend(_fuzzy_blocked(gb, gt, cfg))
        else:
            pieces.append(_fuzzy_block(gb, gt, cfg))
    pieces = [p for p in pieces if p.height]
    if not pieces:
        return _key_pass(b.clear(), t.clear(), "inv_norm", None).with_columns(
            pl.lit(0.0).alias("score")
        )
    cands = pl.concat(pieces)
    return cands.filter(_amounts_agree(cfg.tolerance) & _within_days(cfg.date_window))


def _fuzzy_block(gb: pl.DataFrame, gt: pl.DataFrame, cfg: MatchConfig) -> pl.DataFrame:
    scores = process.cdist(
        gb["b_inv_norm"].to_list(),
        gt["t_inv_norm"].to_list(),
        scorer=fuzz.ratio,
        score_cutoff=cfg.fuzzy_threshold,
        dtype="uint8",
        workers=1,
    )
    bi, ti = scores.nonzero()
    if len(bi) == 0:
        return (
            gb.head(0)
            .join(gt.head(0), on=["gstin", "doc_type"])
            .with_columns(pl.lit(0.0).alias("score"))
        )
    left = gb[bi.tolist()]
    right = gt[ti.tolist()].drop("gstin", "doc_type")
    return pl.concat([left, right], how="horizontal").with_columns(
        pl.Series("score", scores[bi, ti], dtype=pl.Float64)
    )


def _fuzzy_blocked(gb: pl.DataFrame, gt: pl.DataFrame, cfg: MatchConfig) -> list[pl.DataFrame]:
    step = max(cfg.tolerance, 1.0) * 2
    gb = gb.with_columns((pl.col("b_taxable") / step).floor().cast(pl.Int64).alias("_bucket"))
    gt = gt.with_columns((pl.col("t_taxable") / step).floor().cast(pl.Int64).alias("_bucket"))
    out = []
    by_t = gt.partition_by("_bucket", as_dict=True)
    for (bucket,), sub_b in gb.partition_by("_bucket", as_dict=True).items():
        near = [by_t[(k,)] for k in (bucket - 1, bucket, bucket + 1) if (k,) in by_t]
        if near:
            sub_t = pl.concat(near).drop("_bucket")
            out.append(_fuzzy_block(sub_b.drop("_bucket"), sub_t, cfg))
    return out


def _amount_date_pairs(b: pl.DataFrame, t: pl.DataFrame, cfg: MatchConfig) -> pl.DataFrame:
    step = max(cfg.tolerance, 1.0) * 2
    left = b.with_columns((pl.col("b_taxable") / step).floor().cast(pl.Int64).alias("_bucket"))
    right = t.with_columns(
        (
            (pl.col("t_taxable") / step).floor().cast(pl.Int64) + pl.Series([-1, 0, 1]).implode()
        ).alias("_bucket")
    ).explode("_bucket")
    cands = left.join(right, on=["gstin", "doc_type", "_bucket"], how="inner").drop("_bucket")
    return cands.filter(_amounts_agree(cfg.tolerance) & _within_days(cfg.amount_date_window))


def _amount_date_pass(
    b: pl.DataFrame, t: pl.DataFrame, b_all: pl.DataFrame, t_all: pl.DataFrame, cfg: MatchConfig
) -> pl.DataFrame:
    """Pair on amount and date alone, but only where that is unambiguous.

    Nothing about the invoice number backs these pairs up. So a pair is taken only
    when each side has exactly one document on the other side, matched or not, with
    that amount in that window. A supplier billing the same amount several times a
    week is left for the accountant instead of guessed.
    """
    cands = _amount_date_pairs(b, t, cfg)
    if cands.height == 0:
        return cands
    seen_by_b = _amount_date_pairs(b, t_all, cfg).group_by("bid").len("n_t")
    seen_by_t = _amount_date_pairs(b_all, t, cfg).group_by("tid").len("n_b")
    return (
        cands.join(seen_by_b, on="bid", how="left")
        .join(seen_by_t, on="tid", how="left")
        .filter((pl.col("n_t") == 1) & (pl.col("n_b") == 1))
        .drop("n_t", "n_b")
    )


# --------------------------------------------------------------------- reconcile


def reconcile(
    books: pl.DataFrame,
    gstr2b: pl.DataFrame,
    config: MatchConfig | None = None,
    decisions: Decisions | None = None,
) -> ReconResult:
    cfg = config or MatchConfig()
    dec = decisions or Decisions()
    timings: dict[str, float] = {}
    clock = time.perf_counter()

    years = fiscal_year_starts(pl.concat([books["inv_date"], gstr2b["inv_date"]]))
    B = _prepare(books, "b", years)
    T = _prepare(gstr2b, "t", years)
    b_all, t_all = _side(B, "b"), _side(T, "t")
    b_left, t_left = b_all, t_all
    timings["prepare"] = time.perf_counter() - clock

    agree = _amounts_agree(cfg.tolerance)
    near_key = _within_days(cfg.key_date_window)
    near = _within_days(cfg.date_window)
    passes = [
        ("manual", 1.0, lambda b, t: _manual_pass(b, t, dec.accepted)),
        ("exact", 1.0, lambda b, t: _key_pass(b, t, "inv_exact", agree & near_key)),
        ("normalised", 0.95, lambda b, t: _key_pass(b, t, "inv_norm", agree & near_key)),
        ("number", 0.9, lambda b, t: _key_pass(b, t, "inv_core", agree & near)),
        ("fuzzy", None, lambda b, t: _fuzzy_pass(b, t, cfg)),
        ("amount_date", 0.6, lambda b, t: _amount_date_pass(b, t, b_all, t_all, cfg)),
        ("exact_amount_off", 0.9, lambda b, t: _key_pass(b, t, "inv_exact", near_key)),
        ("normalised_amount_off", 0.85, lambda b, t: _key_pass(b, t, "inv_norm", near_key)),
        ("number_amount_off", 0.7, lambda b, t: _key_pass(b, t, "inv_core", near)),
    ]

    pairs: list[pl.DataFrame] = []
    for name, confidence, run in passes:
        clock = time.perf_counter()
        if b_left.height == 0 or t_left.height == 0:
            break
        cands = run(b_left, t_left)
        if name != "manual":
            cands = _drop_rejected(cands, dec.rejected)
        if cands.height:
            cands = cands.with_columns(_cost())
            if name == "fuzzy":
                # better similarity should win, so it lowers the cost
                cands = cands.with_columns(pl.col("cost") + (100 - pl.col("score")) / 100)
            chosen = _mutual_best(cands)
            if name == "fuzzy":
                span = 100 - cfg.fuzzy_threshold or 1
                conf = (0.6 + 0.3 * (pl.col("score") - cfg.fuzzy_threshold) / span).round(2)
            else:
                conf = pl.lit(confidence)
            chosen = chosen.select(
                "bid", "tid", pl.lit(name).alias("match_pass"), conf.alias("confidence")
            )
            pairs.append(chosen)
            b_left = b_left.filter(~pl.col("bid").is_in(chosen["bid"].implode()))
            t_left = t_left.filter(~pl.col("tid").is_in(chosen["tid"].implode()))
        timings[f"pass_{name}"] = time.perf_counter() - clock

    clock = time.perf_counter()
    rows = _assemble(B, T, pairs, b_left["bid"], t_left["tid"], cfg)
    timings["assemble"] = time.perf_counter() - clock
    clock = time.perf_counter()
    issues = gstin_issues(B, T)
    timings["gstin_check"] = time.perf_counter() - clock
    return ReconResult(rows=rows, gstin_issues=issues, timings=timings, config=cfg)


# ---------------------------------------------------------------------- assemble

_DETAIL = [
    "row",
    "inv_no",
    "inv_norm",
    "inv_date",
    "taxable",
    "igst",
    "cgst",
    "sgst",
    "cess",
    "tax",
    "inv_value",
]


def _prefixed(df: pl.DataFrame, id_col: str, prefix: str, extra: list[str]) -> pl.DataFrame:
    return df.select(
        [id_col, "gstin", "doc_type", "supplier_name"]
        + [pl.col(c).alias(f"{prefix}_{c}") for c in _DETAIL + extra]
    )


def _assemble(
    B: pl.DataFrame,
    T: pl.DataFrame,
    pairs: list[pl.DataFrame],
    b_unmatched: pl.Series,
    t_unmatched: pl.Series,
    cfg: MatchConfig,
) -> pl.DataFrame:
    dup = B.with_columns(
        (pl.len().over(["gstin", "doc_type", "inv_norm", pl.col("taxable").round(0)]) > 1).alias(
            "dup"
        )
    )
    bx = _prefixed(dup, "bid", "books", ["dup"])
    tx = _prefixed(T, "tid", "gstr2b", ["itc_available", "reverse_charge"])

    if pairs:
        paired = pl.concat(pairs)
    else:
        paired = pl.DataFrame(
            schema={
                "bid": pl.Int64,
                "tid": pl.Int64,
                "match_pass": pl.Utf8,
                "confidence": pl.Float64,
            }
        )
    matched = (
        paired.join(bx, on="bid", how="left")
        .join(
            tx.rename({"gstin": "t_gstin", "doc_type": "t_doc_type"}),
            on="tid",
            how="left",
            suffix="_2b",
        )
        .with_columns(
            pl.coalesce("supplier_name_2b", "supplier_name").alias("supplier_name"),
        )
        .drop("supplier_name_2b", "t_gstin", "t_doc_type")
    )
    only_books = bx.filter(pl.col("bid").is_in(b_unmatched.implode())).with_columns(
        pl.lit(None, pl.Int64).alias("tid")
    )
    only_2b = tx.filter(pl.col("tid").is_in(t_unmatched.implode())).with_columns(
        pl.lit(None, pl.Int64).alias("bid")
    )
    rows = pl.concat([matched, only_books, only_2b], how="diagonal_relaxed")

    tol = cfg.tolerance
    has_b = pl.col("bid").is_not_null()
    has_t = pl.col("tid").is_not_null()
    taxable_diff = pl.col("books_taxable") - pl.col("gstr2b_taxable")
    tax_diff = pl.col("books_tax") - pl.col("gstr2b_tax")
    igst_diff = pl.col("books_igst") - pl.col("gstr2b_igst")
    status = (
        pl.when(~has_t)
        .then(pl.lit(MISSING_IN_2B))
        .when(~has_b)
        .then(pl.lit(MISSING_IN_BOOKS))
        .when((taxable_diff.abs() > tol) | (tax_diff.abs() > tol))
        .then(pl.lit(AMOUNT_MISMATCH))
        .when(igst_diff.abs() > tol)
        .then(pl.lit(TAX_HEAD_MISMATCH))
        .otherwise(pl.lit(MATCHED))
    )
    date_gap = (pl.col("books_inv_date") - pl.col("gstr2b_inv_date")).dt.total_days()
    remark_parts = [
        pl.when(
            has_b
            & has_t
            & (
                pl.col("books_inv_no").str.strip_chars().str.to_uppercase()
                != pl.col("gstr2b_inv_no").str.strip_chars().str.to_uppercase()
            )
        ).then(pl.lit("invoice number written differently")),
        pl.when(date_gap.is_not_null() & (date_gap != 0)).then(
            pl.format("date differs by {} days", date_gap.abs())
        ),
        pl.when(status == TAX_HEAD_MISMATCH).then(
            pl.lit("IGST charged instead of CGST and SGST or the reverse, check place of supply")
        ),
        pl.when(pl.col("gstr2b_itc_available") == False).then(  # noqa: E712
            pl.lit("ITC not available as per 2B")
        ),
        pl.when(pl.col("gstr2b_reverse_charge") == True).then(pl.lit("reverse charge")),  # noqa: E712
        pl.when(pl.col("books_dup") == True).then(pl.lit("possible duplicate entry in books")),  # noqa: E712
        pl.when(status == MISSING_IN_2B).then(
            pl.lit("supplier has not filed it, filed late, or used another GSTIN")
        ),
        pl.when(status == MISSING_IN_BOOKS).then(
            pl.lit("not recorded in books, or recorded under another number or GSTIN")
        ),
    ]
    sign = pl.when(pl.col("doc_type") == CREDIT_NOTE).then(-1.0).otherwise(1.0)
    rows = rows.with_columns(
        status.alias("status"),
        pl.col("match_pass").fill_null("unmatched"),
        pl.col("confidence").fill_null(0.0),
        taxable_diff.round(2).alias("taxable_diff"),
        tax_diff.round(2).alias("tax_diff"),
        date_gap.alias("date_diff_days"),
        pl.concat_list(remark_parts).list.drop_nulls().list.join("; ").alias("remarks"),
        (sign * pl.col("books_tax").fill_null(0)).round(2).alias("itc_books"),
        (sign * pl.col("gstr2b_tax").fill_null(0)).round(2).alias("itc_2b"),
    ).with_columns(
        (has_b & has_t & (pl.col("confidence") < cfg.review_below)).alias("needs_review"),
        # credit that the books claim but 2B does not support yet
        pl.when(pl.col("status") == MISSING_IN_2B)
        .then(pl.col("itc_books"))
        .when(pl.col("status") == AMOUNT_MISMATCH)
        .then(pl.max_horizontal(pl.col("itc_books") - pl.col("itc_2b"), pl.lit(0.0)))
        .when(pl.col("gstr2b_itc_available") == False)  # noqa: E712
        .then(pl.col("itc_books"))
        .otherwise(0.0)
        .round(2)
        .alias("itc_at_risk"),
    )
    # problems first, in the order an accountant works through them
    priority = {s: i for i, s in enumerate(WORK_ORDER)}
    return (
        rows.with_columns(
            pl.col("status").replace_strict(priority, return_dtype=pl.Int8).alias("_order"),
            pl.col("status").cast(pl.Enum(STATUSES)).alias("status"),
        )
        .sort(["_order", "gstin", "books_inv_date", "gstr2b_inv_date"], nulls_last=True)
        .select(RESULT_COLUMNS)
    )


RESULT_COLUMNS = [
    "status",
    "match_pass",
    "confidence",
    "needs_review",
    "gstin",
    "supplier_name",
    "doc_type",
    "books_row",
    "books_inv_no",
    "books_inv_date",
    "books_taxable",
    "books_igst",
    "books_cgst",
    "books_sgst",
    "books_cess",
    "books_tax",
    "gstr2b_row",
    "gstr2b_inv_no",
    "gstr2b_inv_date",
    "gstr2b_taxable",
    "gstr2b_igst",
    "gstr2b_cgst",
    "gstr2b_sgst",
    "gstr2b_cess",
    "gstr2b_tax",
    "taxable_diff",
    "tax_diff",
    "date_diff_days",
    "itc_books",
    "itc_2b",
    "itc_at_risk",
    "gstr2b_itc_available",
    "remarks",
    "books_inv_norm",
    "gstr2b_inv_norm",
]


# ------------------------------------------------------------------- summaries


def status_summary(rows: pl.DataFrame) -> pl.DataFrame:
    base = pl.DataFrame({"status": pl.Series(STATUSES).cast(pl.Enum(STATUSES))})
    agg = rows.group_by("status").agg(
        pl.len().alias("documents"),
        pl.col("itc_books").sum().round(2).alias("itc_in_books"),
        pl.col("itc_2b").sum().round(2).alias("itc_in_2b"),
        pl.col("itc_at_risk").sum().round(2).alias("itc_at_risk"),
    )
    return base.join(agg, on="status", how="left").fill_null(0)


def supplier_summary(rows: pl.DataFrame) -> pl.DataFrame:
    return (
        rows.group_by("gstin")
        .agg(
            pl.col("supplier_name").drop_nulls().first(),
            pl.len().alias("documents"),
            (pl.col("status") == MATCHED).sum().alias("matched"),
            (pl.col("status") != MATCHED).sum().alias("issues"),
            pl.col("itc_books").sum().round(2).alias("itc_in_books"),
            pl.col("itc_2b").sum().round(2).alias("itc_in_2b"),
            pl.col("itc_at_risk").sum().round(2).alias("itc_at_risk"),
        )
        .sort(["itc_at_risk", "issues"], descending=True)
    )


def gstin_issues(B: pl.DataFrame, T: pl.DataFrame) -> pl.DataFrame:
    """Books GSTINs that never appear in 2B, with the likely intended GSTIN."""
    portal = T["gstin"].drop_nulls().unique().to_list()
    portal_set = set(portal)
    stats = (
        B.filter(~pl.col("gstin").is_in(list(portal_set)))
        .group_by("gstin")
        .agg(
            pl.col("supplier_name").drop_nulls().first(),
            pl.len().alias("documents"),
            pl.col("tax").sum().round(2).alias("tax_in_books"),
        )
    )
    rows = []
    by_state: dict[str, list[str]] = {}
    for g in portal:
        by_state.setdefault(g[:2], []).append(g)
    for rec in stats.iter_rows(named=True):
        g = rec["gstin"] or ""
        valid = is_valid_gstin(g)
        suggestion, distance = None, None
        pool = by_state.get(g[:2]) or portal
        if pool:
            hit = process.extractOne(g, pool, scorer=Levenshtein.distance, score_cutoff=2)
            if hit:
                suggestion, distance = hit[0], int(hit[1])
        problem = (
            "GSTIN is not valid (checksum or format)"
            if not valid
            else "valid GSTIN but supplier not in 2B"
        )
        rows.append(
            {
                **rec,
                "valid": valid,
                "problem": problem,
                "did_you_mean": suggestion,
                "edits": distance,
            }
        )
    schema = {
        "gstin": pl.Utf8,
        "supplier_name": pl.Utf8,
        "documents": pl.UInt32,
        "tax_in_books": pl.Float64,
        "valid": pl.Boolean,
        "problem": pl.Utf8,
        "did_you_mean": pl.Utf8,
        "edits": pl.Int64,
    }
    return pl.DataFrame(rows, schema=schema).sort("tax_in_books", descending=True)
