#!/usr/bin/env python3
"""Client and review-growth tracker for Project Exodus.

Backed by a local SQLite file, zero external dependencies. Tracks NFC
review-card clients, periodic review-count/rating snapshots, and MRR.
"""
from __future__ import annotations

import argparse
import json
import random
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path
from typing import Callable, Iterator, Optional, TypeVar

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "exodus.db"
DEFAULT_DASHBOARD_PATH = Path(__file__).resolve().parent.parent / "dashboard.html"

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id               TEXT PRIMARY KEY,
    business_name            TEXT NOT NULL,
    contact_name             TEXT,
    phone                    TEXT,
    email                    TEXT,
    signup_date              DATE NOT NULL,
    card_status              TEXT NOT NULL DEFAULT 'active'
                                 CHECK (card_status IN ('active', 'paused', 'canceled')),
    stripe_subscription_id   TEXT,
    monthly_rate             INTEGER NOT NULL DEFAULT 25
);

CREATE TABLE IF NOT EXISTS review_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id       TEXT NOT NULL REFERENCES clients(client_id),
    snapshot_date   DATE NOT NULL,
    review_count    INTEGER NOT NULL,
    average_rating  REAL NOT NULL
);

-- Matches get_client_report()'s per-client ORDER BY snapshot_date scan.
CREATE INDEX IF NOT EXISTS idx_review_snapshots_client_date
    ON review_snapshots(client_id, snapshot_date);
-- Matches get_mrr()'s WHERE card_status = 'active' scan.
CREATE INDEX IF NOT EXISTS idx_clients_card_status
    ON clients(card_status);
