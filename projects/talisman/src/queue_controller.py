#!/usr/bin/env python3
"""Content queue and sponsorship tracker for Project Talisman.

Backed by a local SQLite file so hosting stays near $0 (run via cron/launchd
on any $0-6/month box, well under the $20/month budget). No managed DB, no
external services.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "talisman.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS content_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT NOT NULL,
    caption         TEXT NOT NULL,
    image_prompt    TEXT NOT NULL,
    scheduled_time  TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    image_path      TEXT,
    posted_at       TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS post_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id      INTEGER NOT NULL REFERENCES content_queue(id),
    impressions     INTEGER NOT NULL DEFAULT 0,
    likes           INTEGER NOT NULL DEFAULT 0,
    comments        INTEGER NOT NULL DEFAULT 0,
    shares          INTEGER NOT NULL DEFAULT 0,
    revenue_usd     REAL NOT NULL DEFAULT 0,
    recorded_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sponsorships (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    brand           TEXT NOT NULL,
    deliverable     TEXT NOT NULL,
    fee_usd         REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'prospecting',
    due_date        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Matches list_pending()'s WHERE status = ? ORDER BY scheduled_time.
CREATE INDEX IF NOT EXISTS idx_content_queue_status_scheduled
    ON content_queue(status, scheduled_time);
CREATE INDEX IF NOT EXISTS idx_content_queue_platform
    ON content_queue(platform);
-- FK lookups from post_metrics -> content_queue, and the monthly summary's
-- strftime('%Y-%m', recorded_at) scan.
CREATE INDEX IF NOT EXISTS idx_post_metrics_content_id
    ON post_metrics(content_id);
CREATE INDEX IF NOT EXISTS idx_post_metrics_recorded_at
    ON post_metrics(recorded_at);
-- Matches monthly_revenue_summary()'s WHERE status = 'paid' AND due_date scan.
CREATE INDEX IF NOT EXISTS idx_sponsorships_status_due
    ON sponsorships(status, due_date);
"""


@dataclass
class ContentItem:
    id: int
    platform: str
    caption: str
    image_prompt: str
    scheduled_time: str
    status: str
    image_path: Optional[str]
    posted_at: Optional[str]


