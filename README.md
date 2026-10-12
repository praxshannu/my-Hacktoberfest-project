# WhatsApp College-Group Organizer (MVP)

A local-only tool that turns cluttered WhatsApp college-group exports into a
single home screen of what matters: deadlines first, then action items,
room schedules, and opportunities. Noise stays hidden by default.

No login, no cloud, no notifications -- just parsing, tagging, and one page.

## How to run

Requirements: Python 3 (standard library only) for the pipeline; any browser
for the page. `pytest` for the tests.

```bash
# 1. Put your WhatsApp exports (*.txt) in data/
#    (Export from WhatsApp: group info -> Export chat -> Without media.)

# 2. Build the JSON the page reads
python3 build.py

# 3. Serve the repo root and open the page
#    The local server also proxies the browser's AI requests to LM Studio.
python3 web/server.py
# then visit http://localhost:8000/web/
```

`output/messages.json` holds one entry per unique notice (repeats collapsed
with a `seen_count`); `output/summary.json` holds tag counts, deadlines
sorted by date, and duplicate groups. Both are git-ignored, like `data/`.

```bash
python3 -m pytest tests/ -q   # parser + categorizer tests
```

## Project layout

| File               | What it does                                                        |
| ------------------ | ------------------------------------------------------------------- |
| `parser.py`        | Parses `data/*.txt` exports (multi-line, forwarded, media, deleted) |
| `rules.json`       | Tag keywords/patterns -- edit this to tune categorization           |
| `categorizer.py`   | Rule-based tagger + date extraction + roll-list redaction            |
| `build.py`         | Runs parser + categorizer, dedupes, writes `output/*.json`          |
| `web/index.html`   | Single static page (vanilla HTML/CSS/JS, no build step)             |
| `tests/`           | pytest suite for parser and categorizer                             |

Tags: `Schedule` (rooms, timings) · `Deadline` (due dates, cutoffs) ·
`Action` (register, fill, submit, bring, do X) · `Opportunity` (contests,
internships, programs) · `Attendance` (absentee lists -- counts only) ·
`Resource` (files, sheets, links with context) · `Noise` (deleted, bare
links, short acks; hidden by default).

## How to edit `rules.json`

- `priority` -- first matching tag wins (Noise checks always run first).
- `tags.<Tag>.keywords` -- case-insensitive substrings.
- `tags.<Tag>.patterns` -- regular expressions (case-insensitive).
- `due_words` -- words that plus a date make a `Deadline`
  (`deadline`, `last date`, `submit by`, ...).
- `noise.ack_phrases` / `max_ack_words` -- what counts as a short
  acknowledgment; `bare_link_max_context_chars` -- how much surrounding
  text saves a bare link from being Noise.

After editing, re-run `python3 build.py` and reload the page.

## Privacy

- Phone-number senders are masked (`+91 ***** 4322`) at parse time; the
  masker skips URLs so links keep working.
- Emails become `[email hidden]`; `password:` / `otp:` values become
  `[hidden]` (labels stay so you still know a credential was shared).
- Roll-number lists are counted and redacted during the build -- `output/`
  and the UI only ever see counts, never the lists.
- `data/` and `output/` are git-ignored and never committed.

## Optional: local LLM second opinion via LM Studio (off by default)

`categorizer.refine_with_llm()` can ask a local LM Studio server
(OpenAI-compatible API) to double-check tags on the unique notices. Rule
tags always stand unless you opt into override. Any failure (server down,
bad reply) keeps the rule tags, and every mismatch is logged to
`output/llm_disagreements.json` for review.

1. In LM Studio, load your model, then start its server from the
   **Developer tab** (default address `http://localhost:1234/v1`).
2. Note the exact model id LM Studio shows (e.g. `qwen2.5-7b-instruct`).
3. Run the build with refining turned on:

```bash
LLM_REFINE=1 LLM_MODEL=<model id> python3 build.py
```

| Variable       | Meaning                                                        |
| -------------- | -------------------------------------------------------------- |
| `LLM_REFINE=1` | Turn the second opinion on (default: off).                     |
| `LLM_MODEL`    | Model id as shown by LM Studio. Required when refining.        |
| `LLM_BASE_URL` | Server base URL. Default `http://localhost:1234/v1`.           |
| `LLM_OVERRIDE=1` | Let the LLM change `tag` (default off: only `tag_llm` is set, `tag_rule` keeps the rule tag). |
