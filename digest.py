"""
Daily Digest — fetches content from all sources, summarises with OpenRouter,
sends a single HTML email via Resend.
"""

import os
import imaplib
import email
import re
import json
import datetime
import feedparser
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Config — all sensitive values come from environment variables / GH Secrets
# ---------------------------------------------------------------------------

OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
RESEND_API_KEY     = os.environ["RESEND_API_KEY"]
GMAIL_ADDRESS      = os.environ["GMAIL_ADDRESS"]      # your full gmail address
GMAIL_APP_PASS     = os.environ["GMAIL_APP_PASS"]     # 16-char app password
DIGEST_TO          = os.environ.get("DIGEST_TO", GMAIL_ADDRESS)  # who to send to

# Free model on OpenRouter — strong and fast
OPENROUTER_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
OPENROUTER_URL   = "https://openrouter.ai/api/v1/chat/completions"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def llm_summarise(system_prompt: str, user_content: str, max_tokens: int = 600) -> str:
    """Call OpenRouter and return the summary text."""
    try:
        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {OPENROUTER_API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/daily-digest",
                "X-Title": "Daily Digest",
            },
            json={
                "model": OPENROUTER_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_content},
                ],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"[Summary unavailable: {e}]"


def fetch_rss(url: str, limit: int = 10) -> list[dict]:
    """Return a list of {title, link, summary} dicts from an RSS feed."""
    feed = feedparser.parse(url)
    items = []
    for entry in feed.entries[:limit]:
        items.append({
            "title":   entry.get("title", ""),
            "link":    entry.get("link", ""),
            "summary": entry.get("summary", entry.get("description", "")),
        })
    return items


def fetch_latest_email(subject_keyword: str, sender_keyword: str) -> str:
    """
    Connect via IMAP, find the most recent email matching sender or subject,
    return plain-text body (truncated to 6000 chars to stay within token limits).
    """
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASS)
        mail.select("inbox")

        # Search by sender first, fall back to subject
        criteria = f'(FROM "{sender_keyword}")'
        _, data = mail.search(None, criteria)
        ids = data[0].split()

        if not ids:
            criteria = f'(SUBJECT "{subject_keyword}")'
            _, data = mail.search(None, criteria)
            ids = data[0].split()

        if not ids:
            return ""

        # Take the most recent match
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
    Scrape luma.com/sf — events are embedded as JSON in __NEXT_DATA__.
    Returns list of {name, url, date, description}.
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

        # Navigate to events — path may shift with Next.js updates
        events_raw = []
        try:
            props = data["props"]["pageProps"]
            # Try common keys
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
            # Luma event objects vary; grab what we can
            name = ev.get("name") or ev.get("title") or ""
            url  = ev.get("url") or ev.get("event_url") or ""
            if url and not url.startswith("http"):
                url = "https://lu.ma/" + url
            start = ev.get("start_at") or ev.get("start") or ""
            desc  = ev.get("description") or ev.get("summary") or ""
            if name:
                events.append({"name": name, "url": url, "date": start, "description": desc})

        return events

    except Exception as e:
        return [{"name": f"Luma fetch failed: {e}", "url": "", "date": "", "description": ""}]


def today_str() -> str:
    return datetime.date.today().strftime("%A, %B %-d, %Y")


# ---------------------------------------------------------------------------
# Section builders — each returns (html_block, plain_text_for_llm)
# ---------------------------------------------------------------------------

SYSTEM_SUMMARISER = (
    "You are a concise, friendly assistant writing a personal morning digest. "
    "Write in plain English. No hype, no filler. Be direct and specific. "
    "Use bullet points. Do not exceed the requested length."
)


def section_simon_willison() -> str:
    items = fetch_rss("https://simonwillison.net/atom/everything/", limit=8)
    if not items:
        return "<p>No posts found.</p>"
    raw = "\n".join(f"- {it['title']}: {it['link']}\n  {BeautifulSoup(it['summary'], 'html.parser').get_text()[:300]}" for it in items)
    summary = llm_summarise(
        SYSTEM_SUMMARISER,
        f"Summarise the most interesting AI/tech posts from Simon Willison's blog today. Pick the 3-4 most notable. Include the URL for each.\n\n{raw}",
        max_tokens=400,
    )
    return md_to_html(summary)


def section_tldr() -> str:
    body = fetch_latest_email(subject_keyword="TLDR", sender_keyword="dan@tldrnewsletter.com")
    if not body:
        return "<p>TLDR email not found in inbox.</p>"
    summary = llm_summarise(
        SYSTEM_SUMMARISER,
        f"Extract and summarise the 4-5 most important AI/tech stories from this TLDR newsletter. One sentence per story.\n\n{body}",
        max_tokens=400,
    )
    return md_to_html(summary)


def section_techcrunch() -> str:
    items = fetch_rss("https://techcrunch.com/tag/venture/feed/", limit=10)
    if not items:
        # fallback to main feed
        items = fetch_rss("https://techcrunch.com/feed/", limit=15)
    raw = "\n".join(f"- {it['title']}: {it['link']}" for it in items)
    summary = llm_summarise(
        SYSTEM_SUMMARISER,
        f"Pick the 4-5 most notable startup funding or venture news items from today's TechCrunch. Include dollar amounts and company names where mentioned. Include the URL.\n\n{raw}",
        max_tokens=350,
    )
    return md_to_html(summary)


def section_product_hunt() -> str:
    items = fetch_rss("https://www.producthunt.com/feed", limit=20)
    if not items:
        return "<p>Product Hunt feed unavailable.</p>"
    raw = "\n".join(f"- {it['title']}: {it['link']}" for it in items[:20])
    summary = llm_summarise(
        SYSTEM_SUMMARISER,
        f"Pick the top 5 most interesting products from this Product Hunt feed. One line each: name, what it does, URL.\n\n{raw}",
        max_tokens=300,
    )
    return md_to_html(summary)


