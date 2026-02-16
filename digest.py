"""
Daily Digest v1.5 — Agentic curation layer added.

Pipeline: fetch → curate (score + filter + rank) → summarise (editorial) → format → send

Key changes from v1.0:
- LLM now acts as a relevance filter, not just a summariser
- User preference profile (config/user_profile.yaml) drives curation decisions
- One batched scoring call scores all items across all sources
- Editorial intro written by LLM highlights the day's most important theme
- Items grouped and formatted by relevance tier (high/medium/low)
- Run metadata logged to logs/YYYY-MM-DD.json after each digest
- Failed sources are logged and noted in the email footer, never crash the pipeline
"""

import os
import imaplib
import email
import re
import json
import datetime
import time
import yaml
import feedparser
import requests
import anthropic
from bs4 import BeautifulSoup
from pathlib import Path

# ---------------------------------------------------------------------------
# Config — all sensitive values come from environment variables / GH Secrets
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
RESEND_API_KEY    = os.environ["RESEND_API_KEY"]
GMAIL_ADDRESS     = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASS    = os.environ["GMAIL_APP_PASS"]
DIGEST_TO         = os.environ.get("DIGEST_TO", GMAIL_ADDRESS)

ANTHROPIC_MODEL = "claude-haiku-4-5"  # ~$0.018/day at current usage

# ---------------------------------------------------------------------------
# Load user profile
# ---------------------------------------------------------------------------

PROFILE_PATH = Path(__file__).parent / "config" / "user_profile.yaml"

def load_profile() -> dict:
    """Load user preference profile from YAML config."""
    with open(PROFILE_PATH) as f:
        return yaml.safe_load(f)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def today_str() -> str:
    return datetime.date.today().strftime("%A, %B %-d, %Y")

def today_iso() -> str:
    return datetime.date.today().isoformat()


