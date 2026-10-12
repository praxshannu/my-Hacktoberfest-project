"""Rule-based message categorizer for the college-group organizer.

Tags (one primary tag per message, first match in rules.json priority wins):

    Schedule    room allotments, class timings
    Deadline    due dates, registration cutoffs
    Action      must register / fill / submit / bring / do X
    Opportunity contests, competitions, programs, challenges
    Attendance  absentee lists
    Resource    timetables, sheets, PDFs, docs, links with context
    Noise       deleted, bare links, short acknowledgments

Also extracts:
  - deadline_text / deadline_iso : a date or deadline mentioned in the text
  - roll_count / display_text     : roll-number lists are counted and redacted;
    display_text is the only text the UI may show.

Optional LLM hook: refine_with_llm() can re-tag via a local Ollama server.
It is DISABLED by default and never required -- the rules above are the
whole MVP. Enable with OLLAMA_REFINE=1 (see build.py).

Standard library only.
"""

import json
import os
import re
import urllib.request
from datetime import datetime, timedelta

RULES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rules.json")

URL_RE = re.compile(r"https?://\S+|www\.\S+")

# College roll ids like 24L31A4375 / 25l35a4312. Always redacted, never shown.
ROLL_RE = re.compile(r"\b\d{2}[A-Za-z]\d{2}[A-Za-z0-9]{3,5}\b")

# Comma-separated short-token runs: "5,17,18,26,D1,G1" / "70,75,78,80".
LIST_RUN_RE = re.compile(
    r"(?:\b[A-Za-z0-9]{1,4}\b[\s]*)?,(?:[\s]*\b[A-Za-z0-9]{1,4}\b[\s]*,)+[\s]*\b[A-Za-z0-9]{1,4}\b"
    r"|\b[A-Za-z0-9]{1,4}\b(?:[\s]*,[\s]*\b[A-Za-z0-9]{1,4}\b){2,}"
)

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# Mechanical date shapes (kept in code; tag vocabulary lives in rules.json).
DATE_NUM_RE = re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})\b")
DATE_TEXT_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?"
    r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
    r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?)\b(?:\s*,?\s*(20\d{2}))?",
    re.IGNORECASE,
)
DATE_REL_RE = re.compile(
    r"\b(today|tonight|tomorrow|repu|repe|eroju)\b", re.IGNORECASE)
# repu / repe = "tomorrow" and eroju = "today" in Telugu (both groups mix Telugu + English).
REL_DAY_OFFSET = {"today": 0, "tonight": 0, "eroju": 0,
                  "tomorrow": 1, "repu": 1, "repe": 1}
DATE_WITHIN_RE = re.compile(r"\bwithin\s+(\d{1,3})\s+hours?\b", re.IGNORECASE)
TIME_RE = re.compile(r"\b(0?[1-9]|1[0-2])(?::([0-5]\d))?\s*([AP])\.?\s*M\.?\b", re.IGNORECASE)
WEEKDAY_RE = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)


def load_rules(path=RULES_PATH):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _text_without_urls(text):
    return URL_RE.sub(" ", text)


def _is_bare_link(text, rules):
    """A URL (or media placeholder) with almost no surrounding context."""
    stripped = _text_without_urls(text)
    for marker in ("<image omitted>", "<document omitted>", "<video omitted>",
                   "<audio omitted>", "<sticker omitted>", "<gif omitted>"):
        stripped = stripped.replace(marker, " ").replace(marker.upper(), " ")
    stripped = re.sub(r"\s+", " ", stripped).strip(" .,:;-|")
    limit = rules.get("noise", {}).get("bare_link_max_context_chars", 30)
    has_link = bool(URL_RE.search(text)) or "<" in text and "omitted>" in text.lower()
    return has_link and len(stripped) <= limit


def _is_short_ack(text, rules):
    words = re.sub(r"\s+", " ", text).strip().lower()
    if len(words.split()) > rules.get("noise", {}).get("max_ack_words", 5):
        return False
    acks = rules.get("noise", {}).get("ack_phrases", [])
    return any(phrase in words for phrase in acks) or len(words) <= 3