def section_lenny() -> str:
    body = fetch_latest_email(subject_keyword="Lenny", sender_keyword="lenny@lennysnewsletter.com")
    if not body:
        return "<p>Lenny's Newsletter email not found in inbox.</p>"
    summary = llm_summarise(
        SYSTEM_SUMMARISER,
        f"Summarise the key ideas and takeaways from this Lenny's Newsletter edition in 4-5 bullet points.\n\n{body}",
        max_tokens=400,
    )
    return md_to_html(summary)


def section_luma() -> str:
    events = fetch_luma_sf(limit=10)
    if not events:
        return "<p>No Luma events found.</p>"
    raw = "\n".join(
        f"- {ev['name']} | {ev['date'][:10] if ev['date'] else 'TBD'} | {ev['url']}"
        for ev in events
    )
    summary = llm_summarise(
        SYSTEM_SUMMARISER,
        f"Today is {today_str()}. From this list of SF events on Luma, pick the 4-5 most relevant AI or tech meetups happening in the next 7 days. Include date and URL.\n\n{raw}",
        max_tokens=350,
    )
    return md_to_html(summary)


def section_funcheap() -> str:
    items = fetch_rss("https://feeds.feedburner.com/funcheapsf_recent_added_events/", limit=20)
    if not items:
        return "<p>Funcheap feed unavailable.</p>"
    raw = "\n".join(f"- {it['title']}: {it['link']}" for it in items)
    summary = llm_summarise(
        SYSTEM_SUMMARISER,
        f"Today is {today_str()}. From these SF cheap/free events, pick the 3 most fun and interesting ones happening in the next 7 days. Include date and URL.\n\n{raw}",
        max_tokens=300,
    )
    return md_to_html(summary)


# ---------------------------------------------------------------------------
# Markdown → minimal HTML converter (keeps it dependency-light)
# ---------------------------------------------------------------------------

def md_to_html(text: str) -> str:
    """Convert basic markdown (bullets, bold) to HTML."""
    lines = text.split("\n")
    html_lines = []
    in_list = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                html_lines.append("<ul>")
                in_list = True
            content = stripped[2:]
            content = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", content)
            # linkify bare URLs
            content = re.sub(
                r"(?<![\"'])(https?://[^\s<>\"']+)",
                r'<a href="\1">\1</a>',
                content,
            )
            html_lines.append(f"  <li>{content}</li>")
        else:
            if in_list:
                html_lines.append("</ul>")
                in_list = False
            if stripped:
                stripped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", stripped)
                stripped = re.sub(
                    r"(?<![\"'])(https?://[^\s<>\"']+)",
                    r'<a href="\1">\1</a>',
                    stripped,
                )
                html_lines.append(f"<p>{stripped}</p>")
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


def build_html(sections: dict[str, str]) -> str:
    section_blocks = ""
    icons = {
        "AI News: Simon Willison":    "🔬",
        "AI News: TLDR":              "📰",
        "Tech & Funding: TechCrunch": "💰",
        "Tech & Product: Product Hunt":"🚀",
        "Product: Lenny's Newsletter":"💡",
        "SF Meetups: Luma":           "🤝",
        "Fun in SF: Funcheap":        "🎉",
    }
    for title, body_html in sections.items():
        icon = icons.get(title, "•")
        section_blocks += f"""
        <div style="{SECTION_STYLE}">
            <div style="{HEADER_STYLE}">{icon} {title}</div>
            {body_html}
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body>
        <div style="{BODY_STYLE}">
            <h1 style="font-size:22px; font-weight:700; margin-bottom:4px;">
                Good morning ☀️
            </h1>
            <p style="color:#6b7280; margin-top:0; margin-bottom:32px;">
                Your daily digest for {today_str()}
            </p>
            {section_blocks}
            <p style="color:#9ca3af; font-size:12px; margin-top:40px; border-top:1px solid #e5e7eb; padding-top:16px;">
                Generated automatically · <a href="https://github.com" style="color:#9ca3af;">View source</a>
            </p>
        </div>
    </body>
    </html>
    """


def send_email(subject: str, html: str) -> None:
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from":    "Daily Digest <digest@yourdomain.com>",  # update to your verified Resend domain
            "to":      [DIGEST_TO],
            "subject": subject,
            "html":    html,
        },
        timeout=15,
    )
    resp.raise_for_status()
    print(f"Email sent — status {resp.status_code}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print(f"Building digest for {today_str()}...")

    print("  Fetching Simon Willison...")
    simon = section_simon_willison()

    print("  Fetching TLDR from Gmail...")
    tldr = section_tldr()

    print("  Fetching TechCrunch...")
    tc = section_techcrunch()

    print("  Fetching Product Hunt...")
    ph = section_product_hunt()

    print("  Fetching Lenny's Newsletter from Gmail...")
    lenny = section_lenny()

    print("  Fetching Luma SF events...")
    luma = section_luma()

    print("  Fetching Funcheap SF events...")
    cheap = section_funcheap()

    sections = {
        "AI News: Simon Willison":    simon,
        "AI News: TLDR":              tldr,
        "Tech & Funding: TechCrunch": tc,
        "Tech & Product: Product Hunt": ph,
        "Product: Lenny's Newsletter": lenny,
        "SF Meetups: Luma":           luma,
        "Fun in SF: Funcheap":        cheap,
    }

    html = build_html(sections)
    subject = f"Your Daily Digest — {today_str()}"

    print("  Sending email via Resend...")
    send_email(subject, html)
    print("Done.")


if __name__ == "__main__":
    main()
