# Daily Digest

An automated personal morning email digest delivered at 7am PT via GitHub Actions. Instead of opening 7+ tabs every morning, I get a single consolidated HTML email summarising everything I care about — AI news, tech funding, new products, newsletters, and SF events.

---

## Motivation

I found myself spending 20–30 minutes each morning context-switching between Simon Willison's blog, my TLDR newsletter, TechCrunch, Product Hunt, and a handful of local SF event sites. Most days I'd miss something, or run out of time before getting to it all.

This project collapses all of that into a single email that's waiting in my inbox at 7am. It's not trying to replace deep reading — it's a triage layer so I know what's worth opening.

---

## What's New in v1.5

v1.5 adds an **agentic curation layer** between fetch and summarise. The LLM now decides what's worth including based on a personal preference profile, then writes a focused editorial intro before summarising.

| Feature | v1.0 | v1.5 |
|---------|------|------|
| Fetch sources | 7 sources via RSS, IMAP, scrape | Same |
| LLM role | Summariser only | Curator + Summariser |
| Relevance scoring | None | 0–1 score per item, against user profile |
| Content filtering | Everything included | Items below 0.6 threshold dropped |
| Item ranking | Fetch order | Sorted by relevance score |
| Editorial intro | None | LLM writes a themed intro highlighting the day's top thread |
| Section headers | By tier label | By relevance tier (high / medium / low) |
| Run logging | None | `logs/YYYY-MM-DD.json` with scores, fetch status, top items |
| LLM backend | OpenRouter (free models) | Direct Anthropic API (`claude-haiku-4-5`) |
| Rate limit delays | 15s between calls | 3s between calls (Haiku has much higher limits) |
| Cost | $0 (free tier) | ~$0.018/day (~$6.50/year, within $25 credit) |

---

## Architecture (v1.5)

```
GitHub Actions cron (7am PT)
        │
        ▼
  fetch_all_raw()
  ┌─────────────────────────────────┐
  │ RSS: Simon Willison             │
  │ RSS: TechCrunch Venture         │
  │ RSS: Product Hunt               │
  │ RSS: Funcheap SF                │
  │ IMAP: TLDR Newsletter (Gmail)   │
  │ IMAP: Lenny's Newsletter (Gmail)│
  │ Scrape: Luma SF events          │
  └─────────────────────────────────┘
        │
        ▼
  curate()  ← one batched LLM call
  • Scores every item 0–1 against user_profile.yaml
  • Drops items below 0.6 threshold
  • Groups remaining items: high (≥0.8) / medium (0.6–0.8) / low (<0.6)
        │
        ▼
  summarise_all()  ← 1 + N LLM calls
  • build_editorial_intro(): LLM writes themed intro for the day
  • Per-section summaries, ordered by relevance tier
        │
        ▼
  build_html()  → HTML email
        │
        ▼
  send_email()  → Resend API  →  Inbox
        │
        ▼
  write_log()  → logs/YYYY-MM-DD.json
```

**Pipeline:** `fetch → curate → summarise → format → send → log`

---

## Data Sources

| Section | Source | Fetch Method | Status |
|---------|--------|--------------|--------|
| AI News: Simon Willison | simonwillison.net/atom/everything | RSS | ✅ Working |
| AI News: TLDR | Gmail inbox | IMAP + App Password | ✅ Working |
| Tech & Funding: TechCrunch | techcrunch.com/tag/venture/feed | RSS | ✅ Working |
| Tech & Product: Product Hunt | producthunt.com/feed | RSS | ✅ Working |
| Product: Lenny's Newsletter | Gmail inbox | IMAP + App Password | ✅ Working |
| SF Meetups: Luma | luma.com/sf | HTML scrape (`__NEXT_DATA__`) | ❌ Broken — JS-rendered |
| Fun in SF: Funcheap | feeds.feedburner.com/funcheapsf | RSS | ✅ Working |

---

## Infrastructure

