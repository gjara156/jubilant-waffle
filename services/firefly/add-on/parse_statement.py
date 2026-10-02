from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import pdfplumber


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class AccountSummary:
    name: str
    number: str
    beginning_balance: float
    ending_balance: float

@dataclass
class Transaction:
    account_name: str
    account_number: str
    date: str
    description: str
    credit: float
    debit: float
    amount: float   # credit + debit, since debit is already stored negative
    balance: float

def money_to_float(s: str) -> float:
    """
    '$108.59' -> 108.59
    '-$37.02' -> -37.02
    '-$0.00'  -> -0.0   (fine -- -0.0 == 0.0 in Python, and prints as -0.0
                          if you str() it directly, so round() it on output
                          if that bugs you)

    LEARN: the statement always writes amounts with a leading $ and commas
    for thousands. Strip both, then let float() handle the sign -- it
    already understands a leading '-'.
    """
    s = s.strip().replace("$", "").replace(",", "")
    return float(s)

def extract_full_text(pdf_path: str) -> str:
    """
    Pull text from every page and join with newlines into one big string.

    LEARN: joining pages into ONE string (rather than keeping them separate)
    matters here because a single account's activity table gets split
    across multiple PDF pages (e.g. Jorge's Checking runs from page 2 to
    page 4 in a typical statement) without repeating the "Account Number:"
    header. If we parsed page-by-page we'd lose track of which account
    we're in every time a page breaks mid-account. One combined string
    lets us split on account boundaries instead of page boundaries.
    """
    with pdfplumber.open(pdf_path) as pdf:
        page_texts = [page.extract_text() or "" for page in pdf.pages]
    return "\n".join(page_texts)

SUMMARY_ROW_RE = re.compile(
    r"^(.+?)\s+(xxxxxx\d+)\s+(-?\$[\d,]+\.\d{2})\s+(-?\$[\d,]+\.\d{2})$",
    re.MULTILINE,
)

TXN_LINE_RE = re.compile(
    r"^(\d{2}/\d{2}/\d{4})\s+(.+?)\s+(-?\$[\d,]+\.\d{2})\s+(-?\$[\d,]+\.\d{2})\s+(-?\$[\d,]+\.\d{2})$"
)

BALANCE_LINE_RE = re.compile(
    r"^(\d{2}/\d{2}/\d{4})\s+((?:Beginning|Ending) Balance)\s+(-?\$[\d,]+\.\d{2})$"
)

NOISE_LINE_RE = re.compile(
    r"^("
    r"Ally Bank Member FDIC.*"
    r"|COMBINED CUSTOMER STATEMENT"
    r"|Statement Date"
    r"|Page \d+"
    r"|Customer Care Information"
    r"|Toll Free .*"
    r"|www\.ally\.com"
    r"|Activity"
    r"|Date Description Credits Debits Balance"
    # the long routing-code line, e.g. 269327/1549274//...
    r"|\d{2,}/\d{4,}//.*"
    r"|\d{6}-\d{2}-\d{2}"           # e.g. 269327-02-22
    # a bare date line (statement date repeated)
    r"|\d{2}/\d{2}/\d{4}$"
    r")$"
)

def parse_summary_table(full_text: str) -> list[AccountSummary]:
    accounts: list[AccountSummary] = []

    for match in SUMMARY_ROW_RE.finditer(full_text):
        name = match.group(1)
        number = match.group(2)
        beginning = money_to_float(match.group(3))
        ending = money_to_float(match.group(4))

        account = AccountSummary(
            name=name,
            number=number,
            beginning_balance=beginning,
            ending_balance=ending,
        )
        accounts.append(account)

    return accounts

def split_by_account(full_text: str, accounts: list[AccountSummary]) -> dict[str, str]:
    markers = []
    for acct in accounts:
        m = re.search(
            rf"Account Number:\s*{re.escape(acct.number)}\b", full_text)
        if not m:
            raise ValueError(f"Couldn't find header for account {acct.number}")
        markers.append((acct.number, m.start()))

    markers.sort(key=lambda pair: pair[1])

    chunks: dict[str, str] = {}
    for i, (number, start) in enumerate(markers):
        end = markers[i + 1][1] if i + 1 < len(markers) else len(full_text)
        chunks[number] = full_text[start:end]
    return chunks

