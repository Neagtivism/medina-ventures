# Project Talisman

AI Influencer Agency — synthetic content creator, target $3,000/month recurring baseline via sponsorships, affiliate, and a brand retainer. Character details live in `metadata.json` (an original, fully fictional persona — not modeled on any real person).

## Architecture

- **Storage:** single SQLite file (`data/talisman.db`), created on first run. No managed database — keeps hosting near $0.
- **Controller:** `src/queue_controller.py` — stdlib-only Python (sqlite3, argparse). CRUD for the content queue, post-metrics logging, sponsorship tracking, and a monthly revenue summary against the $3,000 target.
- **Hosting budget (<$20/month):** run the controller via cron/launchd. A local machine or a $0 CI cron job covers development; a $5-6/month VPS (Hetzner/DigitalOcean) is enough for 24/7 scheduling once posting goes live. No component in this stack requires more than that.
- **Out of scope here:** actual image generation and platform posting/API integration. `metadata.json.visual_profile.image_prompt_anchors` holds the text prompts to feed into whatever image-gen tool is chosen later; this repo only tracks the queue and the numbers.

## Data model (`data/talisman.db`)

- `content_queue` — platform, caption, image_prompt, scheduled_time, status (pending/posted), posted_at.
- `post_metrics` — impressions/likes/comments/shares/revenue_usd per posted item.
- `sponsorships` — brand, deliverable, fee_usd, status (prospecting/paid/etc.), due_date.

## Roadmap

- [x] **Phase 0 — Scaffold:** `projects/talisman/` created (README, src/, metadata.json).
- [x] **Phase 1 — Brand identity:** character bible defined in `metadata.json` (identity, visual profile, lore, niche, monetization targets, compliance).
- [x] **Phase 2 — Automation core:** SQLite schema + `queue_controller.py` (add/list/mark-posted/log-metrics/add-sponsorship/summary), smoke-tested.
- [ ] **Phase 3 — Content pipeline:** wire an image-generation tool to `image_prompt_anchors`, generate/queue a first content batch.
- [ ] **Phase 4 — Platform setup:** create accounts under the chosen handle (verify availability/trademark first), add the AI-disclosure bio text from `metadata.json.compliance` before the first post.
- [ ] **Phase 5 — Monetization ops:** start logging real `sponsorships` and `post_metrics`; run `summary` monthly against the $3,000 target.
- [ ] **Phase 6 — Optimize:** review top-performing content by logged metrics, adjust `content_pillars` and posting cadence.

## Compliance (non-negotiable, day one)

Every post carries the AI/virtual-creator disclosure from `metadata.json.compliance.bio_disclosure_text`, plus `#ad` and platform-native branded-content labels on anything sponsored. This isn't optional polish — synthetic-persona monetization without disclosure is an FTC Endorsement Guides violation.

## CLI

```
python3 src/queue_controller.py add --platform instagram --caption "..." --image-prompt "..." --scheduled-time 2026-08-05T09:00:00Z
python3 src/queue_controller.py list [--platform instagram]
python3 src/queue_controller.py mark-posted <content_id>
python3 src/queue_controller.py log-metrics <content_id> --impressions N --likes N --comments N --shares N --revenue N
python3 src/queue_controller.py add-sponsorship --brand "..." --deliverable "..." --fee N [--due-date YYYY-MM-DD]
python3 src/queue_controller.py summary YYYY-MM
```