def llm_call(system_prompt: str, user_content: str, max_tokens: int = 800) -> str:
    """Call Anthropic Claude directly and return the response text. Raises on failure."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[
            {"role": "user", "content": user_content},
        ],
    )
    return message.content[0].text.strip()


# ---------------------------------------------------------------------------
# Fetchers (unchanged from v1.0)
# ---------------------------------------------------------------------------

def fetch_rss(url: str, limit: int = 10) -> list[dict]:
    """Return a list of {title, link, summary} dicts from an RSS feed."""
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:limit]:
            items.append({
                "title":   entry.get("title", ""),
                "link":    entry.get("link", ""),
                "summary": entry.get("summary", entry.get("description", "")),
            })
        return items
    except Exception as e:
        return []


def fetch_latest_email(subject_keyword: str, sender_keyword: str) -> str:
    """
    Connect via IMAP, find the most recent email matching sender or subject,
    return plain-text body (truncated to 6000 chars).
    """
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
        mail.select("inbox")

        criteria = f'(FROM "{sender_keyword}")'
        _, data = mail.search(None, criteria)
        ids = data[0].split()

        if not ids:
            criteria = f'(SUBJECT "{subject_keyword}")'
            _, data = mail.search(None, criteria)
            ids = data[0].split()

        if not ids:
            return ""

        latest_id = ids[-1]
        _, msg_data = mail.fetch(latest_id, "(RFC822)")
        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    body = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    break
                elif ct == "text/html" and not body:
                    html = part.get_payload(decode=True).decode("utf-8", errors="ignore")
                    body = BeautifulSoup(html, "html.parser").get_text(separator="\n")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode("utf-8", errors="ignore")

        mail.logout()
        return body[:6000]
    except Exception as e:
        return f"[Email fetch failed: {e}]"


def fetch_luma_sf(limit: int = 10) -> list[dict]:
    """
    Scrape luma.com/sf — currently broken due to JS rendering.
    Returns empty list; failure is handled gracefully upstream.
    """
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; DailyDigestBot/1.0)"}
        resp = requests.get("https://luma.com/sf", headers=headers, timeout=15)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        script_tag = soup.find("script", {"id": "__NEXT_DATA__"})
        if not script_tag:
            return []

        data = json.loads(script_tag.string)
        events_raw = []
        try:
            props = data["props"]["pageProps"]
            for key in ("initialData", "events", "data"):
                if key in props:
                    node = props[key]
                    if isinstance(node, list):
                        events_raw = node
                        break
                    elif isinstance(node, dict):
                        for sub in node.values():
                            if isinstance(sub, list) and len(sub) > 0:
                                events_raw = sub
                                break
        except (KeyError, TypeError):
            pass

        events = []
        for ev in events_raw[:limit]:
            name = ev.get("name") or ev.get("title") or ""
            url  = ev.get("url") or ev.get("event_url") or ""
            if url and not url.startswith("http"):
                url = "https://lu.ma/" + url
            start = ev.get("start_at") or ev.get("start") or ""
            desc  = ev.get("description") or ev.get("summary") or ""
            if name:
                events.append({"name": name, "url": url, "date": start, "description": desc})

        return events
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Fetch stage — returns raw content dict + fetch metadata
# ---------------------------------------------------------------------------

SOURCE_META = {
    "simon":       {"label": "Simon Willison",        "always_include": True},
    "tldr":        {"label": "TLDR Newsletter",        "always_include": False},
    "techcrunch":  {"label": "TechCrunch Venture",     "always_include": False},
    "producthunt": {"label": "Product Hunt",           "always_include": False},
    "lenny":       {"label": "Lenny's Newsletter",     "always_include": True},
    "luma":        {"label": "Luma SF",                "always_include": False},
    "funcheap":    {"label": "Funcheap SF",            "always_include": False},
}


def fetch_all_raw() -> tuple[dict, dict]:
    """
    Fetch all raw content from every source.
    Returns:
        raw: dict of source_key -> raw text
        fetch_log: dict of source_key -> {chars, status, error}
    """
    raw = {}
    fetch_log = {}

    def _record(key, content, error=None):
        raw[key] = content
        fetch_log[key] = {
            "chars": len(content),
            "status": "ok" if not error else "failed",
            "error": error,
        }

    # Simon Willison
    try:
        items = fetch_rss("https://simonwillison.net/atom/everything/", limit=8)
        text = "\n".join(
            f"- {it['title']}: {it['link']}\n  {BeautifulSoup(it['summary'], 'html.parser').get_text()[:200]}"
            for it in items
        ) if items else ""
        _record("simon", text)
    except Exception as e:
        _record("simon", "", error=str(e))

    # TLDR newsletter
    try:
        text = fetch_latest_email(subject_keyword="TLDR", sender_keyword="dan@tldrnewsletter.com")
        error = text if text.startswith("[Email fetch failed") else None
        _record("tldr", "" if error else text, error=error)
    except Exception as e:
        _record("tldr", "", error=str(e))

    # TechCrunch
    try:
        tc_items = fetch_rss("https://techcrunch.com/tag/venture/feed/", limit=10)
        if not tc_items:
            tc_items = fetch_rss("https://techcrunch.com/feed/", limit=15)
        text = "\n".join(f"- {it['title']}: {it['link']}" for it in tc_items)
        _record("techcrunch", text)
    except Exception as e:
        _record("techcrunch", "", error=str(e))

    # Product Hunt
    try:
        ph_items = fetch_rss("https://www.producthunt.com/feed", limit=20)
        text = "\n".join(f"- {it['title']}: {it['link']}" for it in ph_items)
        _record("producthunt", text)
    except Exception as e:
        _record("producthunt", "", error=str(e))

    # Lenny's Newsletter
    try:
        text = fetch_latest_email(subject_keyword="Lenny", sender_keyword="lenny@lennysnewsletter.com")
        error = text if text.startswith("[Email fetch failed") else None
        _record("lenny", "" if error else text, error=error)
    except Exception as e:
        _record("lenny", "", error=str(e))

    # Luma SF
    try:
        luma_events = fetch_luma_sf(limit=10)
        text = "\n".join(
            f"- {ev['name']} | {ev['date'][:10] if ev['date'] else 'TBD'} | {ev['url']}"
            for ev in luma_events
        ) if luma_events else ""
        _record("luma", text, error=None if luma_events else "JS-rendered page returned no events")
    except Exception as e:
        _record("luma", "", error=str(e))

    # Funcheap
    try:
        cheap_items = fetch_rss("https://feeds.feedburner.com/funcheapsf_recent_added_events/", limit=20)
        text = "\n".join(f"- {it['title']}: {it['link']}" for it in cheap_items)
        _record("funcheap", text)
    except Exception as e:
        _record("funcheap", "", error=str(e))

    return raw, fetch_log


# ---------------------------------------------------------------------------
# Curate stage — LLM scores and filters all items against user profile
# ---------------------------------------------------------------------------

SYSTEM_CURATOR = (
    "You are an editorial AI assistant helping curate a personal morning digest. "
    "You will be given a user profile and a set of content items from various sources. "
    "Your job is to score each item for relevance to the user's interests and return structured JSON. "
    "Be strict: only high-quality, specific, relevant items should score above 0.7. "
    "General news filler, clickbait, or off-topic items should score below 0.5."
)


def curate(raw: dict, profile: dict) -> tuple[dict, dict]:
    """
    Send all fetched content to the LLM in one call.
    Returns:
        curated: dict of source_key -> list of scored items
                 Each item: {title, url, score, tier, rationale, category}
        curation_log: stats about what passed/failed the filter
    """
    threshold = profile["content_rules"]["min_relevance_threshold"]
    always_include = set(profile["content_rules"]["always_include_sources"])
    max_items = profile["content_rules"]["max_items_per_section"]

    # Build the prompt — condense raw content into item lists
    items_by_source = {}
    for key, text in raw.items():
        if not text:
            continue
        lines = [l.strip() for l in text.split("\n") if l.strip().startswith("-")]
        if lines:
            items_by_source[key] = lines[:15]  # cap at 15 items per source for token safety
        elif text and not text.startswith("["):
            # Email content — treat as single block item
            items_by_source[key] = [text[:800]]

    if not items_by_source:
        return {}, {"error": "No content to curate"}

    profile_summary = f"""
