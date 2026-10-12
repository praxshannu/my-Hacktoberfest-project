"""Categorizer tests on real example lines from the chat exports.

Texts are verbatim excerpts (truncated with [...] where the original
contains roll-number lists, which are never reproduced here). Senders are
not included -- only the message text matters for tagging.
"""

from categorizer import (
    categorize,
    extract_date,
    load_rules,
    redact_rolls,
    refine_with_llm,
    tag_message,
)

RULES = load_rules()
REF = "2026-07-08T10:00:00"  # a Wednesday morning, anchors relative dates


def _msg(text, **flags):
    base = {
        "timestamp": REF,
        "sender_masked": "+91 ***** 0000",
        "text": text,
        "is_forwarded": False,
        "is_media": False,
        "is_deleted": False,
        "group": "g",
    }
    base.update(flags)
    return base


# -- 16 real example lines: (text, expected_tag) ----------------------------

REAL_EXAMPLES = [
    # Action: must register / fill / submit / do X
    ("This is the link to register for ai agents course and  day 1 "
     "assignments started today.\n\nhttps://www.kaggle.com/competitions/5-day-ai-agents-intensive-vibecoding-course-with-google",
     "Action"),
    ("If Anyone has objection please contact concerned faculty today itself",
     "Action"),
    ("Andaru feedback form fill cheyandi ecap lo absentees kuda",
     "Action"),
    # Deadline: due word + date
    ("Submit by 3rd July and on 4th July ,will have review .timings will be announced soon",
     "Deadline"),
    ("Dear Students, The deadline for submitting the Week 9 Assignment is "
     "Wednesday, 23/09/2026, at 11:59 PM (23:59 IST). Kindly ensure that you "
     "submit the assignment before the deadline.",
     "Deadline"),
    ("Today is Last day for nptel week-5 assignment.",
     "Deadline"),
    # Schedule: rooms and timings
    ("5-8-2026(Wednesday) Class room details: II year A sec: SA14(upto 5th "
     "hour , after that sit in SA43)",
     "Schedule"),
    ("Come to 3rd floor 1st room",
     "Schedule"),
    ("Go to SA16",
     "Schedule"),
    ("The weekly test scheduled for Wednesday has been postponed to Saturday "
     "due to traffic restrictions on some routes.",
     "Schedule"),
    # Opportunity: contests and programs
    ("Join Today's CodeChef Contest at 8:00PM https://www.codechef.com/START250D",
     "Opportunity"),
    # Attendance: absentee list (truncated -- full roll list never stored here)
    ("Today's CRT ABSENTEES- for 1st & 2nd hours 5,17,18,26,29,31 [...]",
     "Attendance"),
    # Resource: file with a real filename
    ("<document omitted> CSE-AI_MidMarksFinal.pdf",
     "Resource"),
    # Noise: deleted / bare link / short ack
    ("This message was deleted",
     "Noise"),
    ("Ok",
     "Noise"),
    ("https://forms.gle/ZCWv7hrMPsv9hLbm6",
     "Noise"),
]


def test_real_examples_get_expected_tags():
    for text, expected in REAL_EXAMPLES:
        flags = {}
        if text == "This message was deleted":
            flags["is_deleted"] = True
        if text.startswith("<document omitted>"):
            flags["is_media"] = True
            flags["is_forwarded"] = True
        tag, _, _ = categorize(_msg(text, **flags), RULES)
        assert tag == expected, "got %s for %r (want %s)" % (tag, text[:60], expected)


def test_deadline_dates_extracted():
    _, found, iso = categorize(
        _msg("Submit by 3rd July and on 4th July ,will have review"), RULES)
    assert found and "3rd July" in found
    assert iso == "2026-07-03"

    _, found, iso = categorize(
        _msg("The deadline for submitting the Week 9 Assignment is Wednesday, "
             "23/09/2026, at 11:59 PM.", timestamp="2026-09-01T10:00:00"), RULES)
    assert iso == "2026-09-23"


def test_relative_and_telugu_dates():
    assert extract_date("submit it by tomorrow", REF)[1] == "2026-07-09"
    assert extract_date("repu submit cheyandi", REF)[1] == "2026-07-09"  # repu = tomorrow
    assert extract_date("exam today at 2 PM", REF) == ("today 2 PM", "2026-07-08")
    # "before 10 minutes" is not a cutoff date
    assert extract_date("buses will start before 10 minutes", REF) == (None, None)


def test_attendance_redacts_lists_to_counts():
    enriched = tag_message(
        _msg("Today's CRT ABSENTEES- for 1st & 2nd hours 5,17,18,26,29,31 [...]"), RULES)
    assert enriched["tag"] == "Attendance"
    assert enriched["roll_count"] >= 6
    for token in ("17", "26", "31"):
        assert token not in enriched["display_text"].replace("hidden", "")


def test_roll_ids_redacted_everywhere():
    display, count = redact_rolls("Team : 09 24L31A4371 24L31A4376 meet at SA14")
    assert count == 2
    assert "24L31A4371" not in display
    assert "SA14" in display  # room info stays


def test_llm_hook_disabled_by_default():
    messages = [tag_message(_msg("Go to SA16"), RULES)]
    assert refine_with_llm(messages) == messages
    assert "tag_llm" not in messages[0]
