# Contribution Guide

Thank you for contributing! Please follow these guidelines to help keep the project healthy.

## 1. Getting Started

- **Fork the repository** on GitHub and clone your fork locally.
- **Create a branch** for each change: `git checkout -b feature/short-description` or `git checkout -b fix/short-description`.
- **Run the tests** locally before committing: `python3 -m pytest tests/ -q`.
- **Build the project** to verify your changes: `python3 build.py`.

## 2. Code Quality & Formatting

- Follow the existing code style (PEP 8 for Python, consistent indentation).
- Keep functions focused and well-documented with docstrings.
- Use meaningful variable and function names.
- Add type hints where appropriate.
- Run any available linters (`ruff` or `pylint`) to check for style issues.
- Ensure new code has corresponding tests in `tests/`.
- Commit messages should be concise and describe the change: `git commit -m "fix: handle edge case in parser"`.
- Avoid committing `output/` or `data/` directories (they are git-ignored).

## 3. Issue Management

- Check existing issues before creating a new one — look for duplicates.
- Use descriptive labels: `good first issue`, `help wanted`, `bug`, `enhancement`.
- Include a clear title and minimal reproduction steps for bug reports.
- For new features, describe the problem being solved and the proposed solution.
- Tag maintainers (`@username`) if you need clarification or review.

## 4. Pull Request Process

- Open a Pull Request against the `main` branch of the upstream repository.
- Fill the PR template (if one exists) describing:
  - What you changed and why.
  - How you tested your changes.
  - Any relevant issue numbers.
- Keep PRs focused — one change per PR.
- Address reviewer feedback promptly.
- Once approved and CI passes, a maintainer will merge your PR.

## 5. Project Layout

| File               | What it does                                                        |
| ------------------ | ------------------------------------------------------------------- |
| `parser.py`        | Parses `data/*.txt` exports (multi-line, forwarded, media, deleted) |
| `rules.json`       | Tag keywords/patterns -- edit this to tune categorization           |
| `categorizer.py`   | Rule-based tagger + date extraction + roll-list redaction            |
| `build.py`         | Runs parser + categorizer, dedupes, writes `output/*.json`          |
| `web/index.html`   | Single static page (vanilla HTML/CSS/JS, no build step)             |
| `tests/`           | pytest suite for parser and categorizer                             |

---

**Need help?** Open an issue or reach out — we're happy to help newcomers get started!