| Component | Choice | Why |
|-----------|--------|-----|
| Scheduler | GitHub Actions cron | Free, no server required, logs included |
| LLM | Anthropic `claude-haiku-4-5` (direct API) | Reliable, fast, higher rate limits than free tiers, ~$0.018/day |
| Email delivery | Resend | Clean API, 100 emails/day free tier |
| Email parsing | Python `imaplib` + Gmail App Password | No OAuth complexity, works reliably |
| User profile | `config/user_profile.yaml` | YAML file drives curation — edit to change what gets surfaced |

---

## Design Decisions & Trade-offs

### v1.0 → v1.5: Why add a curation layer?

v1.0 summarised everything it fetched. That meant Product Hunt items about kitchen gadgets, Lenny newsletter intros with no content, and low-signal news still made it into the email. The curation step moves the LLM from "condense this" to "first, decide if this is worth reading" — which is closer to how a human editor works.

The trade-off is one additional LLM call (the batched scoring call), but this is more than offset by shorter, more relevant summaries for each section.

### Why a YAML user profile?

The curation call sends the user's interest profile to the LLM as context. Having it in a separate YAML file (`config/user_profile.yaml`) makes it easy to tune without touching the pipeline code. The profile describes topics, signal strength preferences, and sources to weight more heavily.

### Why Anthropic direct API over OpenRouter?

Originally used OpenRouter's free model tier (Gemma, then Mistral). Problems:
- Free models have per-minute rate limits that break when running multiple test runs
- Quality was inconsistent — models would sometimes refuse to produce JSON, or truncate mid-response
- A small OpenRouter balance accrued even with "free" models (negative balance allowed)

Switched to direct Anthropic API (`claude-haiku-4-5`):
- Haiku has much higher rate limits — delays reduced from 15s → 3s between calls
- JSON output is reliable (important for the curation scoring call)
- Cost is ~$0.018/day against a direct $25 Anthropic credit (~3.8 years of daily runs)

### Why GitHub Actions over a dedicated server?

A cron job that runs for ~3 minutes once a day doesn't need a server. GitHub Actions gives us scheduling, secret management, execution logs, and retry visibility — all for free, within the 2,000 minutes/month free tier (we use ~90 min/month). The trade-off is a cold start on every run and no persistent state, but neither matters here.

### Why RSS + IMAP + scraping rather than a single approach?

Content lives in different places and there's no universal access method:
- **RSS** is ideal when available — structured, reliable, no auth required
- **IMAP** is necessary for newsletters like TLDR and Lenny's that don't offer free RSS feeds
- **Scraping** is the last resort for sites like Luma that have no feed or API

Each method is encapsulated in its own function (`fetch_rss`, `fetch_latest_email`, `fetch_luma_sf`), making it easy to add new sources by copying the relevant pattern.

### The Luma SF problem

`luma.com/sf` is a Next.js app that renders its event data client-side. The server-rendered HTML contains a `__NEXT_DATA__` JSON blob, but the event list is not included — it's fetched via a subsequent API call in the browser. This means our HTTP scraper always returns an empty event list.

**Potential solutions:**
- Use a headless browser (Playwright/Selenium) to render the page fully before parsing
- Find and call Luma's internal API directly (reverse-engineer from browser DevTools)
- Switch to an alternative SF event source with an RSS feed or open API

---

## Setup

### Prerequisites

