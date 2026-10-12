"""Parser tests: multi-line, forwarded, deleted, media, masking.

Senders below are already masked (or plain names) -- raw phone numbers
never appear in this repo outside data/, which is git-ignored.
"""

import re

from parser import (
    _split_raw_messages,
    mask_phones_in_text,
    mask_secrets,
    mask_sender,
    parse_file,
)

SAMPLE = """Messages and calls are end-to-end encrypted.
[6/15/26, 1:17:24 PM] +91 ***** 0717: [Forwarded] This is the link to register for ai agents course and  day 1 assignments started today.

https://www.kaggle.com/competitions/5-day-ai-agents-intensive-vibecoding-course-with-google
[6/15/26, 1:17:25 PM] SAI SAMPATH: Come to 3rd floor
1st room
[6/17/26, 1:29:16 PM] +91 ***** 2551: This message was deleted
[6/17/26, 8:54:38 AM] +91 ***** 1207: [Forwarded] <document omitted> CSE-AI_MidMarksFinal.pdf
[6/22/26, 10:10:44 AM] +91 ***** 1271: This message was deleted by admin SAI SAMPATH
[6/17/26, 7:57:57 AM] - [System notification]
[6/24/26, 6:11:00 AM] Laveti Yashasri: [Forwarded] <image omitted>
"""


def _parse(tmp_path, text=SAMPLE, group="g1"):
    path = tmp_path / "chat.txt"
    path.write_text(text, encoding="utf-8")
    return parse_file(str(path), group=group)


def test_preamble_skipped_and_count(tmp_path):
    msgs = _parse(tmp_path)
    assert len(msgs) == 7  # encryption preamble is not a message


def test_multiline_message_kept_together(tmp_path):
    msgs = _parse(tmp_path)
    room = [m for m in msgs if "3rd floor" in m["text"]][0]
    assert "1st room" in room["text"]
    assert room["sender_masked"] == "SAI SAMPATH"
    assert room["timestamp"] == "2026-06-15T13:17:25"


def test_multiline_with_blank_line_and_link(tmp_path):
    msgs = _parse(tmp_path)
    kaggle = [m for m in msgs if "kaggle" in m["text"]][0]
    assert "https://www.kaggle.com/competitions" in kaggle["text"]
    assert "\n" in kaggle["text"]  # blank continuation line preserved


def test_forwarded_flag_and_prefix_stripped(tmp_path):
    msgs = _parse(tmp_path)
    kaggle = [m for m in msgs if "kaggle" in m["text"]][0]
    assert kaggle["is_forwarded"] is True
    assert not kaggle["text"].lstrip().startswith("[Forwarded]")
    room = [m for m in msgs if "3rd floor" in m["text"]][0]
    assert room["is_forwarded"] is False


def test_deleted_variants(tmp_path):
    msgs = _parse(tmp_path)
    deleted = [m for m in msgs if m["is_deleted"]]
    assert len(deleted) == 2
    assert any("by admin" in m["text"] for m in deleted)


def test_media_placeholders(tmp_path):
    msgs = _parse(tmp_path)
    doc = [m for m in msgs if "MidMarksFinal" in m["text"]][0]
    img = [m for m in msgs if "<image omitted>" in m["text"]][0]
    assert doc["is_media"] is True
    assert img["is_media"] is True
    assert img["is_forwarded"] is True


def test_system_line_without_colon_is_own_message(tmp_path):
    msgs = _parse(tmp_path)
    system = [m for m in msgs if m["sender_masked"] == "System"]
    assert len(system) == 1
    assert "System notification" in system[0]["text"]
    # ...and it did not glue onto the previous message.
    doc = [m for m in msgs if "MidMarksFinal" in m["text"]][0]
    assert "System notification" not in doc["text"]


def test_sender_masking():
    assert mask_sender("+91 98853 34322") == "+91 ***** 4322"
    assert mask_sender("+918341241254") == "+91 ***** 1254"
    assert mask_sender("SAI SAMPATH") == "SAI SAMPATH"
    assert mask_sender("Laveti Yashasri") == "Laveti Yashasri"
    assert mask_sender("-") == "System"
    assert mask_sender("You") == "You"


def test_group_field(tmp_path):
    assert _parse(tmp_path, group="group2")[0]["group"] == "group2"


def test_no_raw_phone_numbers_leak(tmp_path):
    raw = "[6/15/26, 1:17:24 PM] +91 98853 34322: call me on 98853 34322 ok\n"
    path = tmp_path / "chat.txt"
    path.write_text(raw, encoding="utf-8")
    (msg,) = parse_file(str(path), group="g")
    assert "98853" not in msg["sender_masked"]
    assert "98853" not in msg["text"]
    assert re.search(r"\d{5}\s?\d{5}", msg["sender_masked"] + msg["text"]) is None


def test_url_query_keys_not_masked():
    text = "https://example.com/pages/event?key=627157394627"
    assert mask_phones_in_text(text) == text


def test_secrets_masked():
    assert mask_secrets("email : somebody@example.edu and password : S3cret!") == \
        "email : [email hidden] and password: [hidden]"
    assert mask_secrets("OTP : 482913, do not share") == "OTP: [hidden], do not share"
    # Labels without values pass through untouched.
    assert mask_secrets("reset their passwords within 24 hours") == \
        "reset their passwords within 24 hours"


def test_split_handles_both_year_widths():
    chunks = _split_raw_messages(
        "[6/15/26, 1:17:24 PM] A: hi\n[6/15/2026, 1:17:24 PM] B: yo\ntail"
    )
    assert len(chunks) == 2
    assert chunks[1][2] == ["yo", "tail"]
