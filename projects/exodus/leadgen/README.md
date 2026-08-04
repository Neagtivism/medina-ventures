# Lead Qualification Tool

Standalone internal tool for finding and ranking local-business leads (bakeries, boba shops, cafes, and similar presence-dependent small businesses) worth pitching the NFC review card + retainer to.

**Not connected to `client_tracker.py`, the website, or the dashboard.** Run it, read the report, act on it manually. No auto-anything.

## How it works

**Stage 1 — batch filter** (`places_search.py`): pulls candidates from the Google Places API for search terms you give it, then applies a cheap pass/fail filter — no website (or a sparse/placeholder one), review count under a threshold, rating 3.5+. Fast, no LLM cost.

**Stage 2 — the council** (`council.py`): the Stage 1 PASS list goes through 4 independent Claude calls, each with a different lens — not a heavyweight agent framework, just 4 distinct system prompts reasoning over the same batch:
- **Presence Gap** — how big the online-presence gap is
- **Conversion Likelihood** — signals the business is newer/growing/open to new tools
- **Red Flag** — reasons to deprioritize (chain, already has a review system, doesn't fit the profile)
- **Wildcard** — flags borderline/interesting cases and is explicitly allowed to say "unsure" instead of forcing a verdict

The 4 outputs are combined **deterministically in Python** (not a 5th "synthesizer" call — the combination logic is auditable, not another black box). Businesses where the opportunity score is high *and* the red-flag severity is high are pulled into a separate "mixed signal" section instead of being averaged into a falsely confident rank.

## Setup

```
export GOOGLE_PLACES_API_KEY=...   # https://console.cloud.google.com/apis/credentials
export ANTHROPIC_API_KEY=...
```

Both are paid APIs — Places API Text Search has a per-request cost beyond a small monthly credit; Claude calls cost per the 4 reviewer passes per batch. Not free to run repeatedly at scale, but cheap for on-demand batches.

## Usage

```
python3 places_search.py \
  --query "bakery near San Diego, CA" \
  --query "boba shop near San Diego, CA" \
  --out leads_pass.json

python3 council.py --in leads_pass.json --out report.md
```

Run Stage 1 first and look at `leads_pass.json` before spending API calls on Stage 2 — that's why they're separate scripts, not one pipeline.

## Known limitations (honest, not swept under the rug)

- **`opened_signal` is always `null`.** The Places API doesn't expose a reliable "date opened" field. The Conversion Likelihood reviewer works from review count and business status instead — this isn't a real signal, don't expect it to be filled in later without a different data source.
- **"Sparse website" is a heuristic, not a classifier.** A failed request or a response under ~2KB is treated as sparse. A legitimately lean-but-fine small-business site can occasionally trip this. It's a signal to weigh, not a fact — that's exactly what the council is for.
- **Duplicate businesses across overlapping search queries are deduped by `place_id`** within a single Stage 1 run, not across separate runs on different days.
