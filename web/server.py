"""Stdlib-only local web server and loopback LM Studio proxy."""
import json, os, re, threading, time
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError
try:
    from .tasks import eligible, public_task, sort_tasks, text_hash, task_diff, rule_task
except ImportError:  # direct `python web/server.py` launch
    from tasks import eligible, public_task, sort_tasks, text_hash, task_diff, rule_task

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_BASE = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:1234").rstrip("/")
PORT = int(os.environ.get("PORT", "8000"))
_url = urlsplit(RAW_BASE if "://" in RAW_BASE else "http://" + RAW_BASE)
if _url.hostname not in {"127.0.0.1", "localhost", "::1"}:
    raise SystemExit("FATAL: LLM_BASE_URL must point to 127.0.0.1, localhost, or ::1")
OPENAI_BASE = RAW_BASE if _url.path.rstrip("/").endswith("/v1") else RAW_BASE + "/v1"
HOST_ROOT = OPENAI_BASE[:-3] if OPENAI_BASE.endswith("/v1") else OPENAI_BASE
TASK_JOB_LOCK = threading.Lock()
TASK_JOB = {"state": "idle", "done": 0, "total": 0, "added": 0, "updated": 0, "removed": 0, "skipped": 0, "error": None, "model_calls": 0}

class UpstreamError(Exception):
    def __init__(self, status, detail):
        self.status, self.detail = status, detail
        super().__init__(detail)


def _request(url, method="GET", body=None, timeout=5):
    data = None if body is None else json.dumps(body).encode()
    req = Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as res: return json.loads(res.read().decode("utf-8", "replace"))
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace").strip()
        try:
            parsed = json.loads(raw)
            detail = parsed.get("error", parsed)
            detail = detail.get("message", detail) if isinstance(detail, dict) else detail
        except (ValueError, TypeError):
            detail = raw or "no response body"
        raise UpstreamError(exc.code, "LM Studio returned HTTP %d: %s" % (exc.code, detail))

def _error(exc):
    if isinstance(exc, TimeoutError) or getattr(exc, "reason", None) == "timed out": return "Timed out"
    if isinstance(exc, UpstreamError): return str(exc)
    if isinstance(exc, HTTPError): return "LM Studio returned HTTP %d" % exc.code
    return "LM Studio not running on 127.0.0.1:1234"

def model_status():
    try: data = _request(OPENAI_BASE + "/models")
    except Exception as exc: return False, [], _error(exc)
    raw = data.get("data", []) if isinstance(data, dict) else []
    ids = [x.get("id") for x in raw if isinstance(x, dict) and x.get("id") and "embed" not in x.get("id", "").lower()]
    return (True, ids, None) if ids else (False, [], "No model loaded in LM Studio")

def strip_thinking(text): return re.sub(r"<think>.*?</think>", "", text or "", flags=re.I | re.S).strip()

def date_context(now=None):
    now = now or datetime.now().astimezone()
    return now.date(), "Today is %s, %d %s %d (%s). Resolve 'today', 'tomorrow', weekdays, and 'this week' relative to this date." % (now.strftime("%A"), now.day, now.strftime("%B"), now.year, now.date().isoformat())

def notices_for_question(question, notices):
    """Apply deterministic constraints for the today-actions query."""
    if "what should i do today" not in question.lower(): return notices
    today, _ = date_context()
    selected = []
    for notice in notices:
        deadline = None
        try: deadline = datetime.strptime(notice.get("deadline_iso", ""), "%Y-%m-%d").date()
        except (TypeError, ValueError): pass
        text = str(notice.get("display_text", "")).lower()
        if deadline and today - timedelta(days=7) <= deadline <= today + timedelta(days=7):
            selected.append(notice)
        elif notice.get("tag") == "Action" and ("today" in text or "tomorrow" in text):
            selected.append(notice)
    return selected

