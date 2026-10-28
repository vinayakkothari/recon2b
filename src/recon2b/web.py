"""A one-page upload form: drop in the books and GSTR-2B, get the report back.

Files are written to a temporary folder only for the length of the request and
deleted as soon as the report has been sent. Nothing is stored.

Run with ``recon2b serve`` and open http://127.0.0.1:8000
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask

from .matcher import MatchConfig
from .pipeline import run

MAX_UPLOAD_MB = 200
BOOKS_TYPES = {".csv", ".xlsx", ".xlsm"}
GSTR2B_TYPES = {".json", ".xlsx", ".xlsm"}

app = FastAPI(title="recon2b", docs_url="/api/docs")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return PAGE


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/reconcile")
async def reconcile_files(
    books: Annotated[UploadFile, File()],
    gstr2b: Annotated[list[UploadFile], File()],
    tolerance: Annotated[float, Form()] = 1.0,
) -> FileResponse:
    if not 0 <= tolerance <= 1000:
        raise HTTPException(422, "tolerance must be between 0 and 1000 rupees")
    work = Path(tempfile.mkdtemp(prefix="recon2b-"))
    try:
        books_path = await _save(books, work / "books", BOOKS_TYPES)
        portal_paths = [
            await _save(f, work / f"gstr2b_{i}", GSTR2B_TYPES) for i, f in enumerate(gstr2b)
        ]
        out = work / "reconciliation.xlsx"
        try:
            run(books_path, portal_paths, out, MatchConfig(tolerance=tolerance))
        except ValueError as err:  # unreadable or unrecognised file, said in plain words
            raise HTTPException(422, str(err)) from err
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return FileResponse(
        out,
        filename="reconciliation.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        background=BackgroundTask(shutil.rmtree, work, ignore_errors=True),
    )


async def _save(upload: UploadFile, stem: Path, allowed: set[str]) -> Path:
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(422, f"{upload.filename}: expected one of {', '.join(sorted(allowed))}")
    target = stem.with_suffix(suffix)
    size = 0
    with target.open("wb") as fh:
        while chunk := await upload.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD_MB << 20:
                raise HTTPException(413, f"{upload.filename} is over {MAX_UPLOAD_MB} MB")
            fh.write(chunk)
    return target


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>recon2b</title>
<style>
  :root { --ink:#14213d; --muted:#5f6b7a; --line:#d9dee7; --accent:#1f3a5f; --bg:#f6f7f9; }
  * { box-sizing: border-box; }
  body { margin:0; font:16px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
         color:var(--ink); background:var(--bg); }
  main { max-width:640px; margin:48px auto; padding:0 16px; }
  h1 { font-size:28px; margin:0 0 4px; letter-spacing:-0.01em; }
  p.lead { color:var(--muted); margin:0 0 28px; }
  form { background:#fff; border:1px solid var(--line); border-radius:10px; padding:24px; }
  label { display:block; font-weight:600; margin:0 0 6px; }
  .hint { display:block; font-weight:400; color:var(--muted); font-size:14px; }
  .field { margin-bottom:20px; }
  input[type=file] { width:100%; padding:10px; border:1px dashed var(--line);
                     border-radius:8px; background:var(--bg); }
  input[type=number] { width:120px; padding:8px; border:1px solid var(--line);
                       border-radius:6px; font:inherit; }
  button { background:var(--accent); color:#fff; border:0; border-radius:8px;
           padding:12px 20px; font:inherit; font-weight:600; cursor:pointer; }
  button[disabled] { opacity:.6; cursor:progress; }
  #status { margin-top:16px; color:var(--muted); min-height:24px; }
  #status.error { color:#b3261e; }
  footer { color:var(--muted); font-size:14px; margin-top:24px; }
</style>
</head>
<body>
<main>
  <h1>recon2b</h1>
  <p class="lead">Match your purchase register against GSTR-2B and see which input tax
  credit is at risk. You get back an Excel report.</p>
  <form id="f">
    <div class="field">
      <label for="books">Purchase register
        <span class="hint">CSV or Excel export from Tally, Zoho, Busy or your own sheet</span>
      </label>
      <input id="books" name="books" type="file" accept=".csv,.xlsx,.xlsm" required>
    </div>
    <div class="field">
      <label for="gstr2b">GSTR-2B
        <span class="hint">JSON or Excel from the GST portal. Pick several for more months.</span>
      </label>
      <input id="gstr2b" name="gstr2b" type="file" accept=".json,.xlsx,.xlsm" multiple required>
    </div>
    <div class="field">
      <label for="tolerance">Tolerance in rupees
        <span class="hint">Differences up to this much count as rounding</span>
      </label>
      <input id="tolerance" name="tolerance" type="number" min="0" step="0.5" value="1">
    </div>
    <button id="go" type="submit">Reconcile</button>
    <div id="status" role="status"></div>
  </form>
  <footer>Files are processed for this request only and deleted straight after.</footer>
</main>
<script>
const form = document.getElementById("f"), go = document.getElementById("go"),
      status = document.getElementById("status");
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  go.disabled = true; status.className = ""; status.textContent = "Reconciling...";
  try {
    const res = await fetch("/reconcile", { method: "POST", body: new FormData(form) });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || res.statusText);
    }
    const blob = await res.blob(), a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = "reconciliation.xlsx"; a.click();
    URL.revokeObjectURL(a.href);
    status.textContent = "Done. The report has downloaded.";
  } catch (err) {
    status.className = "error"; status.textContent = err.message;
  } finally { go.disabled = false; }
});
</script>
</body>
</html>
"""