User: {profile['user']['name']}, {profile['user']['role']}
High priority interests: {', '.join(profile['interests']['high_priority'])}
Medium priority interests: {', '.join(profile['interests']['medium_priority'])}
Low priority interests: {', '.join(profile['interests']['low_priority'])}
Relevance threshold: {threshold} (exclude anything below this)
Max items per source: {max_items}
"""

    items_text = ""
    for source_key, lines in items_by_source.items():
        label = SOURCE_META[source_key]["label"]
        items_text += f"\n\n### Source: {source_key} ({label})\n"
        for i, line in enumerate(lines):
            items_text += f"{i+1}. {line}\n"

    user_prompt = f"""Given this user profile:
{profile_summary}

Score each item below for relevance (0.0–1.0) to this user's interests.
Return a JSON object with this exact structure:
{{
  "source_key": [
    {{
      "index": 1,
      "title": "item title or first 80 chars",
      "url": "url if present else empty string",
      "score": 0.85,
      "tier": "high",
      "category": "matching interest category",
      "rationale": "one sentence why"
    }}
  ]
}}

Tiers: "high" (score >= 0.8), "medium" (0.6–0.79), "low" (threshold–0.59).
Only include items that score >= {threshold}, EXCEPT for always-include sources ({', '.join(always_include)}) where include all items but still score them.
Return valid JSON only. No markdown, no explanation outside the JSON.

