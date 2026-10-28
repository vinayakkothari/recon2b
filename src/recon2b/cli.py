"""Command line: ``recon2b run``, ``learn``, ``pair``, ``synth``, ``eval`` and ``serve``."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from .matcher import MatchConfig

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Reconcile a purchase register against GSTR-2B and find the input tax credit at risk.",
)


def _money(x: float) -> str:
    """Indian digit grouping: 12,34,567.89"""
    sign = "-" if x < 0 else ""
    whole, frac = f"{abs(x):.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return sign + ",".join(groups + [tail]) + "." + frac


@app.command()
def run(
    books: Annotated[Path, typer.Option("--books", "-b", help="Purchase register, .csv or .xlsx")],
    gstr2b: Annotated[
        list[Path], typer.Option("--gstr2b", "-g", help="GSTR-2B .json or .xlsx, repeat for more")
    ],
    out: Annotated[Path, typer.Option("--out", "-o", help=".xlsx, .csv or .parquet")] = Path(
        "reconciliation.xlsx"
    ),
    decisions: Annotated[
        Path | None, typer.Option(help="Saved decisions file, see `recon2b learn`")
    ] = None,
    column_map: Annotated[
        Path | None, typer.Option(help='JSON like {"inv_no": "Bill Ref"} for unusual headers')
    ] = None,
    sheet: Annotated[str | None, typer.Option(help="Sheet name in the books workbook")] = None,
    tolerance: Annotated[float, typer.Option(help="Rupees allowed on taxable and tax")] = 1.0,
    date_window: Annotated[int, typer.Option(help="Days allowed when pairing on amounts")] = 7,
    fuzzy: Annotated[float, typer.Option(help="Invoice number similarity cut-off, 0-100")] = 85,
) -> None:
    """Reconcile and write a report."""
    from .pipeline import run as run_pipeline

    cfg = MatchConfig(tolerance=tolerance, date_window=date_window, fuzzy_threshold=fuzzy)
    result, path = run_pipeline(books, gstr2b, out, cfg, decisions, column_map, sheet)
    summary = result.summary()
    typer.echo(f"\n{'status':<20}{'documents':>10}{'ITC at risk':>18}")
    for rec in summary.iter_rows(named=True):
        typer.echo(
            f"{str(rec['status']):<20}{rec['documents']:>10}{_money(rec['itc_at_risk']):>18}"
        )
    total = summary["itc_at_risk"].sum()
    typer.echo(f"{'total':<20}{summary['documents'].sum():>10}{_money(total):>18}")
    review = int(result.rows["needs_review"].sum())
    if review:
        typer.echo(f"\n{review} pairs need a look in the Review sheet.")
    if result.gstin_issues.height:
        typer.echo(f"{result.gstin_issues.height} GSTINs in the books never appear in 2B.")
    took = sum(v for k, v in result.timings.items() if k.startswith("total_"))
    typer.echo(f"\nWrote {path} in {took:.1f}s")


@app.command()
def learn(
    report: Annotated[Path, typer.Argument(help="A report with the Review sheet filled in")],
    decisions: Annotated[Path, typer.Option(help="Decisions file to update")] = Path(
        "decisions.json"
    ),
) -> None:
    """Save the Accept and Reject answers from a report's Review sheet."""
    from .decisions import DecisionStore

    store = DecisionStore(decisions)
    n = store.learn_from_report(report)
    store.save(decisions)
    typer.echo(f"Read {n} decisions. {decisions} now holds {len(store)}.")


@app.command()
def pair(
    gstin: Annotated[str, typer.Option(help="Supplier GSTIN")],
    books_inv: Annotated[str, typer.Option(help="Invoice number as in the books")],
    gstr2b_inv: Annotated[str, typer.Option(help="Invoice number as in 2B")],
    doc_type: Annotated[str, typer.Option(help="INV, CRN or DBN")] = "INV",
    reject: Annotated[bool, typer.Option(help="Never pair these two instead")] = False,
    decisions: Annotated[Path, typer.Option(help="Decisions file to update")] = Path(
        "decisions.json"
    ),
) -> None:
    """Tell the matcher two documents are (or are not) the same."""
    from .decisions import ACCEPT, REJECT, DecisionStore

    store = DecisionStore(decisions)
    store.add(gstin, doc_type, books_inv, gstr2b_inv, REJECT if reject else ACCEPT)
    store.save(decisions)
    typer.echo(f"Saved. {decisions} now holds {len(store)} decisions.")


@app.command()
def synth(
    out_dir: Annotated[Path, typer.Option(help="Folder for the generated files")] = Path("data"),
    invoices: Annotated[int, typer.Option(help="How many supplier documents")] = 10_000,
    seed: Annotated[int, typer.Option()] = 7,
    excel: Annotated[bool, typer.Option(help="Write the books as .xlsx instead of .csv")] = False,
) -> None:
    """Generate realistic books and GSTR-2B files with hidden right answers."""
    from . import synth as s

    paths = s.write(out_dir, invoices, seed=seed, excel=excel)
    for k, p in paths.items():
        typer.echo(f"{k:<12}{p}")


@app.command("eval")
def evaluate(
    data_dir: Annotated[Path, typer.Argument(help="A folder written by `recon2b synth`")],
    tolerance: Annotated[float, typer.Option()] = 1.0,
) -> None:
    """Measure precision and recall against synthetic data with known answers."""
    import time

    import polars as pl

    from . import synth as s
    from .loaders import load_books, load_gstr2b
    from .matcher import reconcile

    books = next(p for p in (data_dir / "books.csv", data_dir / "books.xlsx") if p.exists())
    clock = time.perf_counter()
    b, t = load_books(books), load_gstr2b(data_dir / "gstr2b.json")
    loaded = time.perf_counter() - clock
    res = reconcile(b, t, MatchConfig(tolerance=tolerance))
    report = s.evaluate(
        res.rows,
        pl.read_csv(data_dir / "truth_books.csv"),
        pl.read_csv(data_dir / "truth_2b.csv"),
    )
    typer.echo(f"books rows {b.height:,}   2B rows {t.height:,}")
    typer.echo(f"load {loaded:.2f}s   match {sum(res.timings.values()):.2f}s")
    typer.echo(f"precision {report['precision']:.2%}   recall {report['recall']:.2%}")
    typer.echo(str(report["by_pass"]))


@app.command()
def serve(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int, typer.Option()] = 8000,
) -> None:
    """Start the upload page."""
    try:
        import uvicorn
    except ImportError as err:  # pragma: no cover
        raise typer.Exit("Install the web extra first: pip install 'recon2b[web]'") from err
    uvicorn.run("recon2b.web:app", host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    app()
