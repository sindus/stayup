"""
Functional tests — require a running PostgreSQL instance.

Connection is configured via environment variables:
  DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
"""

import os
import subprocess
from datetime import datetime, timezone
from unittest.mock import patch

import psycopg2
import pytest

from check_changelog import (
    cleanup_old_changelogs,
    clone_repo,
    init_db,
    process_repo,
    save_changelog,
    save_error,
    upsert_repository,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_conn():
    try:
        return psycopg2.connect(
            host=os.environ.get("DB_HOST", "localhost"),
            port=int(os.environ.get("DB_PORT", 5432)),
            dbname=os.environ.get("DB_NAME", "stayup"),
            user=os.environ.get("DB_USER", "stayup"),
            password=os.environ.get("DB_PASSWORD", "stayup"),
        )
    except psycopg2.OperationalError as e:
        pytest.skip(f"PostgreSQL unavailable: {e}")


@pytest.fixture(scope="session", autouse=True)
def setup_db():
    """Create tables once for the whole test session."""
    conn = make_conn()
    init_db(conn)
    conn.close()


@pytest.fixture
def db_conn():
    """Fresh connection per test to guarantee isolation."""
    conn = make_conn()
    yield conn
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("TRUNCATE connector_changelog, log, repository RESTART IDENTITY CASCADE")
    conn.commit()
    conn.close()


@pytest.fixture
def local_git_repo(tmp_path):
    """Local git repository with a committed CHANGELOG.md."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, ["git", "init"])
    _git(repo, ["git", "config", "user.email", "test@example.com"])
    _git(repo, ["git", "config", "user.name", "Test"])
    (repo / "CHANGELOG.md").write_text("## 1.0.0\n- Initial release\n")
    _git(repo, ["git", "add", "."])
    _git(repo, ["git", "commit", "-m", "init"])
    return repo


@pytest.fixture
def local_git_repo_no_changelog(tmp_path):
    """Local git repository with no changelog file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, ["git", "init"])
    _git(repo, ["git", "config", "user.email", "test@example.com"])
    _git(repo, ["git", "config", "user.name", "Test"])
    (repo / "README.md").write_text("no changelog here")
    _git(repo, ["git", "add", "."])
    _git(repo, ["git", "commit", "-m", "init"])
    return repo


