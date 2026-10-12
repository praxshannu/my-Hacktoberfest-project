"""Build pipeline: parse exports -> categorize -> write JSON for the web UI.

Reads every *.txt export in data/, writes:

    output/messages.json  deduped notices (one entry per unique notice,
                          with seen_count, groups, and all occurrence times)
    output/summary.json   counts per tag, upcoming deadlines (today onward,
                          sorted by date), duplicate groups, build metadata
    output/llm_disagreements.json  rule-vs-LLM tag mismatches (only when
                          LLM_REFINE=1 ran)

Duplicate notices (same text within or across groups) are grouped by a
normalized key and shown once by the UI with a "seen N times" badge.

Optional: set LLM_REFINE=1 and LLM_MODEL=<model id> to double-check tags
against a local LM Studio server (OpenAI-compatible API, default
http://localhost:1234/v1). Disabled by default; rule-based tags are the
MVP. Refining runs on unique notices only, after dedupe.

Standard library only. Usage:  python3 build.py [--data DIR] [--out DIR]
"""

import argparse
import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone

from categorizer import last_llm_run, load_rules, refine_with_llm, tag_message
from parser import parse_all

WS_RE = re.compile(r"\s+")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PASSWORD_RE = re.compile(r"\b(password|passwd|pwd)\s*[:=]\s*([^\s,;]+)", re.IGNORECASE)


def redact_display(text):
    """Final privacy pass for UI text, independent of parser internals."""
    text = PASSWORD_RE.sub(lambda m: "%s: [hidden]" % m.group(1), text or "")
    return EMAIL_RE.sub("[email hidden]", text)


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
                "display_text": redact_display(message["display_text"]),
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
            entry["display_text"] = redact_display(message["display_text"])
            entry["text"] = message["text"]
            entry["preview"] = WS_RE.sub(" ", message["display_text"])[:140]
    notices = [groups[key] for key in order]
    notices.sort(key=lambda n: n["timestamp"] or "", reverse=True)
    return notices


def build_summary(notices, total_messages):
    counts = Counter(n["tag"] for n in notices)
    today = datetime.now().date().isoformat()
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
        if n["deadline_iso"] and n["deadline_iso"] >= today
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


def run_llm_refine(notices, out_dir):
    """Refine unique notices with LM Studio; returns the one-line status.

    Writes output/llm_disagreements.json. Any failure keeps rule tags.
    """
    disagreements_path = os.path.join(out_dir, "llm_disagreements.json")
    if os.environ.get("LLM_REFINE") != "1":
        if os.path.exists(disagreements_path):
            os.remove(disagreements_path)  # stale results from an older run
        return "LLM refine: off, using rules only."
    try:
        refine_with_llm(notices)
    except ValueError as exc:  # e.g. LLM_MODEL missing
        with open(disagreements_path, "w", encoding="utf-8") as fh:
            json.dump([], fh)
        return "LLM refine: unavailable (%s), using rules only." % exc
    run = last_llm_run
    with open(disagreements_path, "w", encoding="utf-8") as fh:
        json.dump(run["disagreements"], fh, ensure_ascii=False, indent=2)
    if run["checked"] == 0 and notices:
        reason = run["error"] or "server gave no usable tags"
        return "LLM refine: unavailable (%s), using rules only." % reason
    return "LLM refine: ON (model %s at %s), %d disagreements." % (
        run["model"], run["base_url"], len(run["disagreements"]))


def main():
    parser = argparse.ArgumentParser(description="Build organizer JSON output.")
    parser.add_argument("--data", default="data", help="chat exports directory")
    parser.add_argument("--out", default="output", help="output directory")
    args = parser.parse_args()

    rules = load_rules()
    raw = parse_all(args.data)
    tagged = [tag_message(message, rules) for message in raw]
    notices = dedupe(tagged)

    os.makedirs(args.out, exist_ok=True)
    llm_status = run_llm_refine(notices, args.out)
    summary = build_summary(notices, len(raw))
    with open(os.path.join(args.out, "messages.json"), "w", encoding="utf-8") as fh:
        json.dump(notices, fh, ensure_ascii=False, indent=2)
    with open(os.path.join(args.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)

    print("messages: %d -> %d unique notices" % (len(raw), len(notices)))
    print("tag counts (unique): %s" % json.dumps(summary["counts_per_tag"]))
    print("duplicates: %d groups" % len(summary["duplicate_groups"]))
    print("deadlines: %d extracted" % len(summary["upcoming_deadlines"]))
    print(llm_status)
    print("wrote %s" % os.path.join(args.out, "messages.json"))


if __name__ == "__main__":
    main()
