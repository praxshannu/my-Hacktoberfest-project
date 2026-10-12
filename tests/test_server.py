import json
from unittest.mock import patch

import build
import web.server as server


def test_build_redacts_passwords_and_emails():
    assert build.redact_display("email me at a.person@example.com; password: hunter2") == "email me at [email hidden]; password: [hidden]"


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