def _git(cwd, cmd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


# ---------------------------------------------------------------------------
# repository
# ---------------------------------------------------------------------------


class TestUpsertRepositoryFunctional:
    def test_creates_new_repository(self, db_conn):
        repo_id = upsert_repository(db_conn, "https://github.com/user/repo")
        assert isinstance(repo_id, int)
        with db_conn.cursor() as cur:
            cur.execute("SELECT url FROM repository WHERE id = %s", (repo_id,))
            row = cur.fetchone()
        assert row[0] == "https://github.com/user/repo"

    def test_returns_same_id_on_duplicate(self, db_conn):
        id1 = upsert_repository(db_conn, "https://github.com/user/repo")
        id2 = upsert_repository(db_conn, "https://github.com/user/repo")
        assert id1 == id2

    def test_different_urls_get_different_ids(self, db_conn):
        id1 = upsert_repository(db_conn, "https://github.com/user/repo1")
        id2 = upsert_repository(db_conn, "https://github.com/user/repo2")
        assert id1 != id2


# ---------------------------------------------------------------------------
# changelog
# ---------------------------------------------------------------------------


class TestSaveChangelogFunctional:
    def test_row_is_persisted_with_version(self, db_conn):
        repo_id = upsert_repository(db_conn, "https://github.com/user/repo")
        executed_at = datetime.now(tz=timezone.utc)
        save_changelog(db_conn, repo_id, "v1.0.0", "release notes", None, None, executed_at)

        with db_conn.cursor() as cur:
            cur.execute("SELECT version, content, success FROM connector_changelog WHERE provider_id = %s", (repo_id,))
            row = cur.fetchone()
        assert row[0] == "v1.0.0"
        assert row[1] == "release notes"
        assert row[2] is True

    def test_row_is_persisted_without_version(self, db_conn):
        repo_id = upsert_repository(db_conn, "https://github.com/user/repo")
        save_changelog(db_conn, repo_id, None, "## 1.0.0\n- init", None, None, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT version, content FROM connector_changelog WHERE provider_id = %s", (repo_id,))
            row = cur.fetchone()
        assert row[0] is None
        assert "1.0.0" in row[1]

    def test_changelog_date_stored(self, db_conn):
        repo_id = upsert_repository(db_conn, "https://github.com/user/repo")
        changelog_date = datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        save_changelog(db_conn, repo_id, None, "content", None, changelog_date, datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT datetime FROM connector_changelog WHERE provider_id = %s", (repo_id,))
            row = cur.fetchone()
        assert row[0].replace(tzinfo=timezone.utc) == changelog_date


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


class TestCleanupOldChangelogsFunctional:
    def test_keeps_only_last_3(self, db_conn):
        repo_id = upsert_repository(db_conn, "https://github.com/user/repo")
        executed_at = datetime.now(tz=timezone.utc)
        for i in range(5):
            save_changelog(db_conn, repo_id, f"v1.{i}.0", f"content {i}", None, None, executed_at)

        cleanup_old_changelogs(db_conn, repo_id)

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_changelog WHERE provider_id = %s", (repo_id,))
            count = cur.fetchone()[0]
        assert count == 3

    def test_does_nothing_when_less_than_3(self, db_conn):
        repo_id = upsert_repository(db_conn, "https://github.com/user/repo")
        executed_at = datetime.now(tz=timezone.utc)
        save_changelog(db_conn, repo_id, "v1.0.0", "content", None, None, executed_at)

        cleanup_old_changelogs(db_conn, repo_id)

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_changelog WHERE provider_id = %s", (repo_id,))
            count = cur.fetchone()[0]
        assert count == 1


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------


class TestSaveErrorFunctional:
    def test_error_is_persisted(self, db_conn):
        repo_id = upsert_repository(db_conn, "https://github.com/user/repo")
        executed_at = datetime.now(tz=timezone.utc)
        save_error(db_conn, repo_id, "No changelog found.", executed_at)

        with db_conn.cursor() as cur:
            cur.execute("SELECT error, repository_id FROM log WHERE repository_id = %s", (repo_id,))
            row = cur.fetchone()
        assert row[0] == "No changelog found."
        assert row[1] == repo_id

    def test_error_without_repository(self, db_conn):
        save_error(db_conn, None, "Git connection error", datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT error FROM log WHERE repository_id IS NULL")
            row = cur.fetchone()
        assert row[0] == "Git connection error"


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


class TestEndToEnd:
    @patch("check_changelog.get_latest_release")
    def test_process_repo_first_run_via_release(self, mock_release, db_conn):
        """First run via release API — stores content with no diff."""
        mock_release.return_value = ("v1.0.0", "- Initial release", datetime.now(tz=timezone.utc))
        repo_id = upsert_repository(db_conn, "https://github.com/user/repo")
        process_repo(db_conn, repo_id, "https://github.com/user/repo", datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT version, diff, success FROM connector_changelog WHERE provider_id = %s", (repo_id,))
            row = cur.fetchone()
        assert row[0] == "v1.0.0"
        assert row[1] is None  # no diff on first run
        assert row[2] is True

    @patch("check_changelog.get_latest_release")
    def test_process_repo_saves_diff_on_new_release(self, mock_release, db_conn):
        """New tag — stores a new entry."""
        repo_id = upsert_repository(db_conn, "https://github.com/user/repo")
        executed_at = datetime.now(tz=timezone.utc)

        mock_release.return_value = ("v1.0.0", "- Initial", executed_at)
        process_repo(db_conn, repo_id, "https://github.com/user/repo", executed_at)

        mock_release.return_value = ("v1.1.0", "- New feature", datetime.now(tz=timezone.utc))
        process_repo(db_conn, repo_id, "https://github.com/user/repo", datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute(
                "SELECT version FROM connector_changelog WHERE provider_id = %s ORDER BY executed_at DESC LIMIT 1",
                (repo_id,),
            )
            row = cur.fetchone()
        assert row[0] == "v1.1.0"

    @patch("check_changelog.get_latest_release")
    def test_process_repo_no_insert_when_same_release(self, mock_release, db_conn):
        """Same tag — no new entry is inserted."""
        repo_id = upsert_repository(db_conn, "https://github.com/user/repo")
        executed_at = datetime.now(tz=timezone.utc)

        mock_release.return_value = ("v1.0.0", "- Initial", executed_at)
        process_repo(db_conn, repo_id, "https://github.com/user/repo", executed_at)
        process_repo(db_conn, repo_id, "https://github.com/user/repo", datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM connector_changelog WHERE provider_id = %s", (repo_id,))
            count = cur.fetchone()[0]
        assert count == 1

    @patch("check_changelog.get_latest_release")
    def test_process_repo_fallback_to_file(self, mock_release, db_conn, local_git_repo):
        """No release — falls back to the changelog file."""
        mock_release.return_value = None
        repo_id = upsert_repository(db_conn, str(local_git_repo))
        process_repo(db_conn, repo_id, str(local_git_repo), datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT content, version FROM connector_changelog WHERE provider_id = %s", (repo_id,))
            row = cur.fetchone()
        assert "1.0.0" in row[0]
        assert row[1] is None  # no version for file-based changelogs

    @patch("check_changelog.get_latest_release")
    def test_process_repo_logs_error_on_failure(self, mock_release, db_conn):
        """API error — logged to the log table."""
        mock_release.side_effect = Exception("API timeout")
        repo_id = upsert_repository(db_conn, "https://github.com/user/repo")
        process_repo(db_conn, repo_id, "https://github.com/user/repo", datetime.now(tz=timezone.utc))

        with db_conn.cursor() as cur:
            cur.execute("SELECT error FROM log WHERE repository_id = %s", (repo_id,))
            row = cur.fetchone()
        assert "API timeout" in row[0]

    def test_clone_invalid_url_raises(self, tmp_path):
        with pytest.raises(RuntimeError, match="Clone failed"):
            clone_repo("https://github.com/does-not-exist-xyz/nope-404", str(tmp_path / "dest"))
