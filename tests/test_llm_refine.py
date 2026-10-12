"""Tests for the optional LM Studio refine hook (categorizer.refine_with_llm).

The hook is disabled unless LLM_REFINE=1, and all HTTP is mocked -- no
server or model is needed to run these.
"""

import json

import pytest

from categorizer import last_llm_run, parse_llm_tag, refine_with_llm


def _message(**overrides):
    base = {"id": "m0001", "tag": "Action", "text": "do the thing",
            "preview": "do the thing"}
    base.update(overrides)
    return base


class _FakeResponse:
    """Minimal stand-in for urllib's HTTP response (context manager)."""

    def __init__(self, content):
        self._body = json.dumps(
            {"choices": [{"message": {"content": content}}]}).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _mock_urlopen(monkeypatch, content, seen=None):
    def fake_urlopen(request, timeout=None):
        if seen is not None:
            seen.append({"url": request.full_url,
                         "body": json.loads(request.data.decode("utf-8")),
                         "timeout": timeout})
        return _FakeResponse(content)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)


def _clear_env(monkeypatch):
    for var in ("LLM_REFINE", "LLM_MODEL", "LLM_BASE_URL", "LLM_OVERRIDE"):
        monkeypatch.delenv(var, raising=False)


def test_refine_is_noop_when_env_unset(monkeypatch):
    _clear_env(monkeypatch)

    def boom(*args, **kwargs):
        raise AssertionError("network must not be touched when disabled")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    messages = [_message()]
    assert refine_with_llm(messages) == messages
    assert "tag_llm" not in messages[0]
    assert last_llm_run["enabled"] is False


def test_mocked_reply_maps_to_canonical_tag(monkeypatch):
    monkeypatch.setenv("LLM_REFINE", "1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    seen = []
    _mock_urlopen(monkeypatch, "  deadline. ", seen)

    messages = [_message()]
    refine_with_llm(messages)

    assert messages[0]["tag_llm"] == "Deadline"
    assert messages[0]["tag_rule"] == "Action"
    # Default off: the rule tag stands, only tag_llm is recorded.
    assert messages[0]["tag"] == "Action"
    assert len(last_llm_run["disagreements"]) == 1

    # Request contract: OpenAI-compatible chat/completions endpoint.
    assert seen[0]["url"] == "http://localhost:1234/v1/chat/completions"
    assert seen[0]["body"]["model"] == "test-model"
    assert seen[0]["body"]["temperature"] == 0
    assert seen[0]["body"]["max_tokens"] == 10
    assert seen[0]["timeout"] == 30


def test_think_block_and_case_are_normalized():
    assert parse_llm_tag("<think>looks like a cutoff</think>\n  deadline. ") == "Deadline"
    assert parse_llm_tag("SCHEDULE") == "Schedule"
    assert parse_llm_tag("not a tag at all") is None
    assert parse_llm_tag("") is None


def test_missing_model_errors_clearly(monkeypatch):
    monkeypatch.setenv("LLM_REFINE", "1")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(ValueError, match="LLM_MODEL is required"):
        refine_with_llm([_message()])


def test_override_changes_tag_but_keeps_rule(monkeypatch):
    monkeypatch.setenv("LLM_REFINE", "1")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_OVERRIDE", "1")
    _mock_urlopen(monkeypatch, "Noise")

    messages = [_message()]
    refine_with_llm(messages)

    assert messages[0]["tag"] == "Noise"
    assert messages[0]["tag_rule"] == "Action"
    assert messages[0]["tag_llm"] == "Noise"


def test_server_failure_keeps_rule_tags(monkeypatch):
    monkeypatch.setenv("LLM_REFINE", "1")
    monkeypatch.setenv("LLM_MODEL", "test-model")

    def down(*args, **kwargs):
        raise ConnectionRefusedError("no server here")

    monkeypatch.setattr("urllib.request.urlopen", down)
    messages = [_message()]
    assert refine_with_llm(messages) == messages
    assert "tag_llm" not in messages[0]
    assert last_llm_run["checked"] == 0
    assert last_llm_run["error"] is not None
