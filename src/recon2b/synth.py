"""Generate realistic books and GSTR-2B files with known answers.

Real client data is confidential, so tests and benchmarks run on synthetic data
that copies the mess seen in practice: invoice numbers typed differently, dates a
few days off, rounding, wrong tax heads, duplicates, suppliers who never filed and
GSTIN typos. Every document carries a hidden ``true_id`` so we can measure how
often the matcher pairs the right rows.
"""

from __future__ import annotations

import calendar
import datetime as dt
import json
import string
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl

from .normalise import gstin_check_char
from .schema import CREDIT_NOTE, DEBIT_NOTE, INVOICE

STATE_CODES = ["29", "29", "29", "27", "33", "07", "24", "36", "32", "09", "06", "19"]
RECIPIENT_STATE = "29"  # Karnataka
NAME_A = [
    "Shree",
    "Sri",
    "Lakshmi",
    "Ganesh",
    "Balaji",
    "National",
    "Coastal",
    "Deccan",
    "Kaveri",
    "Royal",
    "Vijay",
    "Navya",
    "Apex",
    "Prime",
    "Sai",
    "Om",
    "Indus",
    "Metro",
    "Unity",
    "Pioneer",
]
NAME_B = [
    "Traders",
    "Enterprises",
    "Agencies",
    "Industries",
    "Distributors",
    "Steels",
    "Polymers",
    "Logistics",
    "Electricals",
    "Pharma",
    "Textiles",
    "Foods",
    "Packaging",
    "Hardware",
    "Infotech",
]
NAME_C = ["", "", "Pvt Ltd", "LLP", "& Co", "Private Limited"]

FORMATS = [
    "INV/{fy}/{n:04d}",
    "{n}",
    "SI-{n:05d}",
    "GST/{fy4}/{n}",
    "TX{n:06d}",
    "B2B/{n}",
    "{fy4}/{n:03d}",
    "{pfx}{n:04d}",
]
NOTE_FORMATS = ["CN/{fy}/{n:03d}", "CRN-{n:04d}", "DN/{fy}/{n:03d}"]


@dataclass
class Noise:
    missing_in_2b: float = 0.04
    missing_in_books: float = 0.03
    cosmetic_number: float = 0.20  # case, spacing, separators
    stripped_number: float = 0.10  # financial year and leading zeros dropped
    numeric_only: float = 0.08  # just the serial number
    typo_number: float = 0.04  # one character wrong, missing or swapped
    date_shift: float = 0.08
    rounding: float = 0.10
    amount_mismatch: float = 0.02
    tax_head_swap: float = 0.01
    itc_unavailable: float = 0.01
    duplicate_in_books: float = 0.005
    gstin_typo_suppliers: float = 0.002


def _gstin(rng: np.random.Generator, state: str) -> str:
    letters = string.ascii_uppercase
    pan = (
        "".join(rng.choice(list(letters), 3))
        + rng.choice(list("CPFHT"))
        + rng.choice(list(letters))
        + "".join(rng.choice(list(string.digits), 4))
        + rng.choice(list(letters))
    )
    first14 = f"{state}{pan}1Z"
    return first14 + gstin_check_char(first14)


def _typo_gstin(rng: np.random.Generator, g: str) -> str:
    i = int(rng.integers(2, 12))
    pool = string.digits if g[i].isdigit() else string.ascii_uppercase
    new = g[i]
    while new == g[i]:
        new = str(rng.choice(list(pool)))
    return g[:i] + new + g[i + 1 :]


def _typo(rng: np.random.Generator, s: str) -> str:
    digits = [i for i, c in enumerate(s) if c.isdigit()]
    if not digits:
        return s + "1"
    kind = rng.integers(0, 3)
    i = int(rng.choice(digits))
    if kind == 0:  # wrong digit
        return s[:i] + str((int(s[i]) + int(rng.integers(1, 9))) % 10) + s[i + 1 :]
    if kind == 1 and len(s) > 2:  # dropped character
        return s[:i] + s[i + 1 :]
    j = i + 1 if i + 1 < len(s) else i - 1  # swapped neighbours
    a, b = sorted((i, j))
    return s[:a] + s[b] + s[a] + s[b + 1 :]


