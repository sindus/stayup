#!/usr/bin/env python3
"""
Stayup — monitors GitHub releases and stores changelogs via stayup-api.

For each tracked repository, the script fetches recent GitHub releases.
If no releases exist, it falls back to cloning the repo and reading a
changelog file. New content is stored only when something has changed
since the last run. Entries older than config["retention_days"] are deleted each run.

Talks to stayup-api over HTTP (STAYUP_API_URL + STAYUP_API_KEY) — it never
touches a database directly. See stayup-api/docs/self-hosting-and-providers.md.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import requests

# Candidate changelog filenames, checked in priority order.
CHANGELOG_NAMES = [
    "CHANGELOG.md",
    "CHANGELOG",
    "CHANGELOG.txt",
    "CHANGELOG.rst",
    "changelog.md",
    "changelog.txt",
    "CHANGES.md",
    "CHANGES",
    "CHANGES.txt",
    "HISTORY.md",
    "HISTORY.txt",
]

PROVIDER_TYPE = "changelog"

# Nom affiché du provider dans les apps (fallback : nom de table capitalisé).
DISPLAY_NAME = "Changelog"

# Où ce connecteur se classe parmi les autres dans la barre latérale.
SORT_ORDER = 10

# Instance stayup-api à laquelle parler, et la clé qui authentifie ce
# connecteur pour le provider 'changelog' — obtenue depuis l'admin de cette
# instance (voir stayup-api/docs/self-hosting-and-providers.md).
API_URL = os.environ.get("STAYUP_API_URL", "http://localhost:3000").rstrip("/")
API_KEY = os.environ.get("STAYUP_API_KEY")

DEFAULT_MAX_ITERATIONS = 5
DEFAULT_RETENTION_DAYS = 15

# Manifeste d'affichage : comment les 3 apps (ui / desktop / mobile) rendent les
# lignes de ce connecteur, sans une ligne de code côté app. stayup-api le relaie
# tel quel depuis provider_registry.template, sans jamais l'interpréter.
# Schéma : voir stayup-api/docs/self-hosting-and-providers.md.
#
# Une entrée = une release. `content` est du texte brut (markdown léger), le
# dépôt vient de la source (repository.url).
DISPLAY_TEMPLATE = {
    "version": 1,
    "display": {
        "name": DISPLAY_NAME,
        # Icône auto-descriptive (tracé SVG teintable, langage visuel des apps :
        # trait 1.75, currentColor). Un tag de release.
        "icon": {
            "paths": [
                "M20.59 13.41 13.42 20.58a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z",
                "M7 7h.01",
            ],
            "viewBox": "0 0 24 24",
            "stroke": True,
        },
        "accent": "#f4b585",
        "sortOrder": SORT_ORDER,
        "feedLabel": {"path": "$source.url", "format": "urlSlug"},
    },
    "item": {
        "parseContentAsJson": False,
        "vars": {"repo": {"path": "$source.url", "format": "urlSlug"}},
        "fields": {
            "title": "{repo}",
            "subtitle": "$row.version",
            "summary": {"path": "content", "format": "stripMarkdown"},
            "url": "https://github.com/{repo}/releases/tag/{$row.version}",
            "timestamp": "$row.datetime",
        },
    },
    "list": {
        "layout": "row",
        "primary": "title",
        "secondary": "subtitle",
        "meta": "timestamp",
        "snippet": "summary",
    },
    "detail": {
        "mode": "text",
        "title": "{repo}",
        "badge": "$row.version",
        "body": {"path": "content", "format": "stripMarkdown"},
        "openUrl": "https://github.com/{repo}/releases/tag/{$row.version}",
        "openLabel": "Open on GitHub",
    },
    "form": {
        "label": "GitHub repo (owner/repo or URL)",
        "placeholder": "vercel/next.js",
        "urlTemplate": "https://github.com/{value}/",
        "transform": {
            "trim": True,
            "extract": r"github\.com/([^/]+/[^/]+)",
            "stripSuffix": [".git", "/"],
        },
    },
}


# ---------------------------------------------------------------------------
# stayup-api client
# ---------------------------------------------------------------------------


def api_request(method: str, path: str, **kwargs) -> dict | None:
    """Call one of stayup-api's /connector-api/changelog/* endpoints.

    Raises RuntimeError if STAYUP_API_KEY isn't set, or requests.HTTPError on
    a non-2xx response (via raise_for_status).
    """
    if not API_KEY:
        raise RuntimeError("STAYUP_API_KEY is not set.")
    url = f"{API_URL}/connector-api/{PROVIDER_TYPE}{path}"
    headers = {"Authorization": f"Bearer {API_KEY}"}
    response = requests.request(method, url, headers=headers, timeout=30, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else None


def register_provider() -> None:
    """Auto-déclaration au démarrage — nom affiché et manifeste d'affichage."""
    api_request(
        "POST",
        "/register",
        json={
            "displayName": DISPLAY_NAME,
            "sortOrder": SORT_ORDER,
            "template": DISPLAY_TEMPLATE,
        },
    )