def prompt_for(question, notices, cap=9000):
    notices = notices_for_question(question, notices)
    ordered = sorted(notices, key=lambda n: (n.get("deadline_iso") or "9999-99-99", n.get("timestamp") or ""))
    chunks = []
    for n in ordered:
        chunk = json.dumps({k: n.get(k, "") for k in ("id", "tag", "group", "deadline_iso")}, ensure_ascii=False)
        chunk += " display_text=" + str(n.get("display_text", ""))[:400]
        if len("\n".join(chunks + [chunk])) > cap: break
        chunks.append(chunk)
    omitted = len(ordered) - len(chunks)
    note = "\n%d least urgent notices omitted due to prompt size." % omitted if omitted else ""
    _, date_line = date_context()
    system = (date_line + " Answer only from the supplied notices and do not invent dates, names, or links. Give only the final answer. "
              "For greetings or questions about the app (such as hi or what can you do), reply briefly that you can answer "
              "questions about the visible notices, such as deadlines, actions, and schedules. "
              "If the notices don't contain the answer, say exactly: The notices don't contain enough information to answer this. "
              "For 'what should I do today', list only tasks due today plus overdue items, then put undated tasks under a separate heading 'No date'. "
              "Use short bullet points with a date on each item, no more than 8 items. If nothing is due today, say so clearly and list the next 3 dated items. "
              "For greetings, reply in one sentence.")
    user = "Question: %s\nNotices:\n%s%s" % (question, "\n".join(chunks), note)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}], len(chunks)


def _read_task_cache():
    path = os.path.join(ROOT, "output", "tasks.json")
    try:
        with open(path, encoding="utf-8") as fh: cache = json.load(fh)
    except (OSError, ValueError):
        cache = {"updated_at": None, "tasks": {}}
    cache.setdefault("tasks", {})
    return path, cache


