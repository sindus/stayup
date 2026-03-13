"""Unit tests — no external dependencies (DB, git, network)."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from check_changelog import (
    cleanup_old_changelogs,
    clone_repo,
    compute_diff,
    find_changelog,
    get_changelog_git_date,
    get_latest_release,
    init_db,
    parse_github_owner_repo,
    save_changelog,
    save_error,
    upsert_repository,
)

# ---------------------------------------------------------------------------
# parse_github_owner_repo
# ---------------------------------------------------------------------------


class TestParseGithubOwnerRepo:
    def test_standard_url(self):
        assert parse_github_owner_repo("https://github.com/facebook/react") == ("facebook", "react")

    def test_trailing_slash(self):
        assert parse_github_owner_repo("https://github.com/vercel/next.js/") == ("vercel", "next.js")


# ---------------------------------------------------------------------------
# find_changelog
# ---------------------------------------------------------------------------


class TestFindChangelog:
    def test_finds_changelog_md(self, tmp_path):
        (tmp_path / "CHANGELOG.md").write_text("content")
        assert find_changelog(str(tmp_path)).endswith("CHANGELOG.md")

    def test_finds_first_match_in_priority_order(self, tmp_path):
        (tmp_path / "CHANGELOG.md").write_text("a")
        (tmp_path / "changelog.md").write_text("b")
        assert find_changelog(str(tmp_path)).endswith("CHANGELOG.md")

    def test_falls_back_to_other_names(self, tmp_path):
        (tmp_path / "HISTORY.md").write_text("content")
        assert find_changelog(str(tmp_path)).endswith("HISTORY.md")

    def test_returns_none_when_not_found(self, tmp_path):
        assert find_changelog(str(tmp_path)) is None

    def test_ignores_directories_with_changelog_name(self, tmp_path):
        (tmp_path / "CHANGELOG.md").mkdir()
        assert find_changelog(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# clone_repo
# ---------------------------------------------------------------------------


class TestCloneRepo:
    @patch("check_changelog.subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        clone_repo("https://example.com/repo", "/tmp/dest")
        args, kwargs = mock_run.call_args
        assert args[0] == ["git", "clone", "--depth=1", "https://example.com/repo", "/tmp/dest"]
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"

    @patch("check_changelog.subprocess.run")
    def test_failure_raises_runtime_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=128, stderr="repo not found")
        with pytest.raises(RuntimeError, match="Clone failed"):
            clone_repo("https://example.com/bad", "/tmp/dest")


# ---------------------------------------------------------------------------
# get_changelog_git_date
# ---------------------------------------------------------------------------


class TestGetChangelogGitDate:
    @patch("check_changelog.subprocess.run")
    def test_returns_datetime_on_valid_output(self, mock_run):
        mock_run.return_value = MagicMock(stdout="2024-06-15T12:00:00+00:00\n")
        result = get_changelog_git_date("/repo", "CHANGELOG.md")
        assert result == datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    @patch("check_changelog.subprocess.run")
    def test_returns_none_on_empty_output(self, mock_run):
        mock_run.return_value = MagicMock(stdout="")
        result = get_changelog_git_date("/repo", "CHANGELOG.md")
        assert result is None


# ---------------------------------------------------------------------------
# get_latest_release
# ---------------------------------------------------------------------------


class TestGetLatestRelease:
    @patch("check_changelog.requests.get")
    def test_returns_release_data(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "tag_name": "v1.2.3",
                "body": "- Fix bug\n- Add feature",
                "published_at": "2024-06-15T12:00:00Z",
            },
        )
        tag, body, date = get_latest_release("https://github.com/user/repo")
        assert tag == "v1.2.3"
        assert body == "- Fix bug\n- Add feature"
        assert date == datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    @patch("check_changelog.requests.get")
    def test_returns_none_on_404(self, mock_get):
        mock_get.return_value = MagicMock(status_code=404)
        result = get_latest_release("https://github.com/user/repo")
        assert result is None

    @patch("check_changelog.requests.get")
    def test_empty_body_returns_empty_string(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "tag_name": "v1.0.0",
                "body": None,
                "published_at": "2024-01-01T00:00:00Z",
            },
        )
        _, body, _ = get_latest_release("https://github.com/user/repo")
        assert body == ""

    @patch("check_changelog.requests.get")
    def test_sends_token_header_when_set(self, mock_get, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "mytoken")
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "tag_name": "v1.0.0",
                "body": "content",
                "published_at": "2024-01-01T00:00:00Z",
            },
        )
        get_latest_release("https://github.com/user/repo")
        headers = mock_get.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer mytoken"


# ---------------------------------------------------------------------------
# compute_diff
# ---------------------------------------------------------------------------


class TestComputeDiff:
    def test_returns_none_when_identical(self):
        assert compute_diff("same content", "same content") is None

    def test_returns_diff_when_changed(self):
        result = compute_diff("line1\n", "line1\nline2\n")
        assert result is not None
        assert "line2" in result

    def test_diff_format_is_unified(self):
        result = compute_diff("old\n", "new\n")
        assert "---" in result
        assert "+++" in result


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def make_conn_mock():
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cursor


class TestInitDb:
    def test_executes_ddl_and_commits(self):
        conn, cursor = make_conn_mock()
        init_db(conn)
        assert cursor.execute.call_count == 1  # DDL only
        conn.commit.assert_called_once()


class TestUpsertRepository:
    def test_returns_id(self):
        conn, cursor = make_conn_mock()
        cursor.fetchone.return_value = (42,)
        result = upsert_repository(conn, "https://github.com/user/repo")
        assert result == 42
        sql = cursor.execute.call_args[0][0]
        assert "INSERT INTO repository" in sql
        assert "ON CONFLICT" in sql

    def test_passes_url_as_parameter(self):
        conn, cursor = make_conn_mock()
        cursor.fetchone.return_value = (1,)
        upsert_repository(conn, "https://github.com/user/repo")
        params = cursor.execute.call_args[0][1]
        assert params == ("https://github.com/user/repo",)


class TestSaveChangelog:
    def test_inserts_with_version_and_commits(self):
        conn, cursor = make_conn_mock()
        executed_at = datetime.now(tz=timezone.utc)
        save_changelog(conn, 1, "v1.0.0", "## v1.0\n- init", None, None, executed_at)
        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()
        params = cursor.execute.call_args[0][1]
        assert params[0] == 1  # provider_id
        assert params[1] == "v1.0.0"  # version
        assert params[2] == "## v1.0\n- init"  # content
        assert params[5] == executed_at

    def test_success_flag_in_sql(self):
        conn, cursor = make_conn_mock()
        save_changelog(conn, 1, None, "content", None, None, datetime.now(tz=timezone.utc))
        sql = cursor.execute.call_args[0][0]
        assert "TRUE" in sql


class TestSaveError:
    def test_inserts_error_and_commits(self):
        conn, cursor = make_conn_mock()
        executed_at = datetime.now(tz=timezone.utc)
        save_error(conn, 5, "something went wrong", executed_at)
        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()
        params = cursor.execute.call_args[0][1]
        assert params == (5, "something went wrong", executed_at)

    def test_accepts_none_repository_id(self):
        conn, cursor = make_conn_mock()
        save_error(conn, None, "error", datetime.now(tz=timezone.utc))
        params = cursor.execute.call_args[0][1]
        assert params[0] is None


class TestCleanupOldChangelogs:
    def test_executes_delete_and_commits(self):
        conn, cursor = make_conn_mock()
        cleanup_old_changelogs(conn, 1)
        cursor.execute.assert_called_once()
        conn.commit.assert_called_once()
        sql = cursor.execute.call_args[0][0]
        assert "DELETE FROM connector_changelog" in sql

    def test_passes_correct_params(self):
        conn, cursor = make_conn_mock()
        cleanup_old_changelogs(conn, 7)
        params = cursor.execute.call_args[0][1]
        assert params[0] == 7  # provider_id for DELETE
        assert params[1] == 7  # provider_id for subquery
        assert params[2] == 3  # MAX_CHANGELOGS_PER_REPO
