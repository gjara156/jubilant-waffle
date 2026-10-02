"""
Statement Parser web app.

LEARN: this whole app has no database and no filesystem writes -- by
design, since you want it stateless. The uploaded PDF's bytes go into a
BytesIO buffer, pdfplumber reads straight out of that (pdfplumber.open()
accepts any file-like object, not just a path string -- your
extract_full_text() already works unchanged for this reason), the two
CSVs get built as text in memory, and both get zipped into a second
BytesIO buffer that gets streamed back as the response body. Nothing
ever touches disk, so there's no upload directory to clean up and no way
for one request to see another's data.
"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import asdict
from datetime import date

from flask import Flask, abort, render_template, request, send_file

import parse_statement as ps

app = Flask(__name__)

# LEARN: Flask's default max upload size is unlimited. That's a
# non-issue for you alone, but Alejandra will be uploading too, and
# capping it costs nothing. Bump this if a statement ever legitimately
# exceeds 25 MB (unlikely for a single month's PDF).
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024


def _accounts_to_csv_bytes(accounts: list[ps.AccountSummary]) -> bytes:
    """Same fields as write_account_summary_csv, but into an in-memory buffer."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["name", "number", "beginning_balance", "ending_balance"])
    writer.writeheader()
    for acct in accounts:
        writer.writerow(asdict(acct))
    return buf.getvalue().encode("utf-8")


def _transactions_to_csv_bytes(transactions: list[ps.Transaction]) -> bytes:
    """Same fields as write_transactions_csv, but into an in-memory buffer."""
    buf = io.StringIO()
    fieldnames = ["account_name", "account_number", "date", "description", "credit", "debit", "amount", "balance"]
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for txn in transactions:
        writer.writerow(asdict(txn))
    return buf.getvalue().encode("utf-8")


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/parse")
def parse():
    uploaded = request.files.get("statement")
    if uploaded is None or uploaded.filename == "":
        abort(400, "No file uploaded")
    if not uploaded.filename.lower().endswith(".pdf"):
        abort(400, "File must be a PDF")

    pdf_bytes = io.BytesIO(uploaded.read())

    try:
        full_text = ps.extract_full_text(pdf_bytes)
        accounts = ps.parse_summary_table(full_text)
        sections = ps.split_by_account(full_text, accounts)

        all_transactions: list[ps.Transaction] = []
        for acct in accounts:
            all_transactions.extend(ps.parse_transactions(acct.name, acct.number, sections[acct.number]))
    except Exception as exc:
        # TODO(you): pick a failure mode you're happy with. Right now this
        # surfaces the raw exception text to whoever's uploading (you or
        # Alejandra), which is convenient for debugging together but means
        # a Python traceback fragment shows up in the browser. If you'd
        # rather show something friendlier and just check `docker logs` for
        # the real error, log `exc` server-side here and abort with a fixed
        # generic message instead.
        abort(422, f"Couldn't parse this statement: {exc}")

    if not accounts:
        abort(422, "No accounts found -- is this an Ally combined statement PDF?")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("account_summary.csv", _accounts_to_csv_bytes(accounts))
        zf.writestr("transactions.csv", _transactions_to_csv_bytes(all_transactions))
    zip_buf.seek(0)

    return send_file(
        zip_buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"statement_export_{date.today().isoformat()}.zip",
    )


if __name__ == "__main__":
    # LEARN: this dev-server entrypoint is only for running `python app.py`
    # locally while you poke at it in a browser. The Docker image below
    # uses gunicorn instead -- Flask's own dev server prints a warning
    # telling you not to use it for anything real, since it's
    # single-threaded by default and not hardened against concurrent load.
    app.run(host="0.0.0.0", port=8000, debug=True)