| Secret | Where to get it |
|--------|----------------|
| `ANTHROPIC_API_KEY` | [console.anthropic.com/keys](https://console.anthropic.com/keys) — requires account with credits |
| `RESEND_API_KEY` | [resend.com](https://resend.com) — free tier, 100 emails/day |
| `GMAIL_ADDRESS` | Your full Gmail address |
| `GMAIL_APP_PASS` | [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) — requires 2FA enabled |
| `DIGEST_TO` | Email address to deliver the digest to |

> **Resend sandbox note:** On the free tier, Resend can only send to the email address you registered with. To send to a different address, verify a custom domain in the Resend dashboard.

### 1. Clone and push to a private GitHub repo

```bash
git clone https://github.com/sumoseah/daily-digest.git
# or start fresh:
git init
git add .
git commit -m "initial"
gh repo create daily-digest --private --push
```

> Keep the repo **private** — workflow logs will contain fetched email content.

### 2. Add GitHub Secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**

Add all 5 secrets from the table above.

```bash
# Or via CLI:
gh secret set ANTHROPIC_API_KEY
gh secret set RESEND_API_KEY
gh secret set GMAIL_ADDRESS
gh secret set GMAIL_APP_PASS
gh secret set DIGEST_TO
```

### 3. Customise your profile

Edit `config/user_profile.yaml` to describe your interests. This file drives the curation scoring — the LLM uses it to decide what's relevant to you.

### 4. Install dependencies locally (optional)

```bash
pip install -r requirements.txt
```

### 5. Test with a dry run

```bash
export ANTHROPIC_API_KEY=...
export RESEND_API_KEY=dummy
export GMAIL_ADDRESS=you@gmail.com
export GMAIL_APP_PASS=yourapppassword

python digest.py --dry-run
# Saves HTML to dry-run-YYYY-MM-DD.html instead of sending email
# open dry-run-YYYY-MM-DD.html
```

### 6. Timezone

The cron is set to `0 15 * * *` (15:00 UTC = 7:00 AM PST).
In summer (PDT, UTC-7), update `.github/workflows/digest.yml` to `0 14 * * *`.

---

## Costs

| Service | Plan | Usage | Cost |
|---------|------|-------|------|
| Anthropic API (`claude-haiku-4-5`) | Pay-as-you-go | ~8 LLM calls/day | ~$0.018/day |
| Resend | Free tier | 1 email/day | $0 |
| GitHub Actions | Free tier | ~90 min/month | $0 |
| **Total** | | | **~$6.50/year** |

$25 in Anthropic credits lasts approximately 3.8 years at current usage.

---

## Changelog

### v1.5 (current, `v1.5` branch — in development)
- Added agentic curation layer: LLM scores every item 0–1 against `config/user_profile.yaml`
- Items below 0.6 threshold are filtered out before summarisation
- Curated items ranked by relevance score and grouped into tiers (high / medium / low)
- Added editorial intro: LLM writes a themed intro based on the day's top items
- Switched LLM backend from OpenRouter (free models) to direct Anthropic API (`claude-haiku-4-5`)
- Reduced inter-call delay from 15s → 3s (Haiku's higher rate limits allow this)
- Increased curation call `max_tokens` to 2500 to prevent JSON truncation on full dataset
- Added `--dry-run` flag: saves HTML locally, does not send email
- Added `--test-curation` flag: runs curation with synthetic items to verify scoring in isolation
- Added `write_log()`: saves per-run metadata to `logs/YYYY-MM-DD.json`
- Fixed `md_to_html()` double-wrap bug: `[text](url)` links were being double-linkified

### v1.0 (stable, `main` branch)
- Fetch 7 sources (RSS, IMAP, scrape)
- Summarise each section with one LLM call
- Build HTML email and send via Resend
- Run daily at 7am PT via GitHub Actions cron

---

## Future Improvements

- **Fix Luma SF** — use Playwright to render the page, or reverse-engineer the Luma internal API
- **Digest archive** — save each HTML digest to a `digests/` folder for a browsable history
- **Smart deduplication** — avoid re-summarising stories that appeared in yesterday's digest (log already captures top items)
- **Send to Gmail** — verify a custom domain in Resend to remove the sandbox restriction
- **Web UI** — a simple Streamlit or Next.js app on top of the archive
- **More sources** — any RSS feed or email newsletter can be added in minutes following the existing patterns

---

## Troubleshooting

**"No module named 'anthropic'"**
→ Run `pip install -r requirements.txt` to install the Anthropic SDK.

**"AuthenticationError" from Anthropic**
→ Check that `ANTHROPIC_API_KEY` is set correctly and your account has credits.

**"Email fetch failed: [AUTH] Application-specific password required"**
→ You used your real Gmail password. Generate an App Password instead (see Setup above).

**"No email found in inbox"**
→ Check that TLDR/Lenny emails land in your Primary inbox, not Promotions. Star them or create a filter to move them to Primary.

**Luma section is empty**
→ Known issue — see the Luma SF problem section above.

**Lenny section has no useful content**
→ Lenny's Newsletter emails are often paywalled. The IMAP fetcher retrieves whatever text is in the email body — if the full article is behind a paywall, only the intro will be available to summarise.