def _matches(rule, text):
    lowered = text.lower()
    for keyword in rule.get("keywords", []):
        if keyword.lower() in lowered:
            return True
    for pattern in rule.get("patterns", []):
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def _has_due_word(text, rules):
    lowered = text.lower()
    if any(word in lowered for word in rules.get("due_words", [])):
        return True
    # "by <date>" / "before <date>" style cutoffs. The lookahead insists on
    # an actual date after by/before so "before 10 minutes" does not match.
    if re.search(
        r"\b(by|before)\s+(?=\d{1,2}[-/.]|\d{1,2}\s*(?:st|nd|rd|th)\b"
        r"|today|tonight|tomorrow|repu|repe"
        r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
        r"|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
        text, re.IGNORECASE,
    ):
        return True
    if re.search(r"\bsubmit\b.{0,30}\d{1,2}[-/]", text, re.IGNORECASE):
        return True
    return False


def extract_date(text, reference_iso=None):
    """Return (deadline_text, deadline_iso) for the first date found.

    reference_iso anchors relative dates ("today", "tomorrow", weekdays);
    it should be the message timestamp. deadline_iso is YYYY-MM-DD.
    """
    try:
        ref = datetime.fromisoformat(reference_iso) if reference_iso else datetime.now()
    except ValueError:
        ref = datetime.now()

    match = DATE_NUM_RE.search(text)
    if match:
        day, month, year = int(match.group(1)), int(match.group(2)), match.group(3)
        year = int(year) if len(year) == 4 else 2000 + int(year)
        # Exports here are D-M-Y; if month is impossible, try M-D-Y.
        if not 1 <= month <= 12 and 1 <= day <= 12:
            day, month = month, day
        try:
            resolved = datetime(year, month, day).date().isoformat()
        except ValueError:
            resolved = None
        if resolved:
            return match.group(0), resolved

    match = DATE_TEXT_RE.search(text)
    if match:
        day = int(match.group(1))
        month = MONTHS[match.group(2).lower()]
        year = int(match.group(3)) if match.group(3) else ref.year
        try:
            resolved = datetime(year, month, day).date().isoformat()
        except ValueError:
            resolved = None
        if resolved:
            return match.group(0), resolved

    match = DATE_REL_RE.search(text)
    if match:
        word = match.group(1).lower()
        delta = REL_DAY_OFFSET.get(word, 0)
        stamp = (ref + timedelta(days=delta)).date().isoformat()
        time = TIME_RE.search(text)
        label = match.group(0) + (" " + time.group(0) if time else "")
        return label, stamp

    match = DATE_WITHIN_RE.search(text)
    if match:
        stamp = (ref + timedelta(hours=int(match.group(1)))).date().isoformat()
        return match.group(0), stamp

    match = WEEKDAY_RE.search(text)
    if match:
        target = WEEKDAYS[match.group(1).lower()]
        delta = (target - ref.weekday()) % 7 or 7  # next occurrence
        stamp = (ref + timedelta(days=delta)).date().isoformat()
        return match.group(0), stamp

    time = TIME_RE.search(text)
    if time:
        return time.group(0), ref.date().isoformat()

    return None, None


def redact_rolls(text):
    """Count roll-number/list content and return (display_text, roll_count).

    Roll ids and comma-list runs are replaced with a placeholder so the UI
    can show only a count, never the list.
    """
    rolls = ROLL_RE.findall(text)
    count = len(rolls)
    redacted = ROLL_RE.sub("[roll no.]", text)

    # Count list runs before replacing.
    for match in LIST_RUN_RE.finditer(redacted):
        tokens = [t for t in re.split(r",", match.group(0)) if t.strip()]
        if len(tokens) >= 3:
            count += len(tokens)
    redacted = LIST_RUN_RE.sub(
        lambda m: "[list of %d hidden]" % len([t for t in re.split(r",", m.group(0)) if t.strip()])
        if len([t for t in re.split(r",", m.group(0)) if t.strip()]) >= 3
        else m.group(0),
        redacted,
    )
    # Collapse repeats like "[roll no.], [roll no.]" leftovers.
    redacted = re.sub(r"(\[roll no\.\][,\s]*){2,}", "[roll list hidden] ", redacted)
    redacted = re.sub(r"[ \t]+", " ", redacted)
    redacted = re.sub(r"\n{3,}", "\n\n", redacted).strip()
    return redacted.strip(" ,"), count


