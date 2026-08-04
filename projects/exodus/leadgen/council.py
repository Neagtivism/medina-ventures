#!/usr/bin/env python3
"""Stage 2: four-reviewer reasoning council over Stage 1's PASS list.

Standalone — no dependency on client_tracker.py, the website, or the
dashboard. Zero external packages (urllib only). Needs ANTHROPIC_API_KEY
in the environment.

Four distinct Claude calls, not a multi-agent framework: each gets the
same lead batch with a different system prompt (lens), returns
structured JSON keyed by place_id, and the results are combined
*deterministically in Python* — no fifth "synthesizer" call, so the
combination logic is auditable rather than another black box.

Disagreement between reviewers is surfaced explicitly as "mixed signal
— human review needed," not averaged into a falsely confident score.

Usage:
    ANTHROPIC_API_KEY=... python3 council.py --in leads_pass.json --out report.md
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, TypeVar

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
MAX_TOKENS = 4096

TOP_LIST_MAX = 15
# A business is "mixed signal" (excluded from the ranked list, shown
# separately) when the red-flag severity is meaningfully high *while*
# the opportunity signals are also high — i.e. the reviewers actually
# disagree, not just score things a bit differently.
MIXED_SIGNAL_REDFLAG_THRESHOLD = 6
MIXED_SIGNAL_OPPORTUNITY_THRESHOLD = 6

RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY_SECONDS = 2.0
RETRY_MAX_DELAY_SECONDS = 20.0

T = TypeVar("T")


class ClaudeAPIError(RuntimeError):
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


def call_claude(system_prompt: str, user_content: str, api_key: str, model: str = DEFAULT_MODEL) -> str:
    payload = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_content}],
    }

    def do_request() -> dict:
        req = urllib.request.Request(
            ANTHROPIC_API_URL,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        result = with_retry(do_request)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise ClaudeAPIError(f"Claude request failed ({e.code}): {detail}") from e
    except urllib.error.URLError as e:
        raise ClaudeAPIError(f"Claude request failed (network error): {e}") from e

    blocks = result.get("content", [])
    text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
    if not text:
        raise ClaudeAPIError(f"empty response: {result}")
    return text


def _extract_json(text: str) -> list:
    """Reviewers are told to return raw JSON, but strip markdown fences
    defensively in case a model wraps it anyway."""
    stripped = text.strip()
    match = re.search(r"```(?:json)?\s*(\[.*\])\s*```", stripped, re.S)
    if match:
        stripped = match.group(1)
    return json.loads(stripped)


PRESENCE_GAP_SYSTEM = """You are the "Presence Gap" reviewer for a local-business lead qualification tool. \
We sell a physical Google Review NFC card + a monthly review/reputation retainer. \
Your ONLY job: judge how large the online-presence gap is for each business — no website or a \
weak one, combined with decent existing reviews, means high priority (they already have real \
customers but no digital presence to show for it, which is exactly our pitch).

You will receive a JSON array of candidate businesses. For EACH business, respond with an entry in a \
JSON array, keyed by place_id:
[{"place_id": "...", "score": <0-10 integer, 10 = biggest opportunity>, "verdict": "pursue"|"unsure"|"skip", "note": "<one sentence, specific to this business>"}]

Respond with ONLY the JSON array. No prose, no markdown fences, no explanation outside the JSON."""

CONVERSION_LIKELIHOOD_SYSTEM = """You are the "Conversion Likelihood" reviewer for a local-business lead \
qualification tool. We sell a physical Google Review NFC card + a monthly review/reputation retainer. \
Your ONLY job: judge how likely each business is to actually say yes and adopt a new tool — signals like \
a newer/growing business, moderate-but-climbing review count, an owner-operator feel (vs. large chain \
rigidity), and general openness-to-new-things energy inferred from what's in the data.

You will receive a JSON array of candidate businesses. For EACH business, respond with an entry in a \
JSON array, keyed by place_id:
[{"place_id": "...", "score": <0-10 integer, 10 = very likely to convert>, "verdict": "pursue"|"unsure"|"skip", "note": "<one sentence, specific to this business>"}]

