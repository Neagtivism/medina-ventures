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
<title>Exodus Dashboard</title>
<style>
  :root {{
    --bg: #f7f7f5; --surface: #ffffff; --border: #e5e5e0;
    --text: #1a1a1a; --text-muted: #6b6b66; --accent: #2f6f4f;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #121212; --surface: #1a1a1a; --border: #2e2e2e;
      --text: #f2f2f0; --text-muted: #9a9a95; --accent: #7fd9a8;
    }}
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    padding: 32px 20px; line-height: 1.5;
  }}
  .container {{ max-width: 880px; margin: 0 auto; }}
  h1 {{ font-size: 1.4rem; margin-bottom: 4px; }}
  .subtitle {{ color: var(--text-muted); font-size: 0.85rem; margin-bottom: 28px; }}
  .stats {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 32px; }}
  .stat-card {{
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
    padding: 16px 20px; min-width: 140px; flex: 1;
  }}
  .stat-value {{ font-size: 1.6rem; font-weight: 600; color: var(--accent); }}
  .stat-label {{ font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.04em; }}
  table {{
    width: 100%; border-collapse: collapse; background: var(--surface);
    border: 1px solid var(--border); border-radius: 10px; overflow: hidden;
  }}
  .table-wrap {{ overflow-x: auto; border-radius: 10px; }}
  th, td {{ text-align: left; padding: 10px 14px; font-size: 0.88rem; border-bottom: 1px solid var(--border); }}
  th {{ color: var(--text-muted); font-weight: 500; font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.03em; }}
  tr:last-child td {{ border-bottom: none; }}
  .status {{ padding: 2px 8px; border-radius: 999px; font-size: 0.75rem; }}
  .status-active {{ background: rgba(47,111,79,0.15); color: var(--accent); }}
  .status-paused {{ background: rgba(180,140,20,0.15); color: #b48c14; }}
  .status-canceled {{ background: rgba(180,40,40,0.15); color: #b42828; }}
  .empty {{ color: var(--text-muted); text-align: center; padding: 24px; }}
</style>
</head>
<body>
<div class="container">
  <h1>Exodus — Client Dashboard</h1>
  <div class="subtitle">Generated {generated_at} · read-only snapshot</div>
  <div class="stats">
    <div class="stat-card"><div class="stat-value">${mrr}</div><div class="stat-label">MRR</div></div>
    <div class="stat-card"><div class="stat-value">{total_clients}</div><div class="stat-label">Total clients</div></div>
    <div class="stat-card"><div class="stat-value">{active_count}</div><div class="stat-label">Active</div></div>
    <div class="stat-card"><div class="stat-value">{paused_count}</div><div class="stat-label">Paused</div></div>
    <div class="stat-card"><div class="stat-value">{canceled_count}</div><div class="stat-label">Canceled</div></div>
  </div>
  <div class="table-wrap">
  <table>
    <thead>
      <tr><th>Business</th><th>Status</th><th>Rate</th><th>Review growth</th></tr>
    </thead>
    <tbody>
{table_rows}
    </tbody>
  </table>
  </div>
</div>
</body>
</html>
"""


def _render_client_row(client: sqlite3.Row, snapshots: list[sqlite3.Row]) -> str:
    if snapshots:
        first, latest = snapshots[0], snapshots[-1]
        gain = latest["review_count"] - first["review_count"]
        gain_str = f"+{gain}" if gain >= 0 else str(gain)
        growth = f'{gain_str} reviews ({first["review_count"]}→{latest["review_count"]}) · {latest["average_rating"]:.1f}★'
    else:
        growth = "no snapshots yet"
    return (
        "      <tr>"
        f'<td>{escape(client["business_name"])}</td>'
        f'<td><span class="status status-{escape(client["card_status"])}">{escape(client["card_status"])}</span></td>'
        f'<td>${client["monthly_rate"]}/mo</td>'
        f"<td>{escape(growth)}</td>"
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
            table_rows = '<tr><td colspan="4" class="empty">No clients yet.</td></tr>'

        return _DASHBOARD_TEMPLATE.format(
            generated_at=generated_at,
            mrr=mrr,
            total_clients=len(client_rows),
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