Items to score:
{items_text}"""

    try:
        response = llm_call(SYSTEM_CURATOR, user_prompt, max_tokens=2500)

        # Strip any markdown code fences if present
        response = re.sub(r"^```(?:json)?\s*", "", response.strip())
        response = re.sub(r"\s*```$", "", response.strip())

        scored = json.loads(response)

        # Build curated dict and log
        curated = {}
        curation_log = {}
        for source_key, items in scored.items():
            always = source_key in always_include
            passed = []
            for item in items:
                score = item.get("score", 0)
                if always or score >= threshold:
                    passed.append(item)
            # Sort by score desc, cap at max_items
            passed.sort(key=lambda x: x.get("score", 0), reverse=True)
            curated[source_key] = passed[:max_items]
            curation_log[source_key] = {
                "total_scored": len(items),
                "passed_filter": len(passed[:max_items]),
            }

        return curated, curation_log

    except Exception as e:
        # Fallback: return raw content as-is (degrade to v1 behavior)
        print(f"    [curate] Scoring failed ({e}), falling back to include-all mode")
        fallback = {}
        for key, text in raw.items():
            if text:
                fallback[key] = [{"title": text[:200], "url": "", "score": 1.0,
                                   "tier": "high", "category": "fallback", "rationale": "scoring unavailable"}]
        return fallback, {"error": str(e), "fallback": True}


# ---------------------------------------------------------------------------
# Summarise stage — editorial voice, grouped by relevance
# ---------------------------------------------------------------------------

SYSTEM_SUMMARISER = (
    "You are a concise, friendly assistant writing a personal morning digest. "
    "Write in plain English. No hype, no filler. Be direct and specific. "
    "Use bullet points. Do not exceed the requested length."
)


def build_editorial_intro(curated: dict, profile: dict) -> str:
    """Ask the LLM to write a 2-3 sentence editorial intro for today's digest."""
    # Gather top high-tier items across all sources
    top_items = []
    for source_key, items in curated.items():
        for item in items:
            if item.get("tier") == "high":
                top_items.append(f"- [{SOURCE_META.get(source_key, {}).get('label', source_key)}] {item['title']} (score: {item['score']:.2f})")

    if not top_items:
        return ""

    prompt = (
        f"Today is {today_str()}. Here are today's most relevant items for {profile['user']['name']}, "
        f"a {profile['user']['role']} interested in {', '.join(profile['interests']['high_priority'][:3])}:\n\n"
        + "\n".join(top_items[:6]) +
        "\n\nWrite a 2-3 sentence editorial intro for the morning digest. "
        "Highlight the most important theme or story of the day. "
        "Be direct and specific. No filler phrases like 'Good morning' or 'Here's your digest'."
    )

    try:
        return llm_call(SYSTEM_SUMMARISER, prompt, max_tokens=150)
    except Exception:
        return ""


def summarise_section(source_key: str, items: list[dict], raw_text: str) -> str:
    """
    Summarise a single source's curated items with editorial voice.
    High-tier items get fuller context; medium get one-liners; low get headline+link only.
    """
    if not items:
        return ""

    high   = [i for i in items if i.get("tier") == "high"]
    medium = [i for i in items if i.get("tier") == "medium"]
    low    = [i for i in items if i.get("tier") == "low"]

    # For email-based sources (tldr, lenny) pass the raw text for better summaries
    is_email_source = source_key in ("tldr", "lenny")
    content_for_llm = raw_text[:3000] if is_email_source and raw_text else (
        "\n".join(f"- {i['title']} {i['url']}" for i in items)
    )

    tiers_desc = ""
    if high:
        tiers_desc += f"High-relevance items (write 2-3 sentences each with context on why it matters): {[i['title'] for i in high]}\n"
    if medium:
        tiers_desc += f"Medium-relevance items (one sentence each): {[i['title'] for i in medium]}\n"
    if low:
        tiers_desc += f"Low-relevance items (headline + link only, no summary): {[i['title'] for i in low]}\n"

    prompt = (
        f"Summarise the following content from {SOURCE_META.get(source_key, {}).get('label', source_key)}.\n"
        f"Format by relevance tier:\n{tiers_desc}\n"
        f"Use bullet points. Include URLs where available.\n\n"
        f"Content:\n{content_for_llm}"
    )

    try:
        return llm_call(SYSTEM_SUMMARISER, prompt, max_tokens=400)
    except Exception as e:
        return f"[Summary unavailable: {e}]"