Respond with ONLY the JSON array. No prose, no markdown fences, no explanation outside the JSON."""

RED_FLAG_SYSTEM = """You are the "Red Flag" reviewer for a local-business lead qualification tool. We sell a \
physical Google Review NFC card + a monthly review/reputation retainer. Your ONLY job: look for reasons to \
DEPRIORITIZE each business — signs of being a large chain (chains don't need this and won't have a single \
decision-maker), signs they already have a review/reputation system, a review count or rating pattern that \
suggests this isn't actually a fit, or anything else that contradicts our real target profile (small, \
owner-operated, presence-dependent local businesses like bakeries/cafes/boba shops).

You will receive a JSON array of candidate businesses. For EACH business, respond with an entry in a \
JSON array, keyed by place_id:
[{"place_id": "...", "severity": <0-10 integer, 0 = no concerns, 10 = definitely skip>, "note": "<one sentence: the specific red flag, or \\"no concerns\\" if none>"}]

Respond with ONLY the JSON array. No prose, no markdown fences, no explanation outside the JSON."""

WILDCARD_SYSTEM = """You are the "Wildcard" reviewer for a local-business lead qualification tool. We sell a \
physical Google Review NFC card + a monthly review/reputation retainer to small local businesses. The other \
three reviewers already score every business on presence gap, conversion likelihood, and red flags. Your job \
is different: only speak up about businesses that are borderline, surprising, or genuinely hard to call — \
something interesting the structured scores might miss, or a business where you are honestly unsure and think \
a human should look at it themselves rather than trust a score. Do NOT try to cover every business — most \
businesses need no wildcard note at all. It is completely fine, and expected, to flag very few (or zero).

You will receive a JSON array of candidate businesses. Respond with a JSON array containing ONLY the \
businesses worth flagging:
[{"place_id": "...", "note": "<one or two sentences: what's interesting or uncertain, and why a human should look>"}]

