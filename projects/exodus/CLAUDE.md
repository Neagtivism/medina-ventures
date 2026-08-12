# Project Exodus

Local B2B services: physical Google Review NFC cards → monthly analytics retainer → upsell to custom sites / Retell AI voice systems. See `outreach/` for cold-pitch scripts and the lead-tracker template.

## Architecture

- **Site:** `index.html`, deployed via `.github/workflows/pages-deploy.yml` (GitHub Actions → GitHub Pages, publishing this directory only).
- **Client tracker:** `src/client_tracker.py` — stdlib-only Python/SQLite. `clients` + `review_snapshots` tables, `add_client()`, `log_review_snapshot()`, `get_client_report()`, `get_mrr()`, `generate_dashboard_html()` (CLI: `generate-dashboard`). Indexed on the actual query patterns, verified via `EXPLAIN QUERY PLAN`.
- **Stripe sync:** `src/stripe_sync.py` — stdlib-only, reuses `client_tracker.with_retry()`. Reads `STRIPE_API_KEY` from the environment (must be a **restricted, read-only** key — `subscriptions:read` is enough; this module never writes to Stripe). Maps Stripe subscription status → our 3-value `card_status`:
  - `active`, `trialing` → `active`
  - `past_due`, `unpaid`, `paused`, `incomplete`, and any unrecognized future status → `paused` (surfaces for a human to check rather than silently staying "active")
  - `canceled`, `incomplete_expired` → `canceled`
  - `sync` command updates every client with a `stripe_subscription_id`; clients without one (e.g. paying by other means) are left untouched. Per-client failures (e.g. a deleted subscription) are isolated — one bad lookup doesn't stop the rest of the sync.
- **Onboarding:** a Stripe Payment Link (created no-code in the Stripe dashboard) is the onboarding path from the site — not a server-side Checkout flow. GitHub Pages is static-only and cannot safely hold a Stripe secret key, so there's no backend here by design. The site currently has no onboarding CTA button (removed per design feedback); add one back with the real Payment Link URL once it's created.
- **After a real signup:** Stripe notifies you; add the client manually via `add-client` with the real `stripe_subscription_id`. This is intentionally manual at current volume — automating client creation from Stripe webhooks needs a persistent server (Pages can't run one) and isn't worth building before there's a queue of signups to justify it.

## Public repo — no login on the site yet

`dashboard.html` is generated locally by `generate-dashboard` and is gitignored, not committed. Once real client data exists it contains names, contact info, revenue, and (once Stripe sync runs) live billing status — real access control (Cloudflare Access or Basic Auth) needs to be in front of it before it's ever published, since `projects/exodus/` is now served in full by GitHub Pages to a public repo.

## CLI

```
# Clients
python3 src/client_tracker.py add-client --client-id deli1 --business-name "Corner Deli" --monthly-rate 25 [--stripe-subscription-id sub_...]
python3 src/client_tracker.py log-snapshot --client-id deli1 --review-count 31 --average-rating 4.6
python3 src/client_tracker.py report deli1
python3 src/client_tracker.py mrr
python3 src/client_tracker.py generate-dashboard

# Stripe (needs STRIPE_API_KEY, restricted read-only key)
python3 src/stripe_sync.py sync --db data/exodus.db
python3 src/stripe_sync.py status sub_...
```
