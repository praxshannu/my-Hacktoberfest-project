import json
import io
from unittest.mock import patch

import build
import web.server as server


class FakePostHandler:
    path = "/api/chat"
    def __init__(self, body):
        self.rfile = io.BytesIO(json.dumps(body).encode())
        self.headers = {"Content-Length": str(len(json.dumps(body).encode()))}
    def send_json(self, code, body): self.result = (code, body)
    def app_log(self, *args): pass


def chat_request(monkeypatch, upstream):
    monkeypatch.setattr(server, "model_status", lambda: (True, ["gemma-4-e2b-it-qat"], None))
    monkeypatch.setattr(server, "_request", upstream)
    fake = FakePostHandler({"model": "gemma-4-e2b-it-qat", "question": "hi", "notices": []})
    server.Handler.do_POST(fake)
    return fake.result


def test_build_redacts_passwords_and_emails():
    assert build.redact_display("email me at a.person@example.com; password: hunter2") == "email me at [email hidden]; [credential]: [hidden]"


def test_prompt_orders_deadlines_and_caps_without_splitting():
    notices = [
        {"id": "late", "tag": "Deadline", "group": "G", "deadline_iso": "2027-03-02", "timestamp": "2027-01-01", "display_text": "late"},
        {"id": "soon", "tag": "Deadline", "group": "G", "deadline_iso": "2027-01-01", "timestamp": "2027-01-02", "display_text": "soon"},
    ]
    messages, count = server.prompt_for("nearest", notices, cap=155)
    assert count == 1
    assert '"id": "soon"' in messages[1]["content"]
    assert '"id": "late"' not in messages[1]["content"]


def test_strip_think_and_reasoning_only_is_empty():
    assert server.strip_thinking("<think>hidden</think>final") == "final"
    assert server.strip_thinking("<think>hidden</think>") == ""


def test_status_filters_embeddings(monkeypatch):
    monkeypatch.setattr(server, "_request", lambda *a, **k: {"data": [{"id": "chat-model"}, {"id": "embedding-model"}]})
    assert server.model_status() == (True, ["chat-model"], None)


def test_status_distinguishes_no_model(monkeypatch):
    monkeypatch.setattr(server, "_request", lambda *a, **k: {"data": []})
    assert server.model_status() == (False, [], "No model loaded in LM Studio")


def test_prompt_has_whole_notice_limit():
    notices = [{"id": str(i), "deadline_iso": "2027-01-%02d" % ((i % 9) + 1), "display_text": "x" * 400} for i in range(50)]
    messages, count = server.prompt_for("question", notices)
    assert count < len(notices)
    assert "least urgent notices omitted" in messages[1]["content"]


def test_notices_route_returns_output_count():
    class FakeHandler:
        path = "/api/notices"
        def send_json(self, code, body): self.result = (code, body)
        def app_log(self, *args): pass
    fake = FakeHandler()
    server.Handler.do_GET(fake)
    with open("output/messages.json", encoding="utf-8") as fh:
        expected = json.load(fh)
    assert fake.result[0] == 200
    assert len(fake.result[1]) == len(expected)


def test_chat_content_present(monkeypatch):
    def upstream(url, method="GET", body=None, timeout=5):
        return {"choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}]}
    code, body = chat_request(monkeypatch, upstream)
    assert code == 200 and body["answer"] == "Hello"


def test_chat_length_error(monkeypatch):
    def upstream(url, method="GET", body=None, timeout=5):
        return {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]}
    code, body = chat_request(monkeypatch, upstream)
    assert code == 502
    assert body["error"] == "The model ran out of room before answering. Try a shorter question or fewer notices."


def test_chat_reasoning_only_never_leaks(monkeypatch):
    def upstream(url, method="GET", body=None, timeout=5):
        return {"choices": [{"message": {"content": ""}, "reasoning_content": "PRIVATE REASONING"}]}
    code, body = chat_request(monkeypatch, upstream)
    assert code == 502
    assert body["error"] == "The model returned no answer. Check LM Studio's logs."
    assert "PRIVATE REASONING" not in json.dumps(body)


def test_chat_retries_once_without_reasoning_effort(monkeypatch):
    calls = []
    def upstream(url, method="GET", body=None, timeout=5):
        calls.append(dict(body))
        if "reasoning_effort" in body: raise server.UpstreamError(400, "LM Studio returned HTTP 400: unknown reasoning_effort parameter")
        return {"choices": [{"message": {"content": "Hi"}, "finish_reason": "stop"}]}
    code, body = chat_request(monkeypatch, upstream)
    assert code == 200 and body["answer"] == "Hi"
    assert len(calls) == 2 and "reasoning_effort" in calls[0] and "reasoning_effort" not in calls[1]


def test_system_prompt_contains_greeting_rule(monkeypatch):
    captured = {}
    def upstream(url, method="GET", body=None, timeout=5):
        captured["messages"] = body["messages"]
        return {"choices": [{"message": {"content": "I can help with visible notices."}, "finish_reason": "stop"}]}
    code, _ = chat_request(monkeypatch, upstream)
    assert code == 200
    assert "greetings" in captured["messages"][0]["content"].lower()


def test_today_question_filters_context(monkeypatch):
    captured = {}
    def upstream(url, method="GET", body=None, timeout=5):
        captured["user"] = body["messages"][1]["content"]
        return {"choices": [{"message": {"content": "The notices don't contain enough information to answer this."}, "finish_reason": "stop"}]}
    monkeypatch.setattr(server, "model_status", lambda: (True, ["gemma-4-e2b-it-qat"], None))
    monkeypatch.setattr(server, "_request", upstream)
    fake = FakePostHandler({"model": "gemma-4-e2b-it-qat", "question": "what should I do today", "notices": [
        {"id": "old", "tag": "Action", "deadline_iso": "2020-01-01", "display_text": "old"},
        {"id": "future", "tag": "Action", "deadline_iso": "2099-01-01", "display_text": "future"},
    ]})
    server.Handler.do_POST(fake)
    assert fake.result[0] == 200
    assert '"id": "old"' not in captured["user"]
    assert '"id": "future"' not in captured["user"]
