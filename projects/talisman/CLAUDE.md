# Project Talisman

AI Influencer Agency — synthetic content creator (Mika Reign, [@mikareign_](https://instagram.com/mikareign_)), building toward a sustained passive-income stream via sponsorships, affiliate, and a brand retainer. $3,000/month is a planning benchmark, not a guaranteed floor — see Founder review below. Character details live in `metadata.json` (an original, fully fictional persona — not modeled on any real person).

## Architecture

- **Storage:** single SQLite file (`data/talisman.db`), created on first run. No managed database — keeps hosting near $0.
- **Controller:** `src/queue_controller.py` — stdlib-only Python. CRUD for the content queue, post-metrics logging, sponsorship tracking, monthly revenue summary. `image_path` column + `list_pending_without_image()` / `set_image_path()` hook into image generation.
- **Image generation:** `src/image_generator.py` — fal.ai client (urllib only, no extra deps). Needs `FAL_KEY` in the environment. `FAL_MODEL_ID` / `FAL_LORA_URL` are overridable — verify the default model slug against fal.ai's current docs, endpoint IDs drift over time. `seed` subcommand generates a training set from `metadata.json` anchors; `fill-queue` generates images for every queued item missing one. Network calls retry transient failures (timeouts, connection errors, 429/5xx) with exponential backoff and fail fast on auth/validation errors; `fill-queue` isolates per-item failures so one bad item doesn't abort the batch — it's left for the next run to pick up. Safe to run unattended (cron/launchd) once `FAL_KEY` exists; actual posting to Instagram stays manual by design, not automated here.
- **Media kit:** `media-kit.md` / `media-kit.pdf` (rendered via gstack's `make-pdf`). Has placeholder audience stats — do not send to brands until real numbers are filled in.
- **Hosting budget (<$20/month):** cron/launchd on a local machine or a $5-6/month VPS once posting goes live. This is a *hosting* budget, not a growth budget — LoRA training (~$5-20 one-time) and per-image inference (fractions of a cent) are separate small costs, and any paid growth spend is a separate decision, not assumed here.
- **Compliance carries into image generation:** seed/training images must come from pure text-to-image generation off the anchors in `metadata.json` only — never from real people's photos as reference or training input. Reintroducing a real likeness at the image-gen layer defeats the point of the original-character decision already made for this project.

## Founder review — where this plan actually breaks

Honest risks to the "most success possible" goal, not just the happy path:

1. **Cold-start discovery is the biggest single risk.** A brand-new account gets close to zero organic reach. Static feed posts alone won't reach critical mass — see video note below.
2. **Video/Reels, not deferred.** IG's algorithm weights Reels heavily for discovery; treating video as a "later phase" add-on (as the original plan did) works against the growth goal. Short-form motion content should start in month 1, not after the account is already established.
3. **Differentiation matters more than polish.** Well-executed AI images of an attractive woman are no longer novel — the category is crowded. The "digital creator" framing itself (leaning into being AI, a consistent voice, recurring bits) is a real hook; pure visual quality isn't enough on its own.
4. **Sponsorship money requires proof first.** Brands pay for engaged reach they can see. Sequence gifted/barter partnerships before paid ones, and don't send the media kit until it has real numbers.
5. **Single revenue stream is fragile.** Relying only on brand sponsorships is a platform-risk single point of failure. Add an affiliate storefront (Amazon Influencer / LTK) from day one — it doesn't require negotiation and pays regardless of sponsorship deal flow.
6. **Manual posting is the bottleneck, so batch ahead of it.** Since posting/replying is manual, the pipeline should stay 2-4 weeks ahead in the queue so daily execution is "hit publish," not "generate then post."
7. **Set a checkpoint, not indefinite grinding.** At month 3, review followers/engagement against expectations honestly. If well short, that's a signal to change the hook/niche/cadence, not a reason to keep doing the same thing for another 3 months.

## Roadmap (ownership: 🤖 automated/me · 🧑 manual/you)

- [x] **Phase 0 — Scaffold** 🤖 `projects/talisman/` created (README, src/, metadata.json).
- [x] **Phase 1 — Brand identity** 🤖 character bible in `metadata.json`.
- [x] **Phase 2 — Automation core** 🤖 SQLite schema + `queue_controller.py`, tested.
- [x] **Phase 3a — Image pipeline built** 🤖 `image_generator.py` (fal.ai client) with retry/backoff and per-item failure isolation, tested against mocked API responses (transient recovery, permanent failure, fail-fast on auth errors, batch survives one bad item).
- [ ] **Phase 3b — Image pipeline live** 🧑 sign up at fal.ai, set `FAL_KEY`; 🤖 run `seed` to generate a training set; 🧑 pick best 15-20, kick off LoRA training (fal.ai or Replicate trainer); 🧑 set `FAL_LORA_URL`.
- [ ] **Phase 4 — First content batch** 🤖 run `fill-queue` to batch-generate 2-4 weeks of static content; 🧑 produce first short-form video/Reels (Kling/Runway) — start this in parallel, not after.
- [ ] **Phase 5 — Platform setup** 🧑 add AI-disclosure bio text from `metadata.json.compliance` before the first post; 🧑 begin daily posting/replying from the queue.
- [ ] **Phase 6 — Growth** 🧑 cross-post to TikTok, engage comments/DMs, lean into the digital-creator narrative; 🤖 log `post_metrics` weekly for visibility. **Checkpoint at month 3.**
- [ ] **Phase 7 — Monetize** 🧑 pursue gifted partnerships first, using `media-kit.pdf` once real numbers exist; 🧑 set up affiliate storefront; 🤖 track everything in `sponsorships`, run `summary` monthly.
- [ ] **Phase 8 — Optimize** 🤖 surface top performers from `post_metrics`; 🧑 convert proven traction into retainer deals.

## Data model (`data/talisman.db`)

- `content_queue` — platform, caption, image_prompt, scheduled_time, status (pending/posted), image_path, posted_at.
- `post_metrics` — impressions/likes/comments/shares/revenue_usd per posted item.
- `sponsorships` — brand, deliverable, fee_usd, status (prospecting/paid/etc.), due_date.

## Compliance (non-negotiable, day one)

Every post carries the AI/virtual-creator disclosure from `metadata.json.compliance.bio_disclosure_text`, plus `#ad` and platform-native branded-content labels on anything sponsored. This isn't optional polish — synthetic-persona monetization without disclosure is an FTC Endorsement Guides violation.

## CLI

```
# Queue
python3 src/queue_controller.py add --platform instagram --caption "..." --image-prompt "..." --scheduled-time 2026-08-05T09:00:00Z
python3 src/queue_controller.py list [--platform instagram]
python3 src/queue_controller.py mark-posted <content_id>
python3 src/queue_controller.py log-metrics <content_id> --impressions N --likes N --comments N --shares N --revenue N
python3 src/queue_controller.py add-sponsorship --brand "..." --deliverable "..." --fee N [--due-date YYYY-MM-DD]
python3 src/queue_controller.py summary YYYY-MM

# Images (needs FAL_KEY)
python3 src/image_generator.py seed --count 20
python3 src/image_generator.py fill-queue --db data/talisman.db
python3 src/image_generator.py generate --prompt "..." --out data/images/test.png
```