"""

T = TypeVar("T")

RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY_SECONDS = 1.0
RETRY_MAX_DELAY_SECONDS = 20.0


def with_retry(
    func: Callable[[], T],
    *,
    retryable: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError),
) -> T:
    """Generic exponential-backoff retry wrapper for flaky network calls.

    Not wired to anything yet — there's no external API call in this
    module today. Kept here ready for when review-count checking moves
    from manual entry to an API (e.g. Google Business Profile)."""
    last_error: Optional[BaseException] = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return func()
        except retryable as e:
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            last_error = e
        delay = min(RETRY_MAX_DELAY_SECONDS, RETRY_BASE_DELAY_SECONDS * (2**attempt))
        time.sleep(delay + random.uniform(0, 0.5))
    raise last_error  # pragma: no cover — loop always returns or raises above


class ClientError(RuntimeError):
    pass


@dataclass
class Client:
    client_id: str
    business_name: str
    contact_name: Optional[str]
    phone: Optional[str]
    email: Optional[str]
    signup_date: str
    card_status: str
    stripe_subscription_id: Optional[str]
    monthly_rate: int


@dataclass
class ReviewSnapshot:
    id: int
    client_id: str
    snapshot_date: str
    review_count: int
    average_rating: float


_DASHBOARD_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Exodus — Client Ledger</title>
<style>
  :root {{
    --ink: #0a0a0c;
    --hairline: rgba(201,164,104,0.14);
    --gold: #c9a468;
    --gold-bright: #ddc08a;
    --text: #f2f0ea;
    --text-muted: #98a0b3;
    --good: #7fae7a;
    --warn: #c98a4d;
    --bad: #c0665f;
    --font-serif: 'Iowan Old Style','Palatino Linotype',Palatino,Georgia,serif;
    --font-sans: -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--ink); color: var(--text);
    font-family: var(--font-sans); line-height: 1.55;
    -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
    padding: 48px 24px 72px;
  }}
  .page {{ max-width: 720px; margin: 0 auto; }}
  header {{
    display: flex; justify-content: space-between; align-items: baseline;
    border-bottom: 1px solid var(--hairline); padding-bottom: 20px; margin-bottom: 40px;
  }}
  .wordmark {{ font-family: var(--font-serif); font-size: 1.15rem; letter-spacing: 0.02em; color: var(--gold-bright); }}
  .wordmark span {{ color: var(--text-muted); font-family: var(--font-sans); font-size: 0.8rem; margin-left: 8px; }}
  .generated {{ font-size: 0.75rem; color: var(--text-muted); font-variant-numeric: tabular-nums; }}

  .hero-label {{
    font-family: var(--font-serif); font-style: italic; font-size: 0.85rem;
    color: var(--text-muted); margin-bottom: 6px;
  }}
  .hero-value {{
    font-size: 3.4rem; font-weight: 600; color: var(--gold);
    font-variant-numeric: tabular-nums; line-height: 1; text-wrap: balance;
  }}
  .fleet-line {{
    font-size: 0.88rem; color: var(--text-muted); margin: 14px 0 44px;
    font-variant-numeric: tabular-nums;
  }}
  .fleet-line b {{ color: var(--text); font-weight: 600; }}
  .dot {{ display: inline-block; width: 7px; height: 7px; border-radius: 50%; }}
  .fleet-line .dot {{ margin-left: 14px; margin-right: 5px; }}
  .dot-good {{ background: var(--good); }}
  .dot-warn {{ background: var(--warn); }}
  .dot-bad {{ background: var(--bad); }}

  .section-label {{
    font-family: var(--font-serif); font-style: italic; font-size: 0.85rem;
    color: var(--text-muted); margin-bottom: 14px;
  }}
  .table-wrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{
    text-align: left; font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em;
    color: var(--text-muted); font-weight: 500; padding: 0 16px 10px 0;
    border-bottom: 1px solid var(--hairline); white-space: nowrap;
  }}
  td {{
    padding: 14px 16px 14px 0; font-size: 0.9rem; border-bottom: 1px solid var(--hairline);
    vertical-align: middle; white-space: nowrap;
  }}
  tr:last-child td {{ border-bottom: none; }}
  .biz {{ color: var(--text); font-weight: 500; }}
  .status {{ display: inline-flex; align-items: center; font-size: 0.82rem; color: var(--text-muted); }}
  .status .dot {{ margin-right: 6px; }}
  .rate {{ font-variant-numeric: tabular-nums; color: var(--text-muted); }}
  .growth {{ font-variant-numeric: tabular-nums; }}
  .growth-up {{ color: var(--good); }}
  .growth-flat {{ color: var(--text-muted); }}
  .growth-detail {{ color: var(--text-muted); font-size: 0.85rem; }}
  .empty {{ color: var(--text-muted); font-style: italic; }}
</style>
</head>
<body>
<div class="page">
  <header>
    <div class="wordmark">Exodus<span>client ledger</span></div>
    <div class="generated">{generated_at}</div>
  </header>

  <div class="hero-label">Monthly recurring revenue</div>
  <div class="hero-value">${mrr}</div>
  <div class="fleet-line">
    <b>{total_clients}</b> client{plural} on the books
    <span class="dot dot-good"></span>{active_count} active
    <span class="dot dot-warn"></span>{paused_count} paused
    <span class="dot dot-bad"></span>{canceled_count} canceled
  </div>

  <div class="section-label">Review growth by client</div>
  <div class="table-wrap">
  <table>
    <thead><tr><th>Business</th><th>Status</th><th>Rate</th><th>Growth</th></tr></thead>
    <tbody>
{table_rows}
    </tbody>
  </table>
  </div>
</div>
</body>
</html>
"""

_STATUS_DOT = {"active": "dot-good", "paused": "dot-warn", "canceled": "dot-bad"}


def _render_client_row(client: sqlite3.Row, snapshots: list[sqlite3.Row]) -> str:
    status = client["card_status"]
    if snapshots:
        first, latest = snapshots[0], snapshots[-1]
        gain = latest["review_count"] - first["review_count"]
        arrow = "↑" if gain > 0 else ("↓" if gain < 0 else "→")
        growth_class = "growth-up" if gain > 0 else "growth-flat"
        growth = (
            f'<span class="growth {growth_class}">{arrow} {gain:+d} reviews</span> '
            f'<span class="growth-detail">({first["review_count"]}→{latest["review_count"]}, '
            f'{latest["average_rating"]:.1f}★)</span>'
        )
    else:
        growth = '<span class="empty">no snapshots yet</span>'
    return (
        "      <tr>"
        f'<td class="biz">{escape(client["business_name"])}</td>'
        f'<td><span class="status"><span class="dot {_STATUS_DOT[status]}"></span>{escape(status)}</span></td>'
        f'<td class="rate">${client["monthly_rate"]}/mo</td>'
        f"<td>{growth}</td>"
        "</tr>"
    )


