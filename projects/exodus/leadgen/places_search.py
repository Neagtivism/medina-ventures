#!/usr/bin/env python3
"""Stage 1: batch-filter candidate leads via the Google Places API.

Standalone — no dependency on client_tracker.py, the website, or the
dashboard. Zero external packages (urllib only). Needs
GOOGLE_PLACES_API_KEY in the environment.

Usage:
    GOOGLE_PLACES_API_KEY=... python3 places_search.py \
        --query "bakery near San Diego, CA" \
        --query "boba shop near San Diego, CA" \
        --out leads_pass.json

Filter (constants below, easily adjustable):
    PASS if (no website OR website looks sparse/placeholder)
        AND review_count <= MAX_REVIEW_COUNT
        AND rating >= MIN_RATING
    FAIL otherwise.

Notes on data honesty:
- Places API does not expose a reliable "date opened" field. We record
  `opened_signal` as None always — it's a documented gap, not a silently
  faked heuristic. The council reasons from review count / status instead.
- "Sparse/placeholder website" is a heuristic, not a real classifier: we
  fetch the site with a short timeout and treat a failed request or a
  very small response body as "sparse." Real but plain small-business
  sites can occasionally trip this — it's a signal to weigh, not a fact.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Optional, TypeVar

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,"
    "places.nationalPhoneNumber,places.websiteUri,places.rating,"
    "places.userRatingCount,places.businessStatus"
)

# --- Adjustable filter thresholds ---
MAX_REVIEW_COUNT = 50
MIN_RATING = 3.5
SPARSE_SITE_MAX_BYTES = 2000
SITE_CHECK_TIMEOUT_SECONDS = 5

RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY_SECONDS = 1.0
RETRY_MAX_DELAY_SECONDS = 15.0

T = TypeVar("T")


class PlacesAPIError(RuntimeError):
    pass


def with_retry(func: Callable[[], T]) -> T:
    last_error: Optional[BaseException] = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return func()
        except urllib.error.HTTPError as e:
            if e.code == 429 or 500 <= e.code < 600:
                if attempt == RETRY_ATTEMPTS - 1:
                    raise
                last_error = e
            else:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            last_error = e
        delay = min(RETRY_MAX_DELAY_SECONDS, RETRY_BASE_DELAY_SECONDS * (2**attempt))
        time.sleep(delay + random.uniform(0, 0.5))
    raise last_error  # pragma: no cover


@dataclass
class Lead:
    place_id: str
    business_name: str
    address: str
    phone: Optional[str]
    website: Optional[str]
    rating: Optional[float]
    review_count: Optional[int]
    business_status: Optional[str]
    opened_signal: Optional[str]  # documented gap — see module docstring
    website_quality: str  # "none" | "sparse" | "present"


def check_website_sparse(url: str) -> bool:
    """Best-effort heuristic. Returns True if the site is unreachable or
    suspiciously small. Never raises — a broken site check should not
    crash the batch."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=SITE_CHECK_TIMEOUT_SECONDS) as resp:
            body = resp.read(SPARSE_SITE_MAX_BYTES + 1)
            return len(body) <= SPARSE_SITE_MAX_BYTES
    except Exception:
        return True


def search_places(query: str, api_key: str, max_results: int = 60) -> list[dict]:
    results: list[dict] = []
    page_token: Optional[str] = None
    while True:
        payload: dict = {"textQuery": query}
        if page_token:
            payload["pageToken"] = page_token

        def do_request() -> dict:
            req = urllib.request.Request(
                PLACES_SEARCH_URL,
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
                headers={
                    "X-Goog-Api-Key": api_key,
                    "X-Goog-FieldMask": FIELD_MASK + ",nextPageToken",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            data = with_retry(do_request)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise PlacesAPIError(f"Places API request failed ({e.code}): {detail}") from e
        except urllib.error.URLError as e:
            raise PlacesAPIError(f"Places API request failed (network error): {e}") from e

        results.extend(data.get("places", []))
        page_token = data.get("nextPageToken")
        if not page_token or len(results) >= max_results:
            break
        time.sleep(2)  # Google requires a short delay before a page token is valid.
    return results[:max_results]


def evaluate_lead(place: dict) -> Lead:
    website = place.get("websiteUri")
    if not website:
        website_quality = "none"
    elif check_website_sparse(website):
        website_quality = "sparse"
    else:
        website_quality = "present"

    return Lead(
        place_id=place.get("id", ""),
        business_name=place.get("displayName", {}).get("text", "unknown"),
        address=place.get("formattedAddress", ""),
        phone=place.get("nationalPhoneNumber"),
        website=website,
        rating=place.get("rating"),
        review_count=place.get("userRatingCount"),
        business_status=place.get("businessStatus"),
        opened_signal=None,
        website_quality=website_quality,
    )


def passes_filter(lead: Lead) -> bool:
    if lead.website_quality == "present":
        return False
    if lead.review_count is None or lead.review_count > MAX_REVIEW_COUNT:
        return False
    if lead.rating is None or lead.rating < MIN_RATING:
        return False
    return True


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Stage 1: Places API lead batch filter")
    parser.add_argument("--query", action="append", required=True, dest="queries",
                         help="Search text, e.g. 'bakery near San Diego, CA'. Repeatable.")
    parser.add_argument("--out", type=Path, default=Path("leads_pass.json"))
    parser.add_argument("--max-results", type=int, default=60, help="Per query")
    args = parser.parse_args()

    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        print("error: GOOGLE_PLACES_API_KEY is not set", file=sys.stderr)
        sys.exit(1)

    passed: list[Lead] = []
    seen_ids: set[str] = set()
    total_seen = 0

    for query in args.queries:
        try:
            places = search_places(query, api_key, args.max_results)
        except PlacesAPIError as e:
            print(f"'{query}' FAILED: {e}", file=sys.stderr)
            continue
        for place in places:
            lead = evaluate_lead(place)
            total_seen += 1
            if lead.place_id in seen_ids:
                continue
            seen_ids.add(lead.place_id)
            if passes_filter(lead):
                passed.append(lead)
        print(f"'{query}': {len(places)} results scanned")

    args.out.write_text(json.dumps([asdict(lead) for lead in passed], indent=2))
    print(f"\n{len(passed)}/{total_seen} candidates passed the filter -> {args.out}")


if __name__ == "__main__":
    _cli()