def add_source(url: str) -> int:
    """Track a new repository URL and return its id."""
    result = api_request("POST", "/sources", json={"url": url})
    return result["id"]


def get_sources() -> list[tuple[int, str, dict]]:
    """Return all tracked sources as (id, url, config) tuples."""
    result = api_request("GET", "/sources")
    return [(s["id"], s["url"], s.get("config") or {}) for s in result["sources"]]


def get_latest_version(repository_id: int) -> str | None:
    """Return the version of the most recently stored entry, or None on first run."""
    result = api_request("GET", f"/sources/{repository_id}/state")
    return result["version"]


def get_saved_versions(repository_id: int) -> set[str]:
    """Return the set of all release versions already saved for a repository."""
    result = api_request("GET", f"/sources/{repository_id}/versions")
    return set(result["versions"])


def save_entry(
    repository_id: int, version: str | None, content: str, changelog_date: datetime | None, executed_at: datetime
) -> None:
    """Persist a single changelog entry."""
    api_request(
        "POST",
        "/items",
        json={
            "items": [
                {
                    "repositoryId": repository_id,
                    "version": version,
                    "content": content,
                    "datetime": changelog_date.isoformat() if changelog_date else None,
                    "executedAt": executed_at.isoformat(),
                    "success": True,
                }
            ]
        },
    )


def cleanup_old_entries(repository_id: int, retention_days: int) -> None:
    """Delete stored entries for a repository older than retention_days days."""
    api_request(
        "DELETE",
        f"/sources/{repository_id}/old-items",
        params={"retentionDays": retention_days},
    )


def save_error(repository_id: int | None, error: str, executed_at: datetime) -> None:
    """Persist a retrieval error."""
    api_request(
        "POST",
        "/errors",
        json={"repositoryId": repository_id, "error": error, "executedAt": executed_at.isoformat()},
    )


# ---------------------------------------------------------------------------
# GitHub API — releases
# ---------------------------------------------------------------------------


def parse_github_owner_repo(url: str) -> tuple[str, str]:
    """Extract (owner, repo) from a GitHub URL.

    Example: "https://github.com/facebook/react" -> ("facebook", "react")
    """
    parts = url.rstrip("/").split("/")
    return parts[-2], parts[-1]


def get_releases(repo_url: str, limit: int = 5) -> list[tuple[str, str, datetime]]:
    """Fetch the most recent GitHub releases for a repository (newest first).

    Returns an empty list if the repository has no releases or does not exist.
    Raises requests.HTTPError for unexpected API errors.
    Uses the GITHUB_TOKEN environment variable when present to increase
    the rate limit from 60 to 5000 requests per hour.
    """
    owner, repo = parse_github_owner_repo(repo_url)
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases"

    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    response = requests.get(api_url, headers=headers, params={"per_page": limit}, timeout=10)

    if response.status_code == 404:
        return []
    response.raise_for_status()

    releases = []
    for data in response.json():
        published_at = datetime.fromisoformat(data["published_at"].replace("Z", "+00:00"))
        releases.append((data["tag_name"], data["body"] or "", published_at))
    return releases


# ---------------------------------------------------------------------------
# Fallback — git clone + changelog file
# ---------------------------------------------------------------------------


def clone_repo(repo_url: str, target_dir: str) -> None:
    """Shallow-clone a repository into target_dir.

    Raises RuntimeError if the clone fails.
    GIT_TERMINAL_PROMPT is disabled to prevent interactive credential prompts.
    """
    result = subprocess.run(
        ["git", "clone", "--depth=1", repo_url, target_dir],
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"Clone failed: {result.stderr.strip()}")


def find_changelog(repo_dir: str) -> str | None:
    """Return the path of the first changelog file found in repo_dir, or None."""
    for name in CHANGELOG_NAMES:
        path = os.path.join(repo_dir, name)
        if os.path.isfile(path):
            return path
    return None


