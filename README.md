# Stayup

[![CI](https://github.com/stayup-app/stayup-cmd-changelog/actions/workflows/ci.yml/badge.svg)](https://github.com/stayup-app/stayup-cmd-changelog/actions/workflows/ci.yml)
[![Daily changelog check](https://github.com/stayup-app/stayup-cmd-changelog/actions/workflows/daily.yml/badge.svg)](https://github.com/stayup-app/stayup-cmd-changelog/actions/workflows/daily.yml)

**Website:** https://stayup-ui.vercel.app

Monitors GitHub releases and stores changelogs via [stayup-api](https://github.com/stayup-app/stayup-api) — this script never touches a database directly, it only calls `stayup-api`'s `/connector-api/changelog/*` endpoints.

For each tracked repository, the script fetches the latest GitHub release(s). If no release exists, it falls back to reading a changelog file from the repository. A new entry is only stored when something has changed since the last run.

## Requirements

- Python 3.13, or [Docker](https://www.docker.com/)
- A `stayup-api` instance (the public one, or your own — see [self-hosting-and-providers.md](https://github.com/stayup-app/stayup-api/blob/main/docs/self-hosting-and-providers.md))
- An API key for the `changelog` provider, created from that instance's admin panel (Connector keys → New key, provider `changelog`). The key is shown once — copy it right away.

## Setup

```bash
git clone https://github.com/stayup-app/stayup-cmd-changelog.git
cd stayup-cmd-changelog
cp .env.example .env
```

Open `.env` and set `STAYUP_API_URL` (your `stayup-api` instance) and `STAYUP_API_KEY` (the key you created for `changelog`). Optionally set `GITHUB_TOKEN` to raise the GitHub API rate limit from 60 to 5000 requests/hour.

> **Note:** the provider registers itself automatically on every run — nothing to create by hand beyond the key.

## Usage

**Track a repository:**
```bash
docker compose run --rm check_changelog --add https://github.com/facebook/react
docker compose run --rm check_changelog --add https://github.com/vercel/next.js
```

**Run the script manually:**
```bash
docker compose run --rm check_changelog
```

Without Docker:
```bash
pip install -r requirements.txt
STAYUP_API_URL=... STAYUP_API_KEY=... python check_changelog.py
```

## Automation

The script runs automatically every night at midnight UTC via GitHub Actions.

To enable it on your fork, add `STAYUP_API_URL` and `STAYUP_API_KEY` secrets in:
**Settings → Secrets and variables → Actions → New repository secret**

You can also trigger the workflow manually from the **Actions → Daily changelog check → Run workflow** tab.

Optionally, add a `GITHUB_TOKEN` secret to raise the GitHub API rate limit from 60 to 5000 requests/hour.

## Development

**Install the pre-commit hook** (runs linter + tests before every commit):
```bash
cp scripts/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
```

**Run tests** (no external dependencies — `stayup-api`, git and the GitHub API are mocked):
```bash
docker compose run --rm test
```

**Check linting:**
```bash
docker compose run --rm --entrypoint="" test sh -c "ruff check . && black --check ."
```

**Auto-format code:**
```bash
docker run --rm --entrypoint="" -v $(pwd):/app -w /app stayup-test black .
```

## What gets stored

Each stored entry's `content` is the full release body (or changelog file content) as plain text, keyed by release tag (`version` — e.g. `v1.2.0`). A file-based changelog (no GitHub release) has no natural version, so a short hash of its content is used instead, purely for dedup — see `stayup-api`'s `connector-api` docs for the full contract.

## Project structure

```
stayup-cmd-changelog/
├── check_changelog.py  # Main script
├── tests/
│   └── test_unit.py    # Tests — stayup-api, git and the GitHub API are mocked
├── .env.example         # Configuration template
├── docker-compose.yml
├── Dockerfile
└── pyproject.toml      # Ruff + Black configuration
```
