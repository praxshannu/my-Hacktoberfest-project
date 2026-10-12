"""Stdlib-only local web server and loopback LM Studio proxy."""
import json, os, re
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_BASE = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:1234").rstrip("/")
PORT = int(os.environ.get("PORT", "8000"))
_url = urlsplit(RAW_BASE if "://" in RAW_BASE else "http://" + RAW_BASE)
if _url.hostname not in {"127.0.0.1", "localhost", "::1"}:
    raise SystemExit("FATAL: LLM_BASE_URL must point to 127.0.0.1, localhost, or ::1")
OPENAI_BASE = RAW_BASE if _url.path.rstrip("/").endswith("/v1") else RAW_BASE + "/v1"
HOST_ROOT = OPENAI_BASE[:-3] if OPENAI_BASE.endswith("/v1") else OPENAI_BASE

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

def prompt_for(question, notices, cap=9000):
    ordered = sorted(notices, key=lambda n: (n.get("deadline_iso") or "9999-99-99", n.get("timestamp") or ""))
    chunks = []
    for n in ordered:
        chunk = json.dumps({k: n.get(k, "") for k in ("id", "tag", "group", "deadline_iso")}, ensure_ascii=False)
        chunk += " display_text=" + str(n.get("display_text", ""))[:400]
        if len("\n".join(chunks + [chunk])) > cap: break
        chunks.append(chunk)
    omitted = len(ordered) - len(chunks)
    note = "\n%d least urgent notices omitted due to prompt size." % omitted if omitted else ""
    system = ("Use only the supplied notices. Do not invent dates, names, or links. Give only the final answer with no reasoning. "
              "If the notices do not contain the answer, say exactly: The notices don't contain enough information to answer this.")
    user = "Question: %s\nNotices:\n%s%s" % (question, "\n".join(chunks), note)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}], len(chunks)

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw): super().__init__(*a, directory=ROOT, **kw)
    def log_message(self, fmt, *args): pass
    def send_json(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def app_log(self, route, model, count, status): print("%s %s model=%s notices=%d status=%d" % (datetime.now().isoformat(timespec="seconds"), route, model or "-", count, status), flush=True)
    def do_GET(self):
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
            ok, ids, err = model_status(); body = {"connected": ok, "models": ids};
            if err: body["error"] = err
            self.send_json(200, body); self.app_log(self.path, "", 0, 200); return
        super().do_GET()
    def do_POST(self):
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
            data = _request(OPENAI_BASE + "/chat/completions", "POST", {"model": model, "messages": messages, "temperature": .2, "max_tokens": 600}, 45)
            content = strip_thinking(((data.get("choices") or [{}])[0].get("message") or {}).get("content", ""))
            if not content: raise ValueError("Model returned no final answer")
        except ValueError as exc: self.send_json(502, {"error": str(exc)}); self.app_log(self.path, model, included, 502); return
        except Exception as exc: self.send_json(502, {"error": _error(exc)}); self.app_log(self.path, model, included, 502); return
        self.send_json(200, {"answer": content, "model": model, "notice_count": included}); self.app_log(self.path, model, included, 200)

def main():
    print("Organizer: http://127.0.0.1:%d/web/" % PORT, flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
if __name__ == "__main__": main()