def get_changelog_git_date(repo_dir: str, filename: str) -> datetime | None:
    """Return the committer date of the most recent commit touching filename.

    Returns None if git produces no output (e.g. untracked file).
    """
    result = subprocess.run(
        ["git", "log", "-1", "--format=%cI", "--", filename],
        capture_output=True,
        text=True,
        cwd=repo_dir,
    )
    date_str = result.stdout.strip()
    if not date_str:
        return None
    return datetime.fromisoformat(date_str)


def _content_hash(content: str) -> str:
    """A short, stable dedup key for a file-based changelog, which has no
    natural version of its own (unlike a GitHub release tag). Used as
    `version` so file-based repos get the same dedup path (getLastKnownVersion)
    as everything else, instead of needing the API to hand back raw content.

    Trade-off accepted: the display template shows `$row.version` as the
    subtitle, so a file-based entry's subtitle is this hash rather than blank
    (as it was before). Only affects repos with zero GitHub releases — the
    less common of the two paths."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def get_changelog_from_repo(repo_url: str) -> tuple[str | None, str, datetime | None]:
    """Clone the repository and read the changelog file.

    Returns (version=None, content, changelog_date).
    Raises RuntimeError if no changelog file is found.
    The temporary clone directory is always removed on exit.
    """
    tmp_dir = tempfile.mkdtemp(prefix="stayup_")
    repo_dir = os.path.join(tmp_dir, "repo")
    try:
        clone_repo(repo_url, repo_dir)
        changelog_path = find_changelog(repo_dir)
        if changelog_path is None:
            raise RuntimeError("No release or changelog file found.")
        changelog_name = os.path.basename(changelog_path)
        changelog_date = get_changelog_git_date(repo_dir, changelog_name)
        with open(changelog_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        return None, content, changelog_date
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def process_repository(repository_id: int, repository_url: str, executed_at: datetime, config: dict) -> None:
    """Fetch the latest release(s) (or changelog file) for one repository and persist new entries.

    Release-based repos:
    - If no previous entry exists, the latest release is stored as the initial snapshot.
    - Otherwise, iterates through recent GitHub releases (newest first) and saves every
      release not already known, stopping when a known version is found.
      At most config["max_iterations"] (default 5) new entries are saved per run.

    File-based repos (no releases):
    - Saves the changelog file content whenever it differs from the last saved entry.

    Any exception is caught, logged via the API, and printed to stderr.
    """
    max_iterations = config.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    try:
        releases = get_releases(repository_url, limit=max_iterations)

        if releases:
            saved_versions = get_saved_versions(repository_id)

            if not saved_versions:
                # First run: save only the latest release.
                version, content, changelog_date = releases[0]
                save_entry(repository_id, version, content, changelog_date, executed_at)
            else:
                count = 0
                for version, content, changelog_date in releases:
                    if count >= max_iterations:
                        break
                    if version in saved_versions:
                        break
                    save_entry(repository_id, version, content, changelog_date, executed_at)
                    count += 1
        else:
            _, content, changelog_date = get_changelog_from_repo(repository_url)
            # Un fichier changelog n'a pas de version propre (contrairement à
            # un tag de release) : son hash en tient lieu, pour dédoublonner
            # sur GET /sources/:id/state comme partout ailleurs plutôt que de
            # faire réexposer le contenu précédent par l'API.
            version = _content_hash(content)
            if version != get_latest_version(repository_id):
                save_entry(repository_id, version, content, changelog_date, executed_at)

    except Exception as e:
        save_error(repository_id, str(e), executed_at)
        print(f"[{repository_url}] Error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor GitHub releases and store changelogs.")
    parser.add_argument("--add", metavar="URL", help="Add a repository to track and exit.")
    args = parser.parse_args()

    register_provider()

    if args.add:
        add_source(args.add)
        print(f"Repository added: {args.add}")
        return

    executed_at = datetime.now(tz=timezone.utc)
    sources = get_sources()

    if not sources:
        print("No repositories tracked. Use --add <url> to add one.")
        return

    for repository_id, repository_url, config in sources:
        process_repository(repository_id, repository_url, executed_at, config)
        cleanup_old_entries(repository_id, config.get("retention_days", DEFAULT_RETENTION_DAYS))


if __name__ == "__main__":
    main()
