#!/usr/bin/env python3
"""Stripe subscription-status sync for Project Exodus.

Zero external dependencies (urllib only), matching client_tracker.py.
Requires STRIPE_API_KEY in the environment — use a *restricted*, read-only
key (subscriptions:read is enough; this module never writes to Stripe),
never the full secret key. This repo is public — the key must only ever
come from the environment, never be committed.

Usage:
    STRIPE_API_KEY=rk_... python3 src/stripe_sync.py sync --db data/exodus.db
    STRIPE_API_KEY=rk_... python3 src/stripe_sync.py status <stripe_subscription_id>
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from client_tracker import ClientTracker, DEFAULT_DB_PATH, with_retry  # noqa: E402

STRIPE_API_BASE = "https://api.stripe.com/v1"

# Stripe has more subscription states than our 3-value card_status. This is
# a deliberate simplification: anything not clearly active or clearly gone
# maps to "paused" so it surfaces for a human to look at, rather than
# silently staying "active" while a payment is actually failing.
STRIPE_STATUS_MAP = {
    "active": "active",
    "trialing": "active",
    "past_due": "paused",
    "unpaid": "paused",
    "paused": "paused",
    "incomplete": "paused",
    "incomplete_expired": "canceled",
    "canceled": "canceled",
}


class StripeAPIError(RuntimeError):
    pass


class StripeClient:
    def __init__(self, api_key: Optional[str] = None, timeout: int = 30):
        self.api_key = api_key or os.environ.get("STRIPE_API_KEY")
        if not self.api_key:
            raise StripeAPIError(
                "STRIPE_API_KEY is not set. Use a restricted, read-only key "
                "from https://dashboard.stripe.com/apikeys"
            )
        self.timeout = timeout

    def get_subscription_status(self, subscription_id: str) -> str:
        url = f"{STRIPE_API_BASE}/subscriptions/{subscription_id}"
        auth = base64.b64encode(f"{self.api_key}:".encode()).decode()

        def do_request() -> dict:
            req = urllib.request.Request(
                url, headers={"Authorization": f"Basic {auth}"}
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                # HTTPError is a subclass of URLError — it must be resolved
                # here, before with_retry's generic URLError branch, or a
                # permanent error like a bad key (401) gets retried forever
                # instead of failing fast.
                if e.code == 429 or 500 <= e.code < 600:
                    raise ConnectionError(f"transient stripe error ({e.code})") from e
                detail = e.read().decode("utf-8", errors="replace")
                raise StripeAPIError(
                    f"stripe request failed for '{subscription_id}' ({e.code}): {detail}"
                ) from e

        try:
            result = with_retry(
                do_request,
                retryable=(urllib.error.URLError, TimeoutError, ConnectionError),
            )
        except urllib.error.URLError as e:
            raise StripeAPIError(f"stripe request failed (network error): {e}") from e

        status = result.get("status")
        if status is None:
            raise StripeAPIError(f"no status in response for '{subscription_id}': {result}")
        return status


def map_status(stripe_status: str) -> str:
    return STRIPE_STATUS_MAP.get(stripe_status, "paused")


def sync_all(tracker: ClientTracker, client: StripeClient) -> dict:
    clients = tracker.clients_with_stripe_subscription()
    updated, unchanged, failed = [], [], []
    for c in clients:
        try:
            stripe_status = client.get_subscription_status(c.stripe_subscription_id)
        except StripeAPIError as e:
            failed.append({"client_id": c.client_id, "error": str(e)})
            continue
        mapped = map_status(stripe_status)
        if mapped != c.card_status:
            tracker.update_card_status(c.client_id, mapped)
            updated.append(
                {"client_id": c.client_id, "from": c.card_status, "to": mapped, "stripe_status": stripe_status}
            )
        else:
            unchanged.append(c.client_id)
    return {"updated": updated, "unchanged": unchanged, "failed": failed}


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Project Exodus Stripe subscription sync")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="Sync card_status for every client with a Stripe subscription")
    p_sync.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)

    p_status = sub.add_parser("status", help="Look up a single subscription's Stripe status")
    p_status.add_argument("subscription_id")

    args = parser.parse_args()

    try:
        stripe_client = StripeClient()
    except StripeAPIError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.command == "status":
        try:
            status = stripe_client.get_subscription_status(args.subscription_id)
        except StripeAPIError as e:
            print(f"error: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"{args.subscription_id}: {status} -> card_status={map_status(status)}")

    elif args.command == "sync":
        tracker = ClientTracker(args.db)
        result = sync_all(tracker, stripe_client)
        for u in result["updated"]:
            print(f"client_id={u['client_id']}: {u['from']} -> {u['to']} (stripe: {u['stripe_status']})")
        for f in result["failed"]:
            print(f"client_id={f['client_id']} FAILED: {f['error']}", file=sys.stderr)
        print(
            f"{len(result['updated'])} updated, {len(result['unchanged'])} unchanged, "
            f"{len(result['failed'])} failed"
        )


if __name__ == "__main__":
    _cli()
