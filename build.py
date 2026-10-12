"""Build pipeline: parse exports -> categorize -> write JSON for the web UI.

Reads every *.txt export in data/, writes:

    output/messages.json  deduped notices (one entry per unique notice,
                          with seen_count, groups, and all occurrence times)
    output/summary.json   counts per tag, upcoming deadlines sorted by date,
                          duplicate groups, and build metadata

Duplicate notices (same text within or across groups) are grouped by a
normalized key and shown once by the UI with a "seen N times" badge.

Optional: set OLLAMA_REFINE=1 to let a local Ollama server double-check
tags (disabled by default; rule-based tags are the MVP).

Standard library only. Usage:  python3 build.py [--data DIR] [--out DIR]
"""

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone

from categorizer import load_rules, refine_with_llm, tag_message
from parser import parse_all

WS_RE = re.compile(r"\s+")


def duplicate_key(text):
    """Normalized identity for a notice: case/whitespace-insensitive."""
    key = text.lower().strip()
    key = WS_RE.sub(" ", key).strip(" .,:;-|")
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def dedupe(messages):
    """Collapse identical notices; keep first as representative."""
    groups = {}
    order = []
    for index, message in enumerate(messages):
        key = duplicate_key(message.get("text", ""))
        if key not in groups:
            groups[key] = {
                "id": "m%04d" % (len(order) + 1),
                "key": key,
                "tag": message["tag"],
                "timestamp": message["timestamp"],
                "timestamp_raw": message.get("timestamp_raw"),
                "date": (message["timestamp"] or "")[:10],
                "sender_masked": message["sender_masked"],
                "senders": [],
                "group": message["group"],
                "groups": [],
                "text": message["text"],
                "display_text": message["display_text"],
                "roll_count": message["roll_count"],
                "deadline_text": message["deadline_text"],
                "deadline_iso": message["deadline_iso"],
                "is_forwarded": message["is_forwarded"],
                "is_media": message["is_media"],
                "is_deleted": message["is_deleted"],
                "seen_count": 0,
                "occurrences": [],
                "preview": WS_RE.sub(" ", message["display_text"])[:140],
            }
            order.append(key)
        entry = groups[key]
        entry["seen_count"] += 1
        entry["occurrences"].append(
            {
                "group": message["group"],
                "timestamp": message["timestamp"],
                "sender_masked": message["sender_masked"],
            }
        )
        if message["sender_masked"] not in entry["senders"]:
            entry["senders"].append(message["sender_masked"])
        if message["group"] not in entry["groups"]:
            entry["groups"].append(message["group"])
        # Representative keeps the earliest timestamp and the richest text.
        if message["timestamp"] and (
            entry["timestamp"] is None or message["timestamp"] < entry["timestamp"]
        ):
            entry["timestamp"] = message["timestamp"]
            entry["timestamp_raw"] = message.get("timestamp_raw")
            entry["date"] = (message["timestamp"] or "")[:10]
        if len(message.get("display_text", "")) > len(entry["display_text"]):
            entry["display_text"] = message["display_text"]
            entry["text"] = message["text"]
            entry["preview"] = WS_RE.sub(" ", message["display_text"])[:140]
    notices = [groups[key] for key in order]
    notices.sort(key=lambda n: n["timestamp"] or "", reverse=True)
    return notices


def build_summary(notices, total_messages):
    counts = Counter(n["tag"] for n in notices)
    deadlines = [
        {
            "id": n["id"],
            "date": n["deadline_iso"],
            "label": n["deadline_text"],
            "tag": n["tag"],
            "preview": n["preview"],
            "groups": n["groups"],
            "seen_count": n["seen_count"],
        }
        for n in notices
        if n["deadline_iso"]
    ]
    deadlines.sort(key=lambda d: d["date"])
    duplicates = [
        {
            "id": n["id"],
            "seen_count": n["seen_count"],
            "groups": n["groups"],
            "preview": n["preview"],
        }
        for n in notices
        if n["seen_count"] > 1
    ]
    duplicates.sort(key=lambda d: d["seen_count"], reverse=True)
    stamps = sorted(n["timestamp"] for n in notices if n["timestamp"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_messages": total_messages,
        "unique_notices": len(notices),
        "counts_per_tag": dict(counts),
        "date_range": [stamps[0][:10] if stamps else None,
                       stamps[-1][:10] if stamps else None],
        "upcoming_deadlines": deadlines[:30],
        "duplicate_groups": duplicates,
    }


def main():
    parser = argparse.ArgumentParser(description="Build organizer JSON output.")
    parser.add_argument("--data", default="data", help="chat exports directory")
    parser.add_argument("--out", default="output", help="output directory")
    args = parser.parse_args()

    rules = load_rules()
    raw = parse_all(args.data)
    tagged = [tag_message(message, rules) for message in raw]

    if os.environ.get("OLLAMA_REFINE") == "1":
        refine_with_llm(tagged, enabled=True)

    notices = dedupe(tagged)
    summary = build_summary(notices, len(raw))

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "messages.json"), "w", encoding="utf-8") as fh:
        json.dump(notices, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    print("messages: %d -> %d unique notices" % (len(raw), len(notices)))
    print("tag counts (unique): %s" % json.dumps(summary["counts_per_tag"]))
    print("duplicates: %d groups" % len(summary["duplicate_groups"]))
    print("deadlines: %d extracted" % len(summary["upcoming_deadlines"]))
    print("wrote %s" % os.path.join(args.out, "messages.json"))


if __name__ == "__main__":
    main()