class ClientTracker:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add_client(
        self,
        client_id: str,
        business_name: str,
        contact_name: Optional[str] = None,
        phone: Optional[str] = None,
        email: Optional[str] = None,
        signup_date: Optional[str] = None,
        card_status: str = "active",
        stripe_subscription_id: Optional[str] = None,
        monthly_rate: int = 25,
    ) -> None:
        signup_date = signup_date or date.today().isoformat()
        try:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO clients
                       (client_id, business_name, contact_name, phone, email,
                        signup_date, card_status, stripe_subscription_id, monthly_rate)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        client_id,
                        business_name,
                        contact_name,
                        phone,
                        email,
                        signup_date,
                        card_status,
                        stripe_subscription_id,
                        monthly_rate,
                    ),
                )
        except sqlite3.IntegrityError as e:
            raise ClientError(f"could not add client '{client_id}': {e}") from e

    def log_review_snapshot(
        self,
        client_id: str,
        review_count: int,
        average_rating: float,
        snapshot_date: Optional[str] = None,
    ) -> int:
        snapshot_date = snapshot_date or date.today().isoformat()
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """INSERT INTO review_snapshots
                       (client_id, snapshot_date, review_count, average_rating)
                       VALUES (?, ?, ?, ?)""",
                    (client_id, snapshot_date, review_count, average_rating),
                )
                return cur.lastrowid
        except sqlite3.IntegrityError as e:
            raise ClientError(f"could not log snapshot for '{client_id}': {e}") from e

    def get_client_report(self, client_id: str) -> dict:
        with self._connect() as conn:
            client_row = conn.execute(
                "SELECT * FROM clients WHERE client_id = ?", (client_id,)
            ).fetchone()
            if client_row is None:
                raise ClientError(f"no such client: '{client_id}'")
            snapshots = conn.execute(
                """SELECT snapshot_date, review_count, average_rating
                   FROM review_snapshots
                   WHERE client_id = ?
                   ORDER BY snapshot_date ASC""",
                (client_id,),
            ).fetchall()

        if not snapshots:
            return {
                "client_id": client_id,
                "business_name": client_row["business_name"],
                "snapshot_count": 0,
                "message": "no snapshots logged yet",
            }

        first, latest = snapshots[0], snapshots[-1]
        return {
            "client_id": client_id,
            "business_name": client_row["business_name"],
            "snapshot_count": len(snapshots),
            "first_snapshot_date": first["snapshot_date"],
            "latest_snapshot_date": latest["snapshot_date"],
            "review_count_start": first["review_count"],
            "review_count_now": latest["review_count"],
            "review_count_gain": latest["review_count"] - first["review_count"],
            "average_rating_start": first["average_rating"],
            "average_rating_now": latest["average_rating"],
        }

    def get_mrr(self) -> int:
        with self._connect() as conn:
            total = conn.execute(
                "SELECT COALESCE(SUM(monthly_rate), 0) AS total FROM clients WHERE card_status = 'active'"
            ).fetchone()["total"]
        return total

    def clients_with_stripe_subscription(self) -> list[Client]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM clients WHERE stripe_subscription_id IS NOT NULL"
            ).fetchall()
        return [Client(**dict(row)) for row in rows]

    def update_card_status(self, client_id: str, card_status: str) -> None:
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    "UPDATE clients SET card_status = ? WHERE client_id = ?",
                    (card_status, client_id),
                )
                if cur.rowcount == 0:
                    raise ClientError(f"no such client: '{client_id}'")
        except sqlite3.IntegrityError as e:
            raise ClientError(f"could not update status for '{client_id}': {e}") from e

    def generate_dashboard_html(self) -> str:
        with self._connect() as conn:
            clients = conn.execute(
                "SELECT * FROM clients ORDER BY business_name ASC"
            ).fetchall()
            client_rows = []
            for client in clients:
                snapshots = conn.execute(
                    """SELECT snapshot_date, review_count, average_rating
                       FROM review_snapshots WHERE client_id = ?
                       ORDER BY snapshot_date ASC""",
                    (client["client_id"],),
                ).fetchall()
                client_rows.append((client, snapshots))

        status_counts = {"active": 0, "paused": 0, "canceled": 0}
        for client, _ in client_rows:
            status_counts[client["card_status"]] = status_counts.get(client["card_status"], 0) + 1

        mrr = self.get_mrr()
        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        table_rows = "\n".join(
            _render_client_row(client, snapshots) for client, snapshots in client_rows
        )
        if not table_rows:
            table_rows = '      <tr><td colspan="4" class="empty">No clients yet.</td></tr>'

        return _DASHBOARD_TEMPLATE.format(
            generated_at=generated_at,
            mrr=mrr,
            total_clients=len(client_rows),
            plural="" if len(client_rows) == 1 else "s",
            active_count=status_counts["active"],
            paused_count=status_counts["paused"],
            canceled_count=status_counts["canceled"],
            table_rows=table_rows,
        )


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Project Exodus client tracker")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add-client", help="Register a new client")
    p_add.add_argument("--client-id", required=True)
    p_add.add_argument("--business-name", required=True)
    p_add.add_argument("--contact-name")
    p_add.add_argument("--phone")
    p_add.add_argument("--email")
    p_add.add_argument("--signup-date", help="YYYY-MM-DD, defaults to today")
    p_add.add_argument("--card-status", default="active", choices=["active", "paused", "canceled"])
    p_add.add_argument("--stripe-subscription-id")
    p_add.add_argument("--monthly-rate", type=int, default=25)

    p_snap = sub.add_parser("log-snapshot", help="Record a review-count/rating check")
    p_snap.add_argument("--client-id", required=True)
    p_snap.add_argument("--review-count", type=int, required=True)
    p_snap.add_argument("--average-rating", type=float, required=True)
    p_snap.add_argument("--snapshot-date", help="YYYY-MM-DD, defaults to today")

    p_report = sub.add_parser("report", help="Review growth report for a client")
    p_report.add_argument("client_id")

    sub.add_parser("mrr", help="Total monthly recurring revenue across active clients")

    p_dash = sub.add_parser(
        "generate-dashboard", help="Render a static read-only HTML dashboard"
    )
    p_dash.add_argument("--out", type=Path, default=DEFAULT_DASHBOARD_PATH)

    args = parser.parse_args()
    tracker = ClientTracker(args.db)

    try:
        if args.command == "add-client":
            tracker.add_client(
                args.client_id,
                args.business_name,
                contact_name=args.contact_name,
                phone=args.phone,
                email=args.email,
                signup_date=args.signup_date,
                card_status=args.card_status,
                stripe_subscription_id=args.stripe_subscription_id,
                monthly_rate=args.monthly_rate,
            )
            print(f"added client '{args.client_id}'")
        elif args.command == "log-snapshot":
            snapshot_id = tracker.log_review_snapshot(
                args.client_id, args.review_count, args.average_rating, args.snapshot_date
            )
            print(f"logged snapshot id={snapshot_id} for '{args.client_id}'")
        elif args.command == "report":
            print(json.dumps(tracker.get_client_report(args.client_id), indent=2))
        elif args.command == "mrr":
            print(f"${tracker.get_mrr()}/month across active clients")
        elif args.command == "generate-dashboard":
            html = tracker.generate_dashboard_html()
            args.out.write_text(html)
            print(f"dashboard written to {args.out}")
    except ClientError as e:
        parser.exit(1, f"error: {e}\n")


if __name__ == "__main__":
    _cli()
