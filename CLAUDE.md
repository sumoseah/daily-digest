# Claude Code — Project Memory

This file gives Claude Code context about the daily-digest project so any new session can pick up where the last one left off.

---

## What this project is

An automated personal morning email digest delivered at 7am PT via GitHub Actions. Fetches content from 7 sources, summarises with an LLM via OpenRouter, sends a single HTML email via Resend.

**Repo:** https://github.com/sumoseah/daily-digest (public)
**Main script:** `digest.py`
**Owner:** sumoseah (linus.seah@kellogg.northwestern.edu)

---

## Credentials & secrets

All credentials are stored as GitHub Actions repository secrets — never in the code. The five secrets are:
- `OPENROUTER_API_KEY` — OpenRouter free tier
- `RESEND_API_KEY` — Resend free tier (re_Hk2... — do not hardcode)
- `GMAIL_ADDRESS` — seah.linus@gmail.com
- `GMAIL_APP_PASS` — 16-char Gmail App Password (do not hardcode)
- `DIGEST_TO` — linus.seah@kellogg.northwestern.edu (Resend sandbox restriction — see below)

---

## Current stack

| Component | Choice |
|-----------|--------|
| Scheduler | GitHub Actions cron `0 15 * * *` (7am PST) |
| LLM | OpenRouter — `mistralai/mistral-small-3.1-24b-instruct:free` |
| Email delivery | Resend (`onboarding@resend.dev`) |
| Language | Python 3.12 |

---

## Data sources & status

| Key | Section | Method | Status |
|-----|---------|--------|--------|
| `simon` | Simon Willison | RSS | ✅ Working |
| `tldr` | TLDR Newsletter | Gmail IMAP | ✅ Working |
| `techcrunch` | TechCrunch Venture | RSS | ✅ Working |
| `producthunt` | Product Hunt | RSS | ✅ Working |
| `lenny` | Lenny's Newsletter | Gmail IMAP | ✅ Working |
| `luma` | Luma SF events | HTML scrape | ❌ Broken — JS-rendered, returns 0 chars |
| `funcheap` | Funcheap SF | RSS | ✅ Working |

---

## Known issues

### 1. Luma SF returns 0 results
`luma.com/sf` is a Next.js app — event data is fetched client-side, not in `__NEXT_DATA__`. The `fetch_luma_sf()` function always returns an empty list.

**Potential fixes (not yet attempted):**
- Reverse-engineer Luma's internal API from browser DevTools network tab
- Use a headless browser (Playwright) to render the page before scraping
- Find an alternative SF tech events source with RSS

### 2. Resend sandbox restriction
Resend free tier can only send to the registered account email (`linus.seah@kellogg.northwestern.edu`). To send to `seah.linus@gmail.com`, a custom domain needs to be verified in the Resend dashboard.

### 3. OpenRouter free tier rate limits
The free Mistral model has per-minute rate limits. Running multiple test workflow runs in quick succession causes 429 errors on some sections. In normal daily use (one run/day) this is not a problem. Current mitigation: 15s delay between each of the 7 LLM calls.

---

## Key functions in digest.py

| Function | What it does |
|----------|-------------|
| `fetch_rss(url, limit)` | Parses RSS/Atom feed, returns list of {title, link, summary} |
| `fetch_latest_email(subject_kw, sender_kw)` | IMAP login to Gmail, finds most recent matching email, returns plain text body |
| `fetch_luma_sf(limit)` | Attempts to scrape luma.com/sf — currently broken |
| `fetch_all_raw()` | Calls all fetchers, returns dict of section_key → raw text |
| `llm_summarise(system_prompt, user_content, max_tokens)` | Single OpenRouter API call, returns summary string |
| `summarise_all(raw)` | Loops over 7 tasks, calls llm_summarise for each with 15s delay |
| `md_to_html(text)` | Converts basic markdown (bullets, bold) to HTML |
| `build_html(sections)` | Assembles full HTML email from section dict |
| `send_email(subject, html)` | POSTs to Resend API |
| `main()` | Orchestrates everything end-to-end |

---

## Next steps (priority order)

1. **Verify the 7am scheduled run works** — check the email after the next automatic run. Should be fine now that we're not hammering the rate limits with test runs.
2. **Fix Luma SF** — investigate Luma's internal API or switch to an alternative source.
3. **Fix Resend sandbox** — verify a custom domain to send to seah.linus@gmail.com.

---

## Git history (notable commits)

| Commit | Description |
|--------|-------------|
| `958b9e6` | Add docs/ARCHITECTURE.md |
| `0cfc98e` | Rewrite README with current setup |
| `dd83518` | Switch to Mistral (current model) |
| `5d77f1b` | Increase delay to 15s |
| `2958ba0` | Fix Gemma system role bug |
| `8b1b876` | Initial commit |