def _cosmetic(rng: np.random.Generator, s: str) -> str:
    choice = rng.integers(0, 4)
    if choice == 0:
        return s.lower()
    if choice == 1:
        return s.replace("/", "-")
    if choice == 2:
        return s.replace("/", " / ")
    return s.replace("/", "").replace("-", "")


def generate(
    n_invoices: int = 10_000,
    n_suppliers: int | None = None,
    seed: int = 7,
    year: int = 2026,
    month: int = 10,
    noise: Noise | None = None,
) -> tuple[pl.DataFrame, dict, pl.DataFrame, pl.DataFrame]:
    """Return (books, gstr2b_json, truth_books, truth_2b)."""
    noise = noise or Noise()
    rng = np.random.default_rng(seed)
    n_suppliers = n_suppliers or max(5, n_invoices // 50)
    fy_start = year if month >= 4 else year - 1
    fy, fy4 = (
        f"{fy_start % 100:02d}-{(fy_start + 1) % 100:02d}",
        f"{fy_start}-{(fy_start + 1) % 100:02d}",
    )
    days_in_month = calendar.monthrange(year, month)[1]

    # suppliers, with a long tail: a few send thousands of invoices, most send a handful
    sup_states = rng.choice(STATE_CODES, n_suppliers)
    sup_gstin = [_gstin(rng, s) for s in sup_states]
    sup_name = [
        " ".join(filter(None, [rng.choice(NAME_A), rng.choice(NAME_B), rng.choice(NAME_C)]))
        for _ in range(n_suppliers)
    ]
    sup_fmt = rng.integers(0, len(FORMATS), n_suppliers)
    sup_pfx = ["".join(rng.choice(list(string.ascii_uppercase), 2)) for _ in range(n_suppliers)]
    weights = 1.0 / np.arange(1, n_suppliers + 1) ** 1.05
    sup_of = np.sort(rng.choice(n_suppliers, n_invoices, p=weights / weights.sum()))
    typo_sup = set(
        rng.choice(n_suppliers, int(n_suppliers * noise.gstin_typo_suppliers), replace=False)
    )
    books_gstin_of = {
        s: (_typo_gstin(rng, sup_gstin[s]) if s in typo_sup else sup_gstin[s])
        for s in range(n_suppliers)
    }

    kind = rng.random(n_invoices)
    doc_type = np.where(kind < 0.05, CREDIT_NOTE, np.where(kind < 0.06, DEBIT_NOTE, INVOICE))
    day = rng.integers(1, days_in_month + 1, n_invoices)
    taxable = np.round(np.exp(rng.normal(9.6, 1.2, n_invoices)), 2)
    rate = rng.choice([5, 12, 18, 28], n_invoices, p=[0.2, 0.2, 0.5, 0.1])
    tax = np.round(taxable * rate / 100, 2)
    inter = np.array([sup_states[s] != RECIPIENT_STATE for s in sup_of])
    start = rng.integers(1, 400, n_suppliers)

    r = {
        k: rng.random(n_invoices)
        for k in ("m2b", "mbk", "num", "date", "rnd", "amt", "head", "itc", "dup")
    }
    in_2b = r["m2b"] >= noise.missing_in_2b
    in_books = (r["mbk"] >= noise.missing_in_books) | ~in_2b

    serial: dict[tuple[int, str], int] = {}
    portal_no, books_no = [], []
    c1 = noise.cosmetic_number
    c2 = c1 + noise.stripped_number
    c3 = c2 + noise.numeric_only
    c4 = c3 + noise.typo_number
    for i in range(n_invoices):
        s = int(sup_of[i])
        dtype = doc_type[i]
        key = (s, dtype)
        serial[key] = serial.get(key, int(start[s])) + 1
        n = serial[key]
        if dtype == INVOICE:
            no = FORMATS[sup_fmt[s]].format(n=n, fy=fy, fy4=fy4, pfx=sup_pfx[s])
        else:
            tmpl = NOTE_FORMATS[2] if dtype == DEBIT_NOTE else NOTE_FORMATS[s % 2]
            no = tmpl.format(n=n, fy=fy)
        portal_no.append(no)
        x = r["num"][i]
        if x < c1:
            bno = _cosmetic(rng, no)
        elif x < c2:
            bno = no.replace(f"/{fy}", "").replace(f"/{fy4}", "").replace(f"{fy4}/", "")
            bno = "".join(ch for ch in bno if ch.isalnum()).lstrip("0") or no
        elif x < c3:
            bno = str(n)
        elif x < c4:
            bno = _typo(rng, no)
        else:
            bno = no
        books_no.append(bno)

    # books side amounts and dates, with noise
    b_taxable = taxable.copy()
    b_tax = tax.copy()
    rnd = r["rnd"] < noise.rounding
    b_taxable[rnd] += np.round(rng.uniform(-0.99, 0.99, rnd.sum()), 2)
    b_tax[rnd] += np.round(rng.uniform(-0.49, 0.49, rnd.sum()), 2)
    amt = r["amt"] < noise.amount_mismatch
    factor = 1 + rng.choice([-1, 1], amt.sum()) * rng.uniform(0.03, 0.2, amt.sum())
    b_taxable[amt] = np.round(b_taxable[amt] * factor, 2)
    b_tax[amt] = np.round(b_taxable[amt] * rate[amt] / 100, 2)
    b_inter = inter.copy()
    swap = r["head"] < noise.tax_head_swap
    b_inter[swap] = ~b_inter[swap]
    shift = np.where(r["date"] < noise.date_shift, rng.integers(-5, 6, n_invoices), 0)
    base = dt.date(year, month, 1)
    t_dates = [base + dt.timedelta(days=int(d) - 1) for d in day]
    b_dates = [d + dt.timedelta(days=int(k)) for d, k in zip(t_dates, shift, strict=True)]

    # ---- books frame
    idx = np.flatnonzero(in_books)
    dup_idx = idx[r["dup"][idx] < noise.duplicate_in_books]
    rows = np.concatenate([idx, dup_idx])
    btx, bt = b_taxable[rows], b_tax[rows]
    binter = b_inter[rows]
    books = pl.DataFrame(
        {
            "Supplier Invoice Date": [b_dates[i].strftime("%d-%m-%Y") for i in rows],
            "Party Name": [sup_name[sup_of[i]] for i in rows],
            "GSTIN/UIN": [books_gstin_of[int(sup_of[i])] for i in rows],
            "Supplier Invoice No": [books_no[i] for i in rows],
            "Document Type": [
                {"INV": "Invoice", "CRN": "Credit Note", "DBN": "Debit Note"}[doc_type[i]]
                for i in rows
            ],
            "Taxable Value": np.round(btx, 2),
            "IGST": np.where(binter, bt, 0.0),
            "CGST": np.where(binter, 0.0, np.round(bt / 2, 2)),
            "SGST": np.where(binter, 0.0, np.round(bt - np.round(bt / 2, 2), 2)),
            "Cess": np.zeros(len(rows)),
            "Invoice Value": np.round(btx + bt, 2),
            "_true_id": rows,
        }
    )
    books = books.sample(fraction=1.0, shuffle=True, seed=seed)
    truth_books = books.select(
        pl.int_range(pl.len()).alias("row"), pl.col("_true_id").alias("true_id")
    )
    books = books.drop("_true_id")

    # ---- GSTR-2B JSON, grouped by supplier the way the portal does it
    gstr2b = {
        "chksum": "",
        "data": {
            "gstin": _gstin(rng, RECIPIENT_STATE),
            "rtnprd": f"{month:02d}{year}",
            "version": "1.0",
            "gendt": (dt.date(year, month, days_in_month) + dt.timedelta(days=14)).strftime(
                "%d-%m-%Y"
            ),
            "docdata": {"b2b": [], "cdnr": []},
        },
    }
    order_2b: list[int] = []
    sections = {INVOICE: "b2b", CREDIT_NOTE: "cdnr", DEBIT_NOTE: "cdnr"}
    grouped: dict[str, dict[int, list[int]]] = {"b2b": {}, "cdnr": {}}
    for i in np.flatnonzero(in_2b):
        grouped[sections[doc_type[i]]].setdefault(int(sup_of[i]), []).append(int(i))
    filed = (dt.date(year, month, 11) + dt.timedelta(days=1)).strftime("%d-%m-%Y")
    for section in ("b2b", "cdnr"):
        for s, docs in grouped[section].items():
            entry = {
                "ctin": sup_gstin[s],
                "trdnm": sup_name[s],
                "supfildt": filed,
                "supprd": f"{month:02d}{year}",
            }
            items = []
            for i in docs:
                igst = float(tax[i]) if inter[i] else 0.0
                half = 0.0 if inter[i] else round(float(tax[i]) / 2, 2)
                other = 0.0 if inter[i] else round(float(tax[i]) - half, 2)
                doc = {
                    "dt": t_dates[i].strftime("%d-%m-%Y"),
                    "val": round(float(taxable[i] + tax[i]), 2),
                    "pos": RECIPIENT_STATE,
                    "rev": "N",
                    "itcavl": "N" if r["itc"][i] < noise.itc_unavailable else "Y",
                    "rsn": "",
                    "diffprcnt": 1,
                    "srctyp": "",
                    "items": [
                        {
                            "num": 1,
                            "rt": int(rate[i]),
                            "txval": float(taxable[i]),
                            "igst": igst,
                            "cgst": half,
                            "sgst": other,
                            "cess": 0,
                        }
                    ],
                }
                if section == "b2b":
                    doc = {"inum": portal_no[i], "typ": "R", **doc}
                else:
                    doc = {
                        "ntnum": portal_no[i],
                        "typ": "D" if doc_type[i] == DEBIT_NOTE else "C",
                        "suptyp": "R",
                        **doc,
                    }
                items.append(doc)
                order_2b.append(i)
            entry["inv" if section == "b2b" else "nt"] = items
            gstr2b["data"]["docdata"][section].append(entry)
    truth_2b = pl.DataFrame({"row": np.arange(len(order_2b)), "true_id": np.array(order_2b)})
    return books, gstr2b, truth_books, truth_2b


def write(out_dir: str | Path, n_invoices: int, seed: int = 7, excel: bool = False) -> dict:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    books, gstr2b, tb, t2 = generate(n_invoices, seed=seed)
    paths = {"books": out / ("books.xlsx" if excel else "books.csv"), "gstr2b": out / "gstr2b.json"}
    if excel:
        books.write_excel(paths["books"], worksheet="Purchase Register", autofit=True)
    else:
        books.write_csv(paths["books"])
    with paths["gstr2b"].open("w") as fh:
        json.dump(gstr2b, fh, separators=(",", ":"))
    paths["truth_books"] = out / "truth_books.csv"
    paths["truth_2b"] = out / "truth_2b.csv"
    tb.write_csv(paths["truth_books"])
    t2.write_csv(paths["truth_2b"])
    return paths


def evaluate(rows: pl.DataFrame, truth_books: pl.DataFrame, truth_2b: pl.DataFrame) -> dict:
    """Precision and recall of the pairs against the hidden answers."""
    pairs = rows.filter(pl.col("books_row").is_not_null() & pl.col("gstr2b_row").is_not_null())
    pairs = pairs.join(
        truth_books.rename({"row": "books_row", "true_id": "b_true"}), on="books_row", how="left"
    ).join(truth_2b.rename({"row": "gstr2b_row", "true_id": "t_true"}), on="gstr2b_row", how="left")
    correct = pairs.filter(pl.col("b_true") == pl.col("t_true"))
    pairable = set(truth_books["true_id"].to_list()) & set(truth_2b["true_id"].to_list())
    found = set(correct["b_true"].to_list())
    by_pass = (
        pairs.group_by("match_pass")
        .agg(pl.len().alias("pairs"), (pl.col("b_true") == pl.col("t_true")).sum().alias("correct"))
        .with_columns((pl.col("correct") / pl.col("pairs")).round(4).alias("precision"))
        .sort("pairs", descending=True)
    )
    return {
        "pairs": pairs.height,
        "correct": correct.height,
        "precision": round(correct.height / max(pairs.height, 1), 4),
        "pairable": len(pairable),
        "recall": round(len(found & pairable) / max(len(pairable), 1), 4),
        "by_pass": by_pass,
    }