def summarise_all(curated: dict, raw: dict) -> tuple[dict, str]:
    """
    Summarise each curated source sequentially with 15s delay between calls.
    Also generates the editorial intro.
    Returns:
        summaries: dict of source_key -> summary text
        editorial_intro: string
    """
    profile = load_profile()

    print("    Generating editorial intro...")
    editorial_intro = build_editorial_intro(curated, profile)
    time.sleep(3)

    summaries = {}
    source_order = ["simon", "tldr", "techcrunch", "producthunt", "lenny", "luma", "funcheap"]

    for key in source_order:
        items = curated.get(key, [])
        if not items:
            summaries[key] = ""
            continue
        print(f"    Summarising {key} ({len(items)} items)...")
        summaries[key] = summarise_section(key, items, raw.get(key, ""))
        time.sleep(3)

    return summaries, editorial_intro


# ---------------------------------------------------------------------------
# Markdown → HTML converter
# ---------------------------------------------------------------------------

def md_to_html(text: str) -> str:
    """Convert basic markdown (bullets, bold, links) to HTML."""

    def _apply_inline(s: str) -> str:
        # 1. Convert [text](url) markdown links FIRST — before bare URL linkify
        s = re.sub(r"\[([^\]]+)\]\((https?://[^\)]+)\)", r'<a href="\2">\1</a>', s)
        # 2. Bold
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        # 3. Linkify bare URLs — the href="..." quotes protect already-converted links
        s = re.sub(r'(?<!["\'])(https?://[^\s<>"\']+)', r'<a href="\1">\1</a>', s)
        return s

    lines = text.split("\n")
    html_lines = []
    in_list = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            content = _apply_inline(stripped[2:])
            html_lines.append(f"  <li>{content}</li>")
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            if stripped:
                html_lines.append(f"<p>{_apply_inline(stripped)}</p>")
    if in_list:
        html_lines.append("</ul>")
    return "\n".join(html_lines)


# ---------------------------------------------------------------------------
# Email assembly
# ---------------------------------------------------------------------------

SECTION_STYLE = "margin-bottom: 32px;"
HEADER_STYLE  = (
    "font-size: 13px; font-weight: 700; letter-spacing: 0.08em; "
    "text-transform: uppercase; color: #6b7280; border-bottom: 1px solid #e5e7eb; "
    "padding-bottom: 6px; margin-bottom: 12px;"
)
BODY_STYLE = (
    "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; "
    "font-size: 15px; line-height: 1.6; color: #1f2937; "
    "max-width: 640px; margin: 0 auto; padding: 24px 16px;"
)
INTRO_STYLE = (
    "background: #f9fafb; border-left: 3px solid #6366f1; "
    "padding: 12px 16px; margin-bottom: 32px; border-radius: 0 6px 6px 0; "
    "font-style: italic; color: #374151;"
)

SECTION_CONFIG = [
    ("simon",       "AI News: Simon Willison",      "🔬"),
    ("tldr",        "AI News: TLDR",                "📰"),
    ("techcrunch",  "Tech & Funding: TechCrunch",   "💰"),
    ("producthunt", "Tech & Product: Product Hunt", "🚀"),
    ("lenny",       "Product: Lenny's Newsletter",  "💡"),
    ("luma",        "SF Meetups: Luma",             "🤝"),
    ("funcheap",    "Fun in SF: Funcheap",          "🎉"),
]


