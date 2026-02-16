# Architecture (v1.5)

## System Overview

Daily Digest is a single-file Python script (`digest.py`) orchestrated by GitHub Actions. It has no persistent server, no database, and no web interface. The entire system runs once per day as an ephemeral process, produces one HTML email, and exits.

**Component responsibilities:**

| Component | Responsibility |
|-----------|---------------|
| `digest.py` | All application logic — fetching, curating, summarising, assembling, sending |
| `config/user_profile.yaml` | User interest profile that drives LLM curation decisions |
| `logs/YYYY-MM-DD.json` | Per-run metadata: fetch stats, curation scores, failures |
| GitHub Actions | Scheduling, secret injection, execution environment, logging |
| OpenRouter API | LLM inference (model-agnostic gateway) |
| Resend API | Transactional email delivery |
| Gmail (IMAP) | Source for email-based newsletters (TLDR, Lenny's) |

---

## Data Flow

```
1. FETCH       fetch_all_raw()
               ├── fetch_rss()          → feedparser parses Atom/RSS feeds
               ├── fetch_latest_email() → imaplib connects to Gmail via IMAP
               └── fetch_luma_sf()      → requests + BeautifulSoup scrapes HTML
               Returns: raw dict + fetch_log (chars fetched, status, errors)

2. CURATE      curate(raw, profile)                           ← NEW in v1.5
               └── ONE batched LLM call → scores all items 0–1 against
                   config/user_profile.yaml interests
               - Items below threshold (0.6) are filtered out
               - always_include sources (simon, lenny) bypass threshold
               - Items ranked by score, capped at max_items_per_source (5)
               - On failure: degrades to include-all (v1.0 behavior)

3. SUMMARISE   summarise_all(curated, raw)
               ├── build_editorial_intro() → 1 LLM call, 2-3 sentence intro
               └── summarise_section() × 7 → tiered summaries per source
                   - high tier (≥0.8): 2-3 sentences with context
                   - medium tier (0.6–0.79): one sentence
                   - low tier (<0.6 but passed): headline + link only
               15s delay between each call to respect rate limits

4. FORMAT      build_html(summaries, editorial_intro, failed_sources)
               ├── editorial intro block (styled callout)
               ├── md_to_html() per section
               └── failed sources note in footer

5. DELIVER     send_email()
               └── Resend API POST → single HTML email to DIGEST_TO

6. LOG         write_log()                                    ← NEW in v1.5
               └── logs/YYYY-MM-DD.json → fetch stats, curation scores,
                   top 3 items, failed sources
```

Each stage passes data forward as plain Python dicts and strings. There are no queues, no async I/O, and no inter-process communication.

---

## Error Handling

The system is designed to **degrade gracefully** — a single failing source never aborts the entire digest.

- **Fetch failures:** Every fetcher (`fetch_rss`, `fetch_latest_email`, `fetch_luma_sf`) wraps its logic in a `try/except` block. On failure it returns an empty string or a descriptive error message (e.g. `"[Email fetch failed: ...]"`). The rest of the pipeline proceeds with whatever content is available.

- **LLM failures:** `llm_summarise()` catches all exceptions and returns a placeholder string (`"[Summary unavailable: ...]"`). The email is still sent — it just shows the placeholder for that section instead of a summary.

- **Email delivery failure:** `send_email()` calls `raise_for_status()` on the Resend response. If delivery fails, the GitHub Actions step exits with a non-zero code, the run is marked failed, and GitHub sends a notification email to the repo owner.

The practical result: most partial failures produce a slightly incomplete digest rather than no digest at all.

---

## Security

All credentials are stored as **GitHub Actions repository secrets** and injected into the workflow as environment variables at runtime. They are:

- Never written to disk or logged (GitHub automatically redacts secret values from logs)
- Never present in the source code or version history
- Scoped to this repo only

The five secrets in use are: `OPENROUTER_API_KEY`, `RESEND_API_KEY`, `GMAIL_ADDRESS`, `GMAIL_APP_PASS`, and `DIGEST_TO`.

Gmail access uses an **App Password** (a 16-character token scoped to a single app) rather than the account password or OAuth. This limits the blast radius if the credential is ever compromised — it can be revoked instantly from Google Account settings without affecting the main account.

The repo is kept **private** to prevent workflow logs (which may contain fetched newsletter content) from being publicly visible.

---

## Scalability

The current design is intentionally minimal. Here is what would need to change at each growth axis:

**Adding more sources**
Each source is one function call in `fetch_all_raw()` and one entry in `summarise_all()`. Adding a new RSS feed is ~3 lines; adding a new email newsletter is ~2 lines. No structural changes needed.

**Switching LLM providers**
The model is a single constant (`OPENROUTER_MODEL`). OpenRouter supports 100+ models behind the same API contract, so switching is a one-line change. Moving off OpenRouter entirely would require updating `llm_summarise()` to use a different client, but the rest of the code is unaffected.

**Serving multiple users**
The current design is hardcoded to one recipient. To support multiple users you would need to:
- Store per-user preferences and source lists (a database or config file)
- Parameterise `fetch_all_raw()` and `summarise_all()` per user
- Loop over users at send time, or run parallel jobs

A lightweight approach would be a Supabase table of user configs with one GitHub Actions matrix job per user. A heavier approach would be a proper web app with a job queue.

**Handling higher fetch volume**
All fetches are currently sequential. For significantly more sources, `fetch_all_raw()` could be parallelised using `concurrent.futures.ThreadPoolExecutor` without changes to the rest of the pipeline.
