"""Parse standard WhatsApp chat exports into structured messages.

Input format (one message per header line, continuations on later lines):

    [M/D/YY, H:MM:SS AM/PM] Sender: text
    ...continuation of the same message...

Special content handled here:
  - "<image omitted>" / "<document omitted>" (and friends) -> is_media
  - "This message was deleted..."                     -> is_deleted
  - "[Forwarded]" prefix                              -> is_forwarded
  - Phone-number senders are masked, e.g. "+91 ***** 4322".
  - Phone-number-like runs inside message text are masked too, so no
    raw number ever reaches the UI JSON.

Standard library only. No third-party dependencies.
"""

import glob
import os
import re
from datetime import datetime

# "[6/15/26, 1:17:24 PM] Some Sender: message text"
MSG_RE = re.compile(
    r"^\[(\d{1,2}/\d{1,2}/\d{2,4}, \d{1,2}:\d{2}:\d{2} [AP]M)\]\s(.*?):\s(.*)$"
)

# Header lines with no "Sender:" part, e.g. "[6/17/26, 7:57:57 AM] - [System notification]".
# These still start a new message (a system one) instead of continuing the last.
SYS_RE = re.compile(
    r"^\[(\d{1,2}/\d{1,2}/\d{2,4}, \d{1,2}:\d{2}:\d{2} [AP]M)\]\s(.*)$"
)

FORWARDED_PREFIX = "[Forwarded]"

MEDIA_MARKERS = (
    "<image omitted>",
    "<video omitted>",
    "<document omitted>",
    "<audio omitted>",
    "<voice message omitted>",
    "<sticker omitted>",
    "<gif omitted>",
    "<unknown message>",
)

DELETED_PREFIX = "This message was deleted"

# Invisible unicode wrappers WhatsApp sometimes adds around senders.
_INVISIBLE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")

# A run that looks like a phone number: 10+ digits with optional
# separators, optional leading +. Roll numbers contain letters, so
# they never match this.
_PHONE_RE = re.compile(r"\+?\d[\d\s\-]{8,}\d")


def _digits(s):
    return re.sub(r"\D", "", s)


def mask_sender(sender):
    """Mask a sender name. Phone-like senders become '+91 ***** 4322'.

    Plain names ("SAI SAMPATH"), "You", and system senders pass through.
    """
    sender = _INVISIBLE.sub("", sender).strip()
    if sender in ("-", "System"):
        return "System"
    digits = _digits(sender)
    if len(digits) >= 10:
        return "+91 ***** %s" % digits[-4:]
    if sender.lower().startswith("you"):
        return "You"
    return sender


def mask_phones_in_text(text):
    """Replace phone-number-like runs in free text with a masked form.

    URL spans (query keys, IDs, video hashes) are left untouched so links
    keep working -- only human-typed number runs are masked.
    """
    url_spans = [
        (m.start(), m.end())
        for m in re.finditer(r"https?://\S+|www\.\S+", text)
    ]

    def _inside_url(pos):
        return any(start <= pos < end for start, end in url_spans)

    def _repl(match):
        if _inside_url(match.start()):
            return match.group(0)
        digits = _digits(match.group(0))
        if len(digits) < 10:
            return match.group(0)
        return "+91 ***** %s" % digits[-4:]

    return _PHONE_RE.sub(_repl, text)


def parse_timestamp(raw):
    """Return an ISO-8601 timestamp for a WhatsApp header date, else None."""
    for fmt in ("%m/%d/%y, %I:%M:%S %p", "%m/%d/%Y, %I:%M:%S %p"):
        try:
            return datetime.strptime(raw.strip(), fmt).isoformat()
        except ValueError:
            continue
    return None


def _split_raw_messages(text):
    """Split export text into (header_datetime, sender, body) tuples.

    Lines without a header prefix continue the previous message.
    Header-less preamble lines (e.g. the encryption notice) are skipped.
    """
    messages = []
    current = None  # [dt_raw, sender, body_lines]
    for line in text.splitlines():
        match = MSG_RE.match(line)
        if match:
            if current is not None:
                messages.append(current)
            current = [match.group(1), match.group(2), [match.group(3)]]
            continue
        sys_match = SYS_RE.match(line)
        if sys_match:
            # System line with no "Sender:" part: its own message.
            if current is not None:
                messages.append(current)
            current = [sys_match.group(1), "-", [sys_match.group(2)]]
            continue
        if current is None:
            continue  # preamble before the first message
        current[2].append(line)
    if current is not None:
        messages.append(current)
    return messages


def parse_file(path, group=None):
    """Parse one WhatsApp export file into a list of message dicts."""
    if group is None:
        group = os.path.splitext(os.path.basename(path))[0]
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()

    parsed = []
    for dt_raw, sender_raw, body_lines in _split_raw_messages(text):
        body = "\n".join(body_lines).strip()
        # Strip leading blank continuation lines (multi-line gap after header).
        body = body.strip("\n").strip()
        if not body and sender_raw.strip() == "-":
            continue  # system ping with no content (e.g. "Someone added You")

        is_forwarded = body.lstrip().startswith(FORWARDED_PREFIX)
        if is_forwarded:
            body = body.lstrip()[len(FORWARDED_PREFIX):].strip()

        lowered = body.lower()
        is_media = any(marker in lowered for marker in MEDIA_MARKERS)
        is_deleted = body.startswith(DELETED_PREFIX)

        parsed.append(
            {
                "timestamp": parse_timestamp(dt_raw),
                "timestamp_raw": dt_raw,
                "sender_masked": mask_sender(sender_raw),
                "text": mask_phones_in_text(body),
                "is_forwarded": is_forwarded,
                "is_media": is_media,
                "is_deleted": is_deleted,
                "group": group,
            }
        )
    return parsed


def parse_all(data_dir="data", pattern="*.txt"):
    """Parse every export in a directory. Returns a flat list of messages."""
    messages = []
    for path in sorted(glob.glob(os.path.join(data_dir, pattern))):
        messages.append((path, parse_file(path)))
    flat = [msg for _, msgs in messages for msg in msgs]
    return flat