def parse_transactions(account_name: str, account_number: str, section_text: str) -> list[Transaction]:
    transactions: list[Transaction] = []
    current = None
    description_parts: list[str] = []

    def finalize() -> None:
        nonlocal current
        if current is not None:
            transactions.append(Transaction(
                account_name=account_name,
                account_number=account_number,
                date=current["date"],
                description=" ".join(description_parts).strip(),
                credit=current["credit"],
                debit=current["debit"],
                amount=current["credit"] + current["debit"],
                balance=current["balance"],
            ))
        current = None

    for line in section_text.splitlines():
        line = line.strip()
        if not line:
            continue

        txn_match = TXN_LINE_RE.match(line)
        if txn_match:
            finalize()
            date, description, credit_str, debit_str, balance_str = txn_match.groups()
            current = {
                "date": date,
                "credit": money_to_float(credit_str),
                "debit": money_to_float(debit_str),
                "balance": money_to_float(balance_str),
            }
            description_parts = [description]
            continue

        if BALANCE_LINE_RE.match(line):
            finalize()
            continue

        if NOISE_LINE_RE.match(line):
            continue

        description_parts.append(line)

    finalize()
    return transactions

def write_account_summary_csv(accounts: list[AccountSummary], path: Path) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["name", "number", "beginning_balance", "ending_balance"])
        writer.writeheader()
        for acct in accounts:
            writer.writerow(asdict(acct))


def write_transactions_csv(transactions: list[Transaction], path: Path) -> None:
    fieldnames = ["account_name", "account_number", "date",
                  "description", "credit", "debit", "amount", "balance"]
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for txn in transactions:
            writer.writerow(asdict(txn))

# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse an Ally combined statement PDF into CSVs.")
    parser.add_argument("pdf_path", help="Path to the statement PDF")
    parser.add_argument("--outdir", default=".",
                        help="Where to write the CSVs")
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    full_text = extract_full_text(args.pdf_path)

    accounts = parse_summary_table(full_text)
    print(f"Found {len(accounts)} accounts:")
    for a in accounts:
        print(
            f"  {a.name} ({a.number}): {a.beginning_balance:.2f} -> {a.ending_balance:.2f}")

    sections = split_by_account(full_text, accounts)

    all_transactions: list[Transaction] = []
    for acct in accounts:
        txns = parse_transactions(
            acct.name, acct.number, sections[acct.number])
        print(f"  {acct.name}: parsed {len(txns)} transactions")
        all_transactions.extend(txns)

    write_account_summary_csv(accounts, outdir / "account_summary.csv")
    write_transactions_csv(all_transactions, outdir / "transactions.csv")
    print(
        f"\nWrote {outdir / 'account_summary.csv'} and {outdir / 'transactions.csv'}")


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Quick self-test fixtures -- run this file with `python parse_statement.py
# --selftest` style usage isn't wired up on purpose; instead, paste these
# into a scratch script or a REPL while you build parse_transactions, so you
# can iterate without re-running pdfplumber every time.
# ---------------------------------------------------------------------------

SAMPLE_SUMMARY_TEXT = """
Jorge's Checking xxxxxx5772 $108.59 -$37.02
Joint Checking xxxxxx3761 $4,435.02 $3,353.41
Jorge's Savings Account xxxxxx3893 $39.14 $6.30
Money Market Savings xxxxxx6633 $5,865.59 $5,814.78
Joint Savings xxxxxx3776 $601.16 $1,103.32
Total Account Balances: $11,049.50 $10,240.79
""".strip()

SAMPLE_ACTIVITY_TEXT = """
Activity
Date Description Credits Debits Balance
07/26/2026 Beginning Balance $108.59
07/27/2026 WEB Funds Transfer $0.00 -$5.63 $102.96
Round ups Booster Transfer to Savings
Account XXXXXX3893
07/27/2026 Check Card Purchase $0.00 -$9.99 $92.97
NEWSHOSTING.COM 127 W. Fairbanks
Ave WINTER PARK, FL, US
Ally Bank Member FDIC STMTCMB100 05/2013
COMBINED CUSTOMER STATEMENT
Statement Date
Page 3
Customer Care Information
Toll Free 877-247-ALLY (2559)
www.ally.com
Activity
Date Description Credits Debits Balance
07/28/2026 WEB Funds Transfer $15.59 -$0.00 $110.26
Internet transfer from Money Market Savings
account
XXXXXX6633
08/25/2026 Ending Balance -$37.02
""".strip()
