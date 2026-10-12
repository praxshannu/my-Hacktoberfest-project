"""Pure helpers for cached, date-aware task extraction."""
import hashlib
from datetime import date, timedelta

TASK_TAGS = {"Action", "Deadline", "Schedule", "Opportunity"}
BUCKET_ORDER = {"overdue": 0, "today": 1, "tomorrow": 2, "this_week": 3, "later": 4, "no_date": 5}


def text_hash(notice):
    return hashlib.sha256(str(notice.get("display_text", notice.get("text", ""))).encode("utf-8")).hexdigest()


def bucket_for(due_date, today):
    if not due_date:
        return "no_date"
    try:
        due = date.fromisoformat(due_date)
    except (TypeError, ValueError):
        return "no_date"
    delta = (due - today).days
    if delta < 0:
        return "overdue"
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta <= 7:
        return "this_week"
    return "later"


def eligible(notice, today):
    if notice.get("tag") not in TASK_TAGS:
        return False
    deadline = notice.get("deadline_iso")
    if not deadline:
        return True
    try:
        return date.fromisoformat(deadline) >= today - timedelta(days=7)
    except ValueError:
        return False


def public_task(task, notice, today):
    due = task.get("due_date") or notice.get("deadline_iso")
    return {
        "id": task["id"], "title": task.get("title", "Untitled task"),
        "steps": task.get("steps", []), "due_date": due,
        "due_text": task.get("due_text"), "source_notice_id": notice["id"],
        "group": notice.get("group", (notice.get("groups") or [""])[0]),
        "bucket": bucket_for(due, today), "timestamp": notice.get("timestamp", "")
    }


def sort_tasks(items):
    dated = sorted((x for x in items if x["bucket"] != "no_date"), key=lambda x: (BUCKET_ORDER[x["bucket"]], x.get("due_date") or ""))
    undated = sorted((x for x in items if x["bucket"] == "no_date"), key=lambda x: x.get("timestamp", ""), reverse=True)
    return dated + undated


def task_diff(notices, cached, today):
    """Classify the current notice/task relation without calling a model."""
    current = {n.get("id"): n for n in notices}
    result = {"new": [], "changed": [], "removed": [], "unchanged": []}
    for notice_id, task in cached.items():
        notice = current.get(notice_id)
        if not notice or not eligible(notice, today):
            result["removed"].append(notice_id)
    for notice in notices:
        if not eligible(notice, today):
            continue
        old = cached.get(notice.get("id"))
        if not old:
            result["new"].append(notice)
        elif old.get("text_hash") == text_hash(notice):
            result["unchanged"].append(notice)
        else:
            result["changed"].append(notice)
    return result


def first_sentence(text, max_words=8):
    sentence = str(text or "").replace("\n", " ").split(".", 1)[0].strip()
    words = sentence.split()
    return " ".join(words[:max_words]) or "Untitled task"


def rule_task(notice):
    return {"id": notice["id"], "text_hash": text_hash(notice), "title": first_sentence(notice.get("display_text")),
            "steps": [], "due_date": notice.get("deadline_iso"), "due_text": notice.get("deadline_text"),
            "actionable": True, "source": "rule"}