Respond with ONLY the JSON array (it may be empty: []). No prose, no markdown fences."""


@dataclass
class CombinedLead:
    place_id: str
    business_name: str
    address: str
    phone: Optional[str]
    presence_score: int
    presence_note: str
    conversion_score: int
    conversion_note: str
    redflag_severity: int
    redflag_note: str
    wildcard_note: Optional[str]
    final_score: int
    mixed_signal: bool


def run_council(leads: list[dict], api_key: str, model: str = DEFAULT_MODEL) -> list[CombinedLead]:
    lead_payload = json.dumps(
        [
            {
                "place_id": l["place_id"],
                "business_name": l["business_name"],
                "address": l["address"],
                "website_quality": l["website_quality"],
                "rating": l["rating"],
                "review_count": l["review_count"],
                "business_status": l["business_status"],
                "opened_signal": l["opened_signal"],
            }
            for l in leads
        ],
        indent=2,
    )
    user_content = f"Candidate businesses:\n{lead_payload}"

    print("Running Presence Gap review...")
    presence_raw = call_claude(PRESENCE_GAP_SYSTEM, user_content, api_key, model)
    print("Running Conversion Likelihood review...")
    conversion_raw = call_claude(CONVERSION_LIKELIHOOD_SYSTEM, user_content, api_key, model)
    print("Running Red Flag review...")
    redflag_raw = call_claude(RED_FLAG_SYSTEM, user_content, api_key, model)
    print("Running Wildcard review...")
    wildcard_raw = call_claude(WILDCARD_SYSTEM, user_content, api_key, model)

    presence = {e["place_id"]: e for e in _extract_json(presence_raw)}
    conversion = {e["place_id"]: e for e in _extract_json(conversion_raw)}
    redflag = {e["place_id"]: e for e in _extract_json(redflag_raw)}
    wildcard = {e["place_id"]: e for e in _extract_json(wildcard_raw)}

    combined = []
    for lead in leads:
        pid = lead["place_id"]
        p = presence.get(pid, {"score": 0, "note": "no presence-gap data returned"})
        c = conversion.get(pid, {"score": 0, "note": "no conversion data returned"})
        r = redflag.get(pid, {"severity": 0, "note": "no red-flag data returned"})
        w = wildcard.get(pid)

        presence_score = int(p.get("score", 0))
        conversion_score = int(c.get("score", 0))
        redflag_severity = int(r.get("severity", 0))
        final_score = presence_score + conversion_score - redflag_severity
        opportunity_avg = (presence_score + conversion_score) / 2
        mixed_signal = (
            redflag_severity >= MIXED_SIGNAL_REDFLAG_THRESHOLD
            and opportunity_avg >= MIXED_SIGNAL_OPPORTUNITY_THRESHOLD
        )

        combined.append(
            CombinedLead(
                place_id=pid,
                business_name=lead["business_name"],
                address=lead["address"],
                phone=lead.get("phone"),
                presence_score=presence_score,
                presence_note=p.get("note", ""),
                conversion_score=conversion_score,
                conversion_note=c.get("note", ""),
                redflag_severity=redflag_severity,
                redflag_note=r.get("note", ""),
                wildcard_note=w.get("note") if w else None,
                final_score=final_score,
                mixed_signal=mixed_signal,
            )
        )
    return combined


def generate_report(combined: list[CombinedLead]) -> str:
    clear = sorted(
        [c for c in combined if not c.mixed_signal], key=lambda c: c.final_score, reverse=True
    )
    mixed = [c for c in combined if c.mixed_signal]
    ranked = clear[:TOP_LIST_MAX]

    lines = ["# Lead Qualification Report", ""]
    lines.append(f"{len(combined)} candidates reviewed · {len(ranked)} ranked · {len(mixed)} mixed-signal\n")

    lines.append("## Ranked Recommendations\n")
    if not ranked:
        lines.append("_No clear-signal leads this batch._\n")
    for i, c in enumerate(ranked, 1):
        lines.append(f"### {i}. {c.business_name} (score {c.final_score})")
        lines.append(f"- **Address:** {c.address}")
        if c.phone:
            lines.append(f"- **Phone:** {c.phone}")
        lines.append(f"- **Presence gap ({c.presence_score}/10):** {c.presence_note}")
        lines.append(f"- **Conversion likelihood ({c.conversion_score}/10):** {c.conversion_note}")
        if c.redflag_severity > 0:
            lines.append(f"- **Red flags ({c.redflag_severity}/10):** {c.redflag_note}")
        if c.wildcard_note:
            lines.append(f"- **⚡ Wildcard:** {c.wildcard_note}")
        lines.append("")

    if mixed:
        lines.append("## Mixed Signal — Human Review Needed\n")
        lines.append(
            "_Reviewers disagreed significantly on these — high opportunity score "
            "alongside a serious red flag. Not ranked; use your judgment.\n_"
        )
        for c in mixed:
            lines.append(f"### {c.business_name}")
            lines.append(f"- **Address:** {c.address}")
            if c.phone:
                lines.append(f"- **Phone:** {c.phone}")
            lines.append(f"- **Presence gap ({c.presence_score}/10):** {c.presence_note}")
            lines.append(f"- **Conversion likelihood ({c.conversion_score}/10):** {c.conversion_note}")
            lines.append(f"- **Red flags ({c.redflag_severity}/10):** {c.redflag_note}")
            if c.wildcard_note:
                lines.append(f"- **⚡ Wildcard:** {c.wildcard_note}")
            lines.append("")

    unranked_wildcards = [c for c in combined if c.wildcard_note and c not in ranked and c not in mixed]
    if unranked_wildcards:
        lines.append("## Other Wildcard Notes\n")
        lines.append("_Flagged as interesting/uncertain but didn't make the ranked list._\n")
        for c in unranked_wildcards:
            lines.append(f"- **{c.business_name}** ({c.address}): {c.wildcard_note}")
        lines.append("")

    return "\n".join(lines)


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Stage 2: council review of Stage 1 leads")
    parser.add_argument("--in", type=Path, required=True, dest="input_path")
    parser.add_argument("--out", type=Path, default=Path("report.md"))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("error: ANTHROPIC_API_KEY is not set", file=sys.stderr)
        sys.exit(1)

    leads = json.loads(args.input_path.read_text())
    if not leads:
        print("no leads in input file — nothing to review", file=sys.stderr)
        sys.exit(1)

    try:
        combined = run_council(leads, api_key, args.model)
    except ClaudeAPIError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    report = generate_report(combined)
    args.out.write_text(report)
    print(f"\nreport written to {args.out}")


if __name__ == "__main__":
    _cli()
