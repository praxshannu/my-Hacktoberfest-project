# Stuwat_clean WhatsApp College-Group Organizer

Turn WhatsApp college-group exports into a focused local dashboard for deadlines, actions, schedules, and opportunities.

<img width="1710" height="1112" alt="image" src="https://github.com/user-attachments/assets/9efe44e0-c930-469e-b141-e9fb37584129" />


Everything runs on your computer. The project uses Python’s standard library for parsing and building the dashboard data, so there is no cloud account or database to configure.

## Requirements

- Python 3.9 or newer
- A modern web browser
- `pytest` only if you want to run the test suite
- Optional: [LM Studio](https://lmstudio.ai/) for local AI summaries and task refinement

No Python packages are required to build or run the dashboard itself.

## 1. Add WhatsApp exports

Create a `data/` directory in the project root, then export one or more WhatsApp chats as plain-text files:

1. Open the WhatsApp group.
2. Open the group information screen.
3. Choose **Export chat**.
4. Select **Without media**.
5. Save the resulting `.txt` files inside `data/`.

The expected layout is:

```text
my-hacktober-project/
├── data/
│   ├── class-group.txt
│   └── department-group.txt
└── build.py
```

Keep the exports local. They can contain personal messages and contact details.

## 2. Build the project

From the project root, run:

```bash
python3 build.py
```

The build reads every `.txt` file in `data/` and creates the JSON files consumed by the dashboard in `output/`:

- `output/messages.json` — cleaned and deduplicated notices
- `output/summary.json` — tag counts, deadlines, and duplicate groups

To use different directories:

```bash
python3 build.py --data path/to/exports --out path/to/output
```

Run the build again whenever you add or change an export.

## 3. Start the local web app

After building the data, start the local server:

```bash
python3 web/server.py
```

The server listens on port `8000` by default. To use another available port:

```bash
PORT=8080 python3 web/server.py
```

Keep this terminal window open while using the dashboard. Stop the server with `Ctrl+C`.

## 4. Open the dashboard

Open this address in your browser:

```text
http://localhost:8000/web/
```

If you changed the port, replace `8000` with your selected port. If the page says that notice data is missing, run `python3 build.py` first.

## Optional: enable LM Studio

The dashboard works without LM Studio. Connect it only if you want local AI summaries, chat, or optional second-opinion tagging.

1. Install and open LM Studio.
2. Load an instruction-following model.
3. Start the local server from LM Studio’s **Developer** tab.
4. Note the model ID shown by LM Studio.
5. Start the organizer as usual:

```bash
python3 web/server.py
```

To enable optional LLM refinement during the build, run:

```bash
LLM_REFINE=1 LLM_MODEL="your-model-id" python3 build.py
```

The default LM Studio endpoint is `http://localhost:1234/v1`. To use another local endpoint:

```bash
LLM_BASE_URL="http://127.0.0.1:1234/v1" \
LLM_REFINE=1 \
LLM_MODEL="your-model-id" \
python3 build.py
```

LLM refinement is off by default. If LM Studio is unavailable, the rule-based build remains usable.

## Run the tests

Install `pytest` if needed, then run:

```bash
python3 -m pytest tests/ -q
```

The tests cover parsing, categorization, the build pipeline, and local server behavior.

## Common troubleshooting

### `python3: command not found`

Install Python 3, then confirm it is available:

```bash
python3 --version
```

### `No such file or directory: data`

Create the input directory and place at least one WhatsApp `.txt` export inside it:

```bash
mkdir -p data
```

### The browser says notice data is missing

Build the data before starting or refreshing the server:

```bash
python3 build.py
```

### Port `8000` is already in use

Start the server on another port and open the matching browser address:

```bash
PORT=8080 python3 web/server.py
```

Then visit `http://localhost:8080/web/`.

### LM Studio is unavailable

Make sure a model is loaded and the local server is running. You can also leave LLM refinement disabled; the organizer’s standard parsing and categorization do not require LM Studio.

### The dashboard looks out of date

Rebuild the JSON data, then refresh the browser:

```bash
python3 build.py
```
