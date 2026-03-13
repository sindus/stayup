# Stayup

Monitors GitHub releases and stores changelogs in a PostgreSQL database.

For each tracked repository, the script fetches the latest GitHub release. If no release exists, it falls back to reading a changelog file from the repository. A new entry is only stored when something has changed since the last run. The three most recent entries per repository are kept.

## Requirements

- [Docker](https://www.docker.com/) and Docker Compose

## Setup

```bash
git clone https://github.com/sindus/stayup.git
cd stayup
cp .env.example .env
```

Open `.env` and configure your database connection.

### Option A — Local database (Docker)

The default values in `.env` work out of the box with the bundled `db` service. No changes needed.

### Option B — External database (Render, Railway, etc.)

Set the full connection URL in `.env`:

```env
DATABASE_URL=postgresql://user:password@host:5432/dbname
```

> **Note:** Tables are created automatically on the first run.

## Usage

**Start the database:**
```bash
docker compose up db -d
```

**Track a repository:**
```bash
docker compose run --rm check_changelog --add https://github.com/facebook/react
docker compose run --rm check_changelog --add https://github.com/vercel/next.js
```

**Run the script manually:**
```bash
docker compose run --rm check_changelog
```

**Browse the database (pgAdmin):**
```bash
docker compose up pgadmin -d
```
Open [http://localhost:5050](http://localhost:5050) — credentials: `admin@admin.com` / `admin`

Connect to the server using host `db`, port `5432`, and the credentials from your `.env`.

## Automation

The script runs automatically every night at midnight UTC via GitHub Actions.

To enable it on your fork, add a `DATABASE_URL` secret in:
**Settings → Secrets and variables → Actions → New repository secret**

You can also trigger the workflow manually from the **Actions → Daily changelog check → Run workflow** tab.

Optionally, add a `GITHUB_TOKEN` secret to raise the GitHub API rate limit from 60 to 5000 requests/hour.

## Development

**Install the pre-commit hook** (runs linter + tests before every commit):
```bash
cp scripts/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
```


**Run tests:**
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

## Database schema

| Table | Description |
|---|---|
| `repository` | Tracked repositories |
| `connector_changelog` | Stored releases/changelogs (last 3 per repository) |
| `log` | Errors encountered during retrieval |

### `connector_changelog` columns

| Column | Description |
|---|---|
| `version` | Release tag (e.g. `v1.2.0`), null for file-based changelogs |
| `content` | Full release body or changelog file content |
| `diff` | Unified diff against the previous entry, null on first run |
| `datetime` | Publication date from GitHub or last git commit date |
| `executed_at` | Timestamp when the script ran |
| `success` | Always `true` — errors are stored in the `log` table |

## Project structure

```
stayup/
├── check_changelog.py      # Main script
├── tests/
│   ├── test_unit.py        # Unit tests (no external dependencies)
│   └── test_functional.py  # Functional tests (require PostgreSQL)
├── .env.example            # Configuration template
├── docker-compose.yml
├── Dockerfile
└── pyproject.toml          # Ruff + Black configuration
```