class QueueController:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(content_queue)")}
            if "image_path" not in columns:
                conn.execute("ALTER TABLE content_queue ADD COLUMN image_path TEXT")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add_content(
        self, platform: str, caption: str, image_prompt: str, scheduled_time: str
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO content_queue
                   (platform, caption, image_prompt, scheduled_time)
                   VALUES (?, ?, ?, ?)""",
                (platform, caption, image_prompt, scheduled_time),
            )
            return cur.lastrowid

    _CONTENT_COLUMNS = (
        "id, platform, caption, image_prompt, scheduled_time, status, image_path, posted_at"
    )

    def list_pending(self, platform: Optional[str] = None) -> list[ContentItem]:
        query = f"SELECT {self._CONTENT_COLUMNS} FROM content_queue WHERE status = 'pending'"
        params: tuple = ()
        if platform:
            query += " AND platform = ?"
            params = (platform,)
        query += " ORDER BY scheduled_time ASC"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [ContentItem(**dict(row)) for row in rows]

    def list_pending_without_image(self) -> list[ContentItem]:
        query = (
            f"SELECT {self._CONTENT_COLUMNS} FROM content_queue "
            "WHERE status = 'pending' AND image_path IS NULL ORDER BY scheduled_time ASC"
        )
        with self._connect() as conn:
            rows = conn.execute(query).fetchall()
        return [ContentItem(**dict(row)) for row in rows]

    def set_image_path(self, content_id: int, image_path: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE content_queue SET image_path = ? WHERE id = ?",
                (image_path, content_id),
            )

    def mark_posted(self, content_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """UPDATE content_queue
                   SET status = 'posted', posted_at = ?
                   WHERE id = ?""",
                (datetime.now(timezone.utc).isoformat(), content_id),
            )

    def log_metrics(
        self,
        content_id: int,
        impressions: int = 0,
        likes: int = 0,
        comments: int = 0,
        shares: int = 0,
        revenue_usd: float = 0.0,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO post_metrics
                   (content_id, impressions, likes, comments, shares, revenue_usd)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (content_id, impressions, likes, comments, shares, revenue_usd),
            )

    def add_sponsorship(
        self, brand: str, deliverable: str, fee_usd: float, due_date: Optional[str] = None
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO sponsorships (brand, deliverable, fee_usd, due_date)
                   VALUES (?, ?, ?, ?)""",
                (brand, deliverable, fee_usd, due_date),
            )
            return cur.lastrowid

    def monthly_revenue_summary(self, year_month: str) -> dict:
        """year_month like '2026-08'."""
        with self._connect() as conn:
            metrics_total = conn.execute(
                """SELECT COALESCE(SUM(revenue_usd), 0) AS total
                   FROM post_metrics WHERE strftime('%Y-%m', recorded_at) = ?""",
                (year_month,),
            ).fetchone()["total"]
            sponsorship_total = conn.execute(
                """SELECT COALESCE(SUM(fee_usd), 0) AS total
                   FROM sponsorships
                   WHERE status = 'paid' AND strftime('%Y-%m', due_date) = ?""",
                (year_month,),
            ).fetchone()["total"]
        total = metrics_total + sponsorship_total
        return {
            "month": year_month,
            "content_revenue_usd": metrics_total,
            "sponsorship_revenue_usd": sponsorship_total,
            "total_usd": total,
            "target_usd": 3000,
            "target_met": total >= 3000,
        }


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Project Talisman content queue controller")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Queue a new content item")
    p_add.add_argument("--platform", required=True)
    p_add.add_argument("--caption", required=True)
    p_add.add_argument("--image-prompt", required=True)
    p_add.add_argument("--scheduled-time", required=True, help="ISO 8601 timestamp")

    p_list = sub.add_parser("list", help="List pending content")
    p_list.add_argument("--platform")

    p_posted = sub.add_parser("mark-posted", help="Mark a content item as posted")
    p_posted.add_argument("content_id", type=int)

    p_metrics = sub.add_parser("log-metrics", help="Log metrics for a posted item")
    p_metrics.add_argument("content_id", type=int)
    p_metrics.add_argument("--impressions", type=int, default=0)
    p_metrics.add_argument("--likes", type=int, default=0)
    p_metrics.add_argument("--comments", type=int, default=0)
    p_metrics.add_argument("--shares", type=int, default=0)
    p_metrics.add_argument("--revenue", type=float, default=0.0)

    p_sponsor = sub.add_parser("add-sponsorship", help="Log a sponsorship deal")
    p_sponsor.add_argument("--brand", required=True)
    p_sponsor.add_argument("--deliverable", required=True)
    p_sponsor.add_argument("--fee", type=float, required=True)
    p_sponsor.add_argument("--due-date")

    p_summary = sub.add_parser("summary", help="Monthly revenue summary")
    p_summary.add_argument("year_month", help="YYYY-MM")

    args = parser.parse_args()
    controller = QueueController(args.db)

    if args.command == "add":
        content_id = controller.add_content(
            args.platform, args.caption, args.image_prompt, args.scheduled_time
        )
        print(f"queued content_id={content_id}")
    elif args.command == "list":
        for item in controller.list_pending(args.platform):
            print(f"[{item.id}] {item.platform} @ {item.scheduled_time} — {item.caption[:60]}")
    elif args.command == "mark-posted":
        controller.mark_posted(args.content_id)
        print(f"content_id={args.content_id} marked posted")
    elif args.command == "log-metrics":
        controller.log_metrics(
            args.content_id, args.impressions, args.likes, args.comments, args.shares, args.revenue
        )
        print(f"metrics logged for content_id={args.content_id}")
    elif args.command == "add-sponsorship":
        sponsorship_id = controller.add_sponsorship(
            args.brand, args.deliverable, args.fee, args.due_date
        )
        print(f"sponsorship_id={sponsorship_id}")
    elif args.command == "summary":
        print(json.dumps(controller.monthly_revenue_summary(args.year_month), indent=2))


if __name__ == "__main__":
    _cli()
