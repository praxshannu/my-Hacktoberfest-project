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

Optional LLM hook: refine_with_llm() can double-check rule tags against a
local LM Studio server (OpenAI-compatible API). It is DISABLED by default
and never required -- the rules above are the whole MVP. Enable with
LLM_REFINE=1 and LLM_MODEL=<model id> (see build.py and README).

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
    from parser import MEDIA_MARKERS  # single source of truth for placeholders

    stripped = _text_without_urls(text)
    for marker in MEDIA_MARKERS:
        stripped = stripped.replace(marker, " ").replace(marker.upper(), " ")
    stripped = re.sub(r"\s+", " ", stripped).strip(" .,:;-|")
    # A filename or any real word is context -- e.g. a forwarded marks PDF
    # is a Resource, while a bare "<image omitted>" is Noise.
    if re.search(r"\.\w{2,4}\b", stripped):
        return False
    if re.findall(r"[A-Za-z]{4,}", stripped):
        return False
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
# Optional local-LLM hook: LM Studio's OpenAI-compatible server.
# Disabled by default; never required for the MVP.
#
#   LLM_REFINE=1            turn the second opinion on
#   LLM_MODEL=<model id>    required when refining (no default)
#   LLM_BASE_URL=...        default http://localhost:1234/v1
#   LLM_OVERRIDE=1          let the LLM change "tag"; default off, in which
#                           case only "tag_llm" is recorded for review
# ---------------------------------------------------------------------------

VALID_TAGS = ["Schedule", "Deadline", "Action", "Opportunity",
              "Attendance", "Resource", "Noise"]

LLM_DEFAULT_BASE_URL = "http://localhost:1234/v1"
LLM_TIMEOUT_SECONDS = 30
LLM_MAX_TEXT_CHARS = 1000

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)

# Run status of the last refine_with_llm() call, for the caller to report.
last_llm_run = {
    "enabled": False, "model": None, "base_url": LLM_DEFAULT_BASE_URL,
    "override": False, "checked": 0, "disagreements": [], "error": None,
}


def parse_llm_tag(text):
    """Map free-form model output to one canonical tag, or None.

    Strips any <think>...</think> reasoning block, then takes the first
    word that exactly matches a valid tag (case-insensitive, so
    "  deadline. " maps to "Deadline"). Anything else is ignored.
    """
    cleaned = _THINK_RE.sub(" ", text or "")
    for word in re.split(r"\s+", cleaned):
        token = re.sub(r"^[^A-Za-z]+|[^A-Za-z]+$", "", word)
        for tag in VALID_TAGS:
            if token and token.lower() == tag.lower():
                return tag
    return None


def _llm_settings(model=None, base_url=None, enabled=None, override=None):
    """Resolve settings from arguments, falling back to environment."""
    if enabled is None:
        enabled = os.environ.get("LLM_REFINE") == "1"
    if base_url is None:
        base_url = os.environ.get("LLM_BASE_URL", LLM_DEFAULT_BASE_URL)
    if model is None:
        model = os.environ.get("LLM_MODEL")
    if override is None:
        override = os.environ.get("LLM_OVERRIDE") == "1"
    return enabled, model, base_url, override


def _query_llm_tag(base_url, model, text):
    """Ask the chat-completions endpoint for one tag; None when unusable."""
    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system",
             "content": "Classify the WhatsApp message into exactly one tag: "
                        + ", ".join(VALID_TAGS) + ". Reply with only the tag."},
            {"role": "user", "content": (text or "")[:LLM_MAX_TEXT_CHARS]},
        ],
        "temperature": 0,
        "max_tokens": 10,
    }).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions", data=payload,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=LLM_TIMEOUT_SECONDS) as response:
        reply = json.load(response)
    content = reply["choices"][0]["message"]["content"]
    return parse_llm_tag(content)


def refine_with_llm(messages, model=None, base_url=None,
                    enabled=None, override=None):
    """Optionally double-check rule tags with a local LM Studio server.

    Disabled by default (returns messages untouched). When enabled, each
    message gains "tag_llm" (and "tag_rule" preserving the rule tag); the
    rule "tag" only changes when override is on. Any per-message failure
    (server down, bad reply) keeps the rule tag. Raises ValueError when
    refining is enabled but LLM_MODEL is missing.

    Run status is recorded in the module-level last_llm_run dict for the
    caller to report (checked count, disagreements, first error).
    """
    enabled, model, base_url, override = _llm_settings(
        model, base_url, enabled, override)
    last_llm_run.update({
        "enabled": enabled, "model": model, "base_url": base_url,
        "override": override, "checked": 0, "disagreements": [], "error": None,
    })
    if not enabled:
        return messages
    if not model:
        error = ("LLM_MODEL is required when LLM_REFINE=1 "
                 "(e.g. LLM_MODEL=qwen2.5-7b-instruct)")
        last_llm_run["error"] = error
        raise ValueError(error)
    for index, message in enumerate(messages):
        rule_tag = message.get("tag")
        try:
            guess = _query_llm_tag(base_url, model, message.get("text", ""))
        except Exception as exc:  # server down, timeout, bad reply: keep rules
            if last_llm_run["error"] is None:
                last_llm_run["error"] = "%s: %s" % (
                    type(exc).__name__, exc)
            continue
        if guess is None:
            continue
        message["tag_llm"] = guess
        message["tag_rule"] = rule_tag
        last_llm_run["checked"] += 1
        if guess != rule_tag:
            last_llm_run["disagreements"].append({
                "id": message.get("id", "m%04d" % (index + 1)),
                "preview": message.get(
                    "preview",
                    re.sub(r"\s+", " ", message.get("text", ""))[:140]),
                "rule_tag": rule_tag,
                "llm_tag": guess,
            })
            if override:
                message["tag"] = guess
    return messages