def build_html(summaries: dict, editorial_intro: str, failed_sources: list[str]) -> str:
    """Assemble the full HTML email."""

    # Editorial intro block
    intro_block = ""
    if editorial_intro:
        intro_block = f'<div style="{INTRO_STYLE}">{editorial_intro}</div>'

    # Section blocks
    section_blocks = ""
    for key, title, icon in SECTION_CONFIG:
        body = summaries.get(key, "").strip()
        if not body:
            continue
        body_html = md_to_html(body)
        section_blocks += f"""
        <div style="{SECTION_STYLE}">
            <div style="{HEADER_STYLE}">{icon} {title}</div>
            {body_html}
        </div>
        """

    # Failed sources note
    failed_note = ""
    if failed_sources:
        labels = [SOURCE_META.get(s, {}).get("label", s) for s in failed_sources]
        failed_note = (
            f'<p style="color:#9ca3af; font-size:12px; margin-top:16px;">'
            f'⚠️ Unavailable today: {", ".join(labels)}</p>'
        )

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body>
        <div style="{BODY_STYLE}">
            <h1 style="font-size:22px; font-weight:700; margin-bottom:4px;">
                Good morning ☀️
            </h1>
            <p style="color:#6b7280; margin-top:0; margin-bottom:24px;">
                Your daily digest for {today_str()}
            </p>
            {intro_block}
            {section_blocks}
            {failed_note}
            <p style="color:#9ca3af; font-size:12px; margin-top:40px; border-top:1px solid #e5e7eb; padding-top:16px;">
                Generated automatically · <a href="https://github.com/sumoseah/daily-digest" style="color:#9ca3af;">View source</a>
            </p>
        </div>
    </body>
    </html>
    """


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------

def send_email(subject: str, html: str) -> None:
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from":    "Daily Digest <onboarding@resend.dev>",
            "to":      [DIGEST_TO],
            "subject": subject,
            "html":    html,
        },
        timeout=15,
    )
    resp.raise_for_status()
    print(f"Email sent — status {resp.status_code}")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def write_log(fetch_log: dict, curation_log: dict, curated: dict) -> None:
    """Write per-run metadata to logs/YYYY-MM-DD.json."""
    logs_dir = Path(__file__).parent / "logs"
    logs_dir.mkdir(exist_ok=True)

    # Top 3 items by score across all sources
    all_items = []
    for source_key, items in curated.items():
        for item in items:
            all_items.append({
                "source": source_key,
                "title": item.get("title", ""),
                "score": item.get("score", 0),
                "tier":  item.get("tier", ""),
            })
    all_items.sort(key=lambda x: x["score"], reverse=True)

    log = {
        "date": today_iso(),
        "model": ANTHROPIC_MODEL,
        "fetch": fetch_log,
        "curation": curation_log,
        "top_3_items": all_items[:3],
        "failed_sources": [k for k, v in fetch_log.items() if v.get("status") == "failed"],
    }

    log_path = logs_dir / f"{today_iso()}.json"
    with open(log_path, "w") as f:
        json.dump(log, f, indent=2)
    print(f"  Log written to {log_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(dry_run: bool = False):
    print(f"Building digest v1.5 for {today_str()}..." + (" [DRY RUN]" if dry_run else ""))
    profile = load_profile()

    # 1. FETCH
    print("  Fetching all sources...")
    raw, fetch_log = fetch_all_raw()
    for k, v in fetch_log.items():
        status = "✓" if v["status"] == "ok" else "✗"
        print(f"    [{status}] {k}: {v['chars']} chars" + (f" — {v['error']}" if v.get("error") else ""))

    failed_sources = [k for k, v in fetch_log.items() if v["status"] == "failed"]

    # 2. CURATE — one batched LLM call to score all items
    print("  Curating content against user profile...")
    curated, curation_log = curate(raw, profile)
    for k, v in curation_log.items():
        if k not in ("error", "fallback"):
            print(f"    [{k}] {v.get('passed_filter', 0)}/{v.get('total_scored', 0)} items passed filter")

    if dry_run:
        print("\n  --- CURATION SCORES (dry run) ---")
        for source_key, items in curated.items():
            if items:
                print(f"  {source_key}:")
                for item in items:
                    print(f"    [{item.get('score', 0):.2f} {item.get('tier','?'):6s}] {item.get('title','')[:80]}")

    # 3. SUMMARISE — editorial voice, one call per source + intro
    print("\n  Summarising with editorial voice...")
    summaries, editorial_intro = summarise_all(curated, raw)

    if dry_run:
        print("\n  --- EDITORIAL INTRO (dry run) ---")
        print(f"  {editorial_intro}\n")
        print("  --- SECTION SUMMARIES (dry run) ---")
        for k, v in summaries.items():
            if v:
                print(f"\n  [{k}]\n  {v[:300]}")

    # 4. FORMAT
    html = build_html(summaries, editorial_intro, failed_sources)
    subject = f"Your Daily Digest — {today_str()}"

    if dry_run:
        # Save HTML to file for inspection instead of sending
        out_path = Path(__file__).parent / f"dry-run-{today_iso()}.html"
        out_path.write_text(html)
        print(f"\n  [DRY RUN] Email NOT sent. HTML saved to: {out_path}")
        print(f"  Open with: open {out_path}")
    else:
        print("  Sending email via Resend...")
        send_email(subject, html)

    # 5. LOG
    write_log(fetch_log, curation_log, curated)

    print("\nDone.")


def test_curation():
    """
    Verify the curation LLM call works using a tiny synthetic dataset.
    Sends ONE LLM call with 6 items (3 high-relevance, 3 low-relevance) and prints scores.
    Use this to confirm scoring is working without running the full pipeline.
    """
    print("Testing curation scoring with synthetic items...")
    profile = load_profile()

    # Synthetic items: mix of clearly relevant and clearly irrelevant
    fake_raw = {
        "simon": (
            "- Claude Code adds multi-agent orchestration support: https://simonwillison.net/2026/agent-arch/\n"
            "- Notes on building LLM-powered developer tools: https://simonwillison.net/2026/llm-tools/\n"
        ),
        "techcrunch": (
            "- AI agent startup raises $200M to automate enterprise workflows: https://techcrunch.com/ai-agent-series-c/\n"
            "- Celebrity chef opens new restaurant in Miami: https://techcrunch.com/miami-restaurant/\n"
        ),
        "funcheap": (
            "- Free jazz concert in Dolores Park this Sunday: https://sf.funcheap.com/jazz/\n"
            "- Celebrity gossip roundup — who wore it best?: https://sf.funcheap.com/celeb/\n"
        ),
    }

    curated, curation_log = curate(fake_raw, profile)

    if curation_log.get("fallback"):
        print(f"\n[FAIL] Curation fell back to include-all mode.")
        print(f"  Error: {curation_log.get('error')}")
        print("\n  This means the LLM scoring call failed (likely rate limit or model error).")
        print("  Try again in a few minutes.")
        return

    print("\n[PASS] Curation scoring succeeded!\n")
    print("Scores by source:")
    for source_key, items in curated.items():
        print(f"\n  {source_key}:")
        for item in items:
            bar = "█" * int(item.get("score", 0) * 10)
            print(f"    [{item['score']:.2f}] {bar:10s} [{item.get('tier','?'):6s}] {item.get('title','')[:70]}")
            if item.get("rationale"):
                print(f"           → {item['rationale']}")

    print(f"\nFilter stats:")
    for src, stats in curation_log.items():
        if isinstance(stats, dict) and "passed_filter" in stats:
            print(f"  {src}: {stats['passed_filter']}/{stats['total_scored']} items passed")

    expected_high = ["Claude Code", "LLM", "AI agent", "$200M"]
    expected_low = ["restaurant", "celebrity gossip", "jazz concert"]
    print("\nSanity check:")
    print("  High-relevance items should include: AI/LLM/agent stories")
    print("  Low-relevance items should score below 0.6 (or be filtered out)")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Run full pipeline but skip sending email. HTML saved to dry-run-DATE.html.")
    parser.add_argument("--test-curation", action="store_true",
                        help="Run only the curation scoring step with synthetic items to verify LLM scoring works.")
    args = parser.parse_args()

    if args.test_curation:
        test_curation()
    else:
        main(dry_run=args.dry_run)