def categorize(message, rules=None):
    """Assign a primary tag; returns (tag, deadline_text, deadline_iso)."""
    rules = rules or load_rules()
    text = message.get("text", "") or ""

    if message.get("is_deleted") or "system notification" in text.lower():
        return "Noise", None, None
    if _is_bare_link(text, rules):
        return "Noise", None, None
    if _is_short_ack(text, rules):
        return "Noise", None, None

    tags = rules.get("tags", {})

    attendance = tags.get("Attendance", {})
    # Attendance needs an attendance word AND evidence: a roll/list run or a
    # take/post announcement. A bare mention ("absentees also fill the form")
    # or a non-attendance list ("Team 09" + roll ids) falls through instead.
    att_words = attendance.get("keywords", ("absent", "attendance"))
    lowered_all = text.lower()
    if any(word.lower() in lowered_all for word in att_words):
        has_list = bool(ROLL_RE.search(text)) or bool(LIST_RUN_RE.search(text))
        takes = bool(re.search(
            r"\btak\w*|\bpost\w*|\bmandatory\b|\btees\w*", lowered_all))
        if has_list or takes:
            return "Attendance", *extract_date(text, message.get("timestamp"))

    if _has_due_word(text, rules):
        found, iso = extract_date(text, message.get("timestamp"))
        if found:
            return "Deadline", found, iso
        if any(w in text.lower() for w in ("deadline", "last date", "cutoff", "cut-off")):
            return "Deadline", None, None

    for tag in ("Action", "Schedule", "Opportunity", "Resource"):
        if _matches(tags.get(tag, {}), text):
            return tag, *extract_date(text, message.get("timestamp"))

    # Fallback: links/files with real context are Resources, else Noise.
    if URL_RE.search(text) or message.get("is_media"):
        return "Resource", *extract_date(text, message.get("timestamp"))
    return "Noise", None, None


def tag_message(message, rules=None):
    """Full enrichment: tag + deadline + roll redaction, on a parsed message."""
    rules = rules or load_rules()
    tag, deadline_text, deadline_iso = categorize(message, rules)
    display_text, roll_count = redact_rolls(message.get("text", ""))
    enriched = dict(message)
    enriched.update(
        {
            "tag": tag,
            "deadline_text": deadline_text,
            "deadline_iso": deadline_iso,
            "display_text": display_text,
            "roll_count": roll_count,
        }
    )
    return enriched


# ---------------------------------------------------------------------------
# Optional local-LLM hook (disabled by default; never required for the MVP).
# ---------------------------------------------------------------------------

def refine_with_llm(messages, model="qwen2.5:3b",
                    endpoint="http://localhost:11434", enabled=False):
    """Optionally ask a local Ollama model to double-check rule tags.

    Disabled by default -- pass enabled=True (build.py does this only when
    the OLLAMA_REFINE=1 environment variable is set). Any failure (no
    server, bad reply) leaves the rule-based tags untouched.
    """
    if not enabled:
        return messages
    tags = ["Schedule", "Deadline", "Action", "Opportunity",
            "Attendance", "Resource", "Noise"]
    for message in messages:
        try:
            payload = json.dumps({
                "model": model,
                "stream": False,
                "messages": [
                    {"role": "system",
                     "content": "Classify the WhatsApp message into exactly one tag: "
                                + ", ".join(tags) + ". Reply with only the tag."},
                    {"role": "user",
                     "content": message.get("text", "")[:1000]},
                ],
            }).encode("utf-8")
            request = urllib.request.Request(
                endpoint.rstrip("/") + "/api/chat", data=payload,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=30) as response:
                reply = json.load(response)
            guess = reply.get("message", {}).get("content", "").strip()
            if guess in tags:
                message["tag_llm"] = guess
        except Exception:
            continue  # local model unavailable: keep rule-based tags
    return messages