def _write_task_cache(path, cache):
    cache["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    with open(path, "w", encoding="utf-8") as fh: json.dump(cache, fh, ensure_ascii=False, indent=2)


def _keyword_context(batch, tasks, notices):
    groups = {n.get("group") for n in batch}
    words = {w.lower() for n in batch for w in re.findall(r"[A-Za-z]{4,}", n.get("display_text", ""))}
    picked = []
    for task_id, task in tasks.items():
        source = notices.get(task_id, {})
        haystack = (str(task.get("title", "")) + " " + str(source.get("display_text", ""))).lower()
        if source.get("group") in groups or any(word in haystack for word in words):
            picked.append({"id": task_id, "title": task.get("title", ""), "due_date": task.get("due_date")})
    return picked[:10]


def _action_batches(notices):
    actions = [n for n in notices if n.get("tag") == "Action"]
    return [actions[i:i + 5] for i in range(0, len(actions), 5)]


def _model_batch(batch, existing, model, date_line):
    notices = [{"id": n["id"], "display_text": str(n.get("display_text", ""))[:600], "group": n.get("group", "")} for n in batch]
    user = json.dumps({"notices": notices, "existing_tasks": existing}, ensure_ascii=False)
    system = date_line + " Return a JSON object only: {\"ops\":[{\"op\":\"add\"|\"update\"|\"none\",\"notice_id\":\"...\",\"task_id\":\"existing id, only for update\",\"title\":\"max 8 words\",\"steps\":[\"...\"],\"due_text\":\"phrase from notice or null\"}]}. Ignore dates from the model; dates come only from deadline_iso."
    if len(system) + len(user) >= 3000:
        notices = [{"id": n["id"], "display_text": str(n.get("display_text", ""))[:300]} for n in batch]
        user = json.dumps({"notices": notices, "existing_tasks": existing}, ensure_ascii=False)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _run_task_update(model):
    global TASK_JOB
    try:
        with open(os.path.join(ROOT, "output", "messages.json"), encoding="utf-8") as fh: notices = json.load(fh)
        today, date_line = date_context(); path, cache = _read_task_cache(); tasks = cache["tasks"]
        diff = task_diff(notices, tasks, today); notice_map = {n["id"]: n for n in notices}
        for task_id in diff["removed"]: tasks.pop(task_id, None)
        for notice in diff["new"] + diff["changed"]:
            if notice.get("tag") in {"Deadline", "Schedule"} and notice.get("deadline_iso"):
                tasks[notice["id"]] = rule_task(notice)
        action_changes = [n for n in diff["new"] + diff["changed"] if n.get("tag") == "Action"]
        planned = (len(action_changes) + 4) // 5
        TASK_JOB.update({"state": "running", "done": 0, "total": len(action_changes), "added": 0, "updated": 0, "removed": len(diff["removed"]), "skipped": 0, "error": None, "model_calls": 0})
        _write_task_cache(path, cache)
        if action_changes and model:
            for batch in [action_changes[i:i + 5] for i in range(0, len(action_changes), 5)]:
                messages = _model_batch(batch, _keyword_context(batch, tasks, notice_map), model, date_line)
                data = None
                for attempt in range(2):
                    try:
                        data = _request(OPENAI_BASE + "/chat/completions", "POST", {"model": model, "messages": messages, "reasoning_effort": "none", "temperature": 0, "max_tokens": 800}, 90)
                        break
                    except Exception as exc:
                        if attempt == 1: print("Task batch failed for %s: %s" % (", ".join(n["id"] for n in batch), exc), flush=True)
                TASK_JOB["model_calls"] += 1
                if data is None:
                    TASK_JOB["skipped"] += len(batch); TASK_JOB["done"] += len(batch); continue
                try: ops = json.loads(strip_thinking(((data.get("choices") or [{}])[0].get("message") or {}).get("content", ""))).get("ops", [])
                except (ValueError, TypeError, AttributeError): ops = []
                batch_ids = {n["id"] for n in batch}; task_ids = set(tasks)
                for op in ops:
                    if op.get("notice_id") not in batch_ids or op.get("op") not in {"add", "update", "none"}: continue
                    if op["op"] == "update" and op.get("task_id") not in task_ids: continue
                    if op["op"] == "none": continue
                    notice = notice_map[op["notice_id"]]
                    task_id = op.get("task_id") if op["op"] == "update" else notice["id"]
                    previous = tasks.get(task_id, {})
                    tasks[task_id] = {"id": task_id, "text_hash": text_hash(notice), "title": " ".join(str(op.get("title", "Untitled task")).split()[:8]), "steps": op.get("steps", []), "due_date": notice.get("deadline_iso"), "due_text": op.get("due_text"), "actionable": True, "source": "model"}
                    TASK_JOB["updated" if previous else "added"] += 1
                TASK_JOB["done"] += len(batch); _write_task_cache(path, cache)
        elif action_changes:
            TASK_JOB["skipped"] = len(action_changes); TASK_JOB["done"] = len(action_changes)
        _write_task_cache(path, cache); TASK_JOB["state"] = "done"
    except Exception as exc:
        TASK_JOB.update({"state": "error", "error": str(exc)})
    finally:
        TASK_JOB["lock_released"] = True

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw): super().__init__(*a, directory=ROOT, **kw)
    def log_message(self, fmt, *args): pass
    def send_json(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def app_log(self, route, model, count, status): print("%s %s model=%s notices=%d status=%d" % (datetime.now().isoformat(timespec="seconds"), route, model or "-", count, status), flush=True)
    def do_GET(self):
        if self.path == "/":
            self.send_response(302)
            self.send_header("Location", "/web/")
            self.end_headers()
            return
        if self.path == "/api/notices":
            path = os.path.join(ROOT, "output", "messages.json")
            if not os.path.exists(path):
                self.send_json(404, {"error": "Notice data is missing: output/messages.json"})
                self.app_log(self.path, "", 0, 404)
                return
            try:
                with open(path, "r", encoding="utf-8") as fh: notices = json.load(fh)
                if not isinstance(notices, list): raise ValueError("notice data is not an array")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                self.send_json(500, {"error": "Could not read output/messages.json: %s" % exc})
                self.app_log(self.path, "", 0, 500)
                return
            self.send_json(200, notices); self.app_log(self.path, "", len(notices), 200); return
        if self.path in ("/api/status", "/api/models"):
            today, _ = date_context(); body = {"today_iso": today.isoformat(), "today_label": today.strftime("%A, %-d %B %Y")}
            ok, ids, err = model_status(); body.update({"connected": ok, "models": ids});
            if err: body["error"] = err
            self.send_json(200, body); self.app_log(self.path, "", 0, 200); return
        if self.path == "/api/tasks":
            self.send_json(200, load_tasks()); return
        if self.path == "/api/tasks/status":
            self.send_json(200, {k: TASK_JOB.get(k) for k in ("state", "done", "total", "added", "updated", "removed", "skipped", "error")}); return
        super().do_GET()
    def do_POST(self):
        if self.path == "/api/tasks/update":
            self.update_tasks(); return
        if self.path == "/api/tasks/generate":
            self.generate_tasks(); return
        if self.path != "/api/chat": self.send_json(404, {"error": "Unknown route"}); return
        try: body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except Exception: self.send_json(400, {"error": "Invalid JSON body"}); self.app_log(self.path, "", 0, 400); return
        model, question, notices = body.get("model"), body.get("question"), body.get("notices", [])
        if not isinstance(model, str) or not model or not isinstance(question, str) or not question.strip() or not isinstance(notices, list):
            self.send_json(400, {"error": "Body must contain model, question, and notices"}); self.app_log(self.path, model, 0, 400); return
        ok, ids, err = model_status()
        if not ok: self.send_json(503, {"error": err}); self.app_log(self.path, model, len(notices), 503); return
        if model not in ids: self.send_json(400, {"error": "Model '%s' is not currently loaded" % model}); self.app_log(self.path, model, len(notices), 400); return
        messages, included = prompt_for(question, notices)
        try:
            request_body = {"model": model, "messages": messages, "reasoning_effort": "none", "temperature": 0, "max_tokens": 1500}
            try:
                data = _request(OPENAI_BASE + "/chat/completions", "POST", request_body, 45)
            except UpstreamError as exc:
                if "reasoning_effort" not in str(exc).lower(): raise
                print("LM Studio rejected reasoning_effort; retrying once without it", flush=True)
                request_body.pop("reasoning_effort", None)
                data = _request(OPENAI_BASE + "/chat/completions", "POST", request_body, 45)
            choice = (data.get("choices") or [{}])[0]
            content = strip_thinking((choice.get("message") or {}).get("content", ""))
            if not content:
                message = ("The model ran out of room before answering. Try a shorter question or fewer notices."
                           if choice.get("finish_reason") == "length" else
                           "The model returned no answer. Check LM Studio's logs.")
                raise ValueError(message)
        except ValueError as exc: self.send_json(502, {"error": str(exc)}); self.app_log(self.path, model, included, 502); return
        except Exception as exc: self.send_json(502, {"error": _error(exc)}); self.app_log(self.path, model, included, 502); return
        self.send_json(200, {"answer": content, "model": model, "notice_count": included}); self.app_log(self.path, model, included, 200)

    def update_tasks(self):
        global TASK_JOB
        if not TASK_JOB_LOCK.acquire(blocking=False):
            self.send_json(409, {"error": "A task update is already running"}); return
        try:
            try: body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            except Exception: body = {}
            model = body.get("model")
            with open(os.path.join(ROOT, "output", "messages.json"), encoding="utf-8") as fh: notices = json.load(fh)
            today, _ = date_context(); _, cache = _read_task_cache(); diff = task_diff(notices, cache["tasks"], today)
            action_changes = [n for n in diff["new"] + diff["changed"] if n.get("tag") == "Action"]
            counts = {"new": len(diff["new"]), "changed": len(diff["changed"]), "removed": len(diff["removed"]), "unchanged": len(diff["unchanged"]), "model_calls_planned": (len(action_changes) + 4) // 5}
            if not action_changes and not any(n.get("tag") in {"Deadline", "Schedule"} and n.get("deadline_iso") for n in diff["new"] + diff["changed"]) and not diff["removed"]:
                TASK_JOB = {"state": "idle", "done": 0, "total": 0, "added": 0, "updated": 0, "removed": 0, "skipped": 0, "error": "Up to date", "model_calls": 0}
                TASK_JOB_LOCK.release(); self.send_json(202, dict(counts, message="Up to date")); return
            TASK_JOB = {"state": "starting", "done": 0, "total": len(action_changes), "added": 0, "updated": 0, "removed": 0, "skipped": 0, "error": None, "model_calls": 0}
            threading.Thread(target=lambda: (_run_task_update(model), TASK_JOB_LOCK.release()), daemon=True).start()
            self.send_json(202, counts)
        except Exception as exc:
            TASK_JOB_LOCK.release(); self.send_json(500, {"error": str(exc)})

    def generate_tasks(self):
        try: body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except Exception: body = {}
        model = body.get("model")
        try:
            with open(os.path.join(ROOT, "output", "messages.json"), encoding="utf-8") as fh: notices = json.load(fh)
            today, date_line = date_context(); path = os.path.join(ROOT, "output", "tasks.json")
            cache = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {"updated_at": None, "tasks": {}}
            cache.setdefault("tasks", {}); candidates = [n for n in notices if eligible(n, today)]
            ok, ids, err = model_status()
            if not model: model = ids[0] if ids else None
            generated = skipped = 0
            for index, notice in enumerate(candidates, 1):
                h = text_hash(notice); old = cache["tasks"].get(notice["id"])
                if old and old.get("text_hash") == h: continue
                if not ok or not model:
                    skipped += 1; continue
                messages = [{"role": "system", "content": date_line + " Return JSON only with keys actionable, title (max 8 words), steps, due_date, due_text. Extract only from this notice."}, {"role": "user", "content": json.dumps(notice, ensure_ascii=False)}]
                try:
                    data = _request(OPENAI_BASE + "/chat/completions", "POST", {"model": model, "messages": messages, "temperature": 0, "reasoning_effort": "none", "max_tokens": 400}, 45)
                    raw = strip_thinking(((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")); result = json.loads(raw)
                    if not result.get("actionable"): continue
                    result.update({"id": notice["id"], "text_hash": h})
                    if notice.get("deadline_iso"): result["due_date"] = notice["deadline_iso"]
                    cache["tasks"][notice["id"]] = result; generated += 1
                except Exception:
                    skipped += 1; print("Skipping invalid task JSON for %s" % notice["id"], flush=True)
            cache["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            with open(path, "w", encoding="utf-8") as fh: json.dump(cache, fh, ensure_ascii=False, indent=2)
            self.send_json(200, {"processed": len(candidates), "generated": generated, "skipped": skipped, "cached": len(cache["tasks"]), "updated_at": cache["updated_at"]})
        except Exception as exc: self.send_json(500, {"error": str(exc)})

def load_tasks():
    path = os.path.join(ROOT, "output", "tasks.json")
    try:
        cache = json.load(open(path, encoding="utf-8")); today, _ = date_context()
        notices = {n["id"]: n for n in json.load(open(os.path.join(ROOT, "output", "messages.json"), encoding="utf-8"))}
        items = [public_task(t, notices[t["id"]], today) for t in cache.get("tasks", {}).values() if t.get("actionable") and t.get("id") in notices]
        return {"updated_at": cache.get("updated_at"), "tasks": sort_tasks(items)}
    except (OSError, ValueError, KeyError, TypeError): return {"updated_at": None, "tasks": []}

def main():
    print("Organizer: http://127.0.0.1:%d/web/" % PORT, flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
if __name__ == "__main__": main()
