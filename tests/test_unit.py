"""Unit tests — no external dependencies. stayup-api itself is mocked
(unittest.mock.patch on `requests.request`); its actual behavior is covered
by stayup-api's own test suite. Git and the GitHub releases API are mocked too."""

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from check_changelog import (
    DISPLAY_TEMPLATE,
    _content_hash,
    add_source,
    cleanup_old_entries,
    clone_repo,
    find_changelog,
    get_changelog_git_date,
    get_latest_version,
    get_releases,
    get_saved_versions,
    get_sources,
    parse_github_owner_repo,
    process_repository,
    register_provider,
    save_entry,
    save_error,
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
# get_releases
# ---------------------------------------------------------------------------


class TestGetReleases:
    @patch("check_changelog.requests.get")
    def test_returns_list_of_releases(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [
                {"tag_name": "v1.2.3", "body": "- Fix bug", "published_at": "2024-06-15T12:00:00Z"},
                {"tag_name": "v1.2.2", "body": "- Previous", "published_at": "2024-05-01T00:00:00Z"},
            ],
        )
        releases = get_releases("https://github.com/user/repo")
        assert len(releases) == 2
        assert releases[0][0] == "v1.2.3"
        assert releases[0][1] == "- Fix bug"
        assert releases[1][0] == "v1.2.2"

    @patch("check_changelog.requests.get")
    def test_returns_empty_list_on_404(self, mock_get):
        mock_get.return_value = MagicMock(status_code=404)
        result = get_releases("https://github.com/user/repo")
        assert result == []

    @patch("check_changelog.requests.get")
    def test_returns_empty_list_when_no_releases(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: [])
        result = get_releases("https://github.com/user/repo")
        assert result == []

    @patch("check_changelog.requests.get")
    def test_empty_body_returns_empty_string(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"tag_name": "v1.0.0", "body": None, "published_at": "2024-01-01T00:00:00Z"}],
        )
        releases = get_releases("https://github.com/user/repo")
        assert releases[0][1] == ""

    @patch("check_changelog.requests.get")
    def test_sends_token_header_when_set(self, mock_get, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "mytoken")
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{"tag_name": "v1.0.0", "body": "content", "published_at": "2024-01-01T00:00:00Z"}],
        )
        get_releases("https://github.com/user/repo")
        headers = mock_get.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer mytoken"

    @patch("check_changelog.requests.get")
    def test_passes_per_page_param(self, mock_get):
        mock_get.return_value = MagicMock(status_code=200, json=lambda: [])
        get_releases("https://github.com/user/repo", limit=3)
        params = mock_get.call_args[1]["params"]
        assert params["per_page"] == 3


# ---------------------------------------------------------------------------
# _content_hash
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_stable_for_the_same_content(self):
        assert _content_hash("## v1\n- a") == _content_hash("## v1\n- a")

    def test_differs_for_different_content(self):
        assert _content_hash("a") != _content_hash("b")


# ---------------------------------------------------------------------------
# api_request helpers
# ---------------------------------------------------------------------------


def mock_response(json_body=None, status=200):
    response = MagicMock()
    response.status_code = status
    response.content = b"{}" if json_body is not None else b""
    response.json.return_value = json_body
    response.raise_for_status.return_value = None
    return response


@patch("check_changelog.API_KEY", "test-key")
class TestRegisterProvider:
    @patch("check_changelog.requests.request")
    def test_posts_display_name_sort_order_and_template(self, mock_request):
        mock_request.return_value = mock_response()
        register_provider()
        method, url = mock_request.call_args[0]
        assert method == "POST"
        assert url.endswith("/connector-api/changelog/register")
        body = mock_request.call_args.kwargs["json"]
        assert body["displayName"] == "Changelog"
        assert body["sortOrder"] == 10
        assert body["template"] == DISPLAY_TEMPLATE


class TestApiRequestWithoutKey:
    @patch("check_changelog.API_KEY", None)
    def test_raises_when_no_api_key_is_configured(self):
        with pytest.raises(RuntimeError, match="STAYUP_API_KEY"):
            register_provider()


@patch("check_changelog.API_KEY", "test-key")
class TestAddSource:
    @patch("check_changelog.requests.request")
    def test_posts_the_url_and_returns_the_id(self, mock_request):
        mock_request.return_value = mock_response({"id": 42, "url": "https://github.com/user/repo"})
        assert add_source("https://github.com/user/repo") == 42
        method, url = mock_request.call_args[0]
        assert method == "POST"
        assert url.endswith("/connector-api/changelog/sources")


@patch("check_changelog.API_KEY", "test-key")
class TestGetSources:
    @patch("check_changelog.requests.request")
    def test_returns_id_url_config_tuples(self, mock_request):
        mock_request.return_value = mock_response(
            {"sources": [{"id": 1, "url": "https://github.com/a/b", "config": {"max_iterations": 3}}]}
        )
        assert get_sources() == [(1, "https://github.com/a/b", {"max_iterations": 3})]


@patch("check_changelog.API_KEY", "test-key")
class TestGetLatestVersion:
    @patch("check_changelog.requests.request")
    def test_returns_none_on_first_run(self, mock_request):
        mock_request.return_value = mock_response({"version": None})
        assert get_latest_version(1) is None

    @patch("check_changelog.requests.request")
    def test_returns_the_version(self, mock_request):
        mock_request.return_value = mock_response({"version": "v1.0.0"})
        assert get_latest_version(1) == "v1.0.0"
        url = mock_request.call_args[0][1]
        assert url.endswith("/connector-api/changelog/sources/1/state")


@patch("check_changelog.API_KEY", "test-key")
class TestGetSavedVersions:
    @patch("check_changelog.requests.request")
    def test_returns_set_of_versions(self, mock_request):
        mock_request.return_value = mock_response({"versions": ["v1.0.0", "v1.1.0"]})
        assert get_saved_versions(1) == {"v1.0.0", "v1.1.0"}
        url = mock_request.call_args[0][1]
        assert url.endswith("/connector-api/changelog/sources/1/versions")

    @patch("check_changelog.requests.request")
    def test_returns_empty_set_when_no_entries(self, mock_request):
        mock_request.return_value = mock_response({"versions": []})
        assert get_saved_versions(1) == set()


@patch("check_changelog.API_KEY", "test-key")
class TestSaveEntry:
    @patch("check_changelog.requests.request")
    def test_posts_a_single_item_with_version(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        executed_at = datetime.now(tz=timezone.utc)
        save_entry(1, "v1.0.0", "## v1.0\n- init", None, executed_at)
        item = mock_request.call_args.kwargs["json"]["items"][0]
        assert item["repositoryId"] == 1
        assert item["version"] == "v1.0.0"
        assert item["content"] == "## v1.0\n- init"
        assert item["success"] is True

    @patch("check_changelog.requests.request")
    def test_accepts_a_none_version(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        save_entry(1, None, "content", None, datetime.now(tz=timezone.utc))
        assert mock_request.call_args.kwargs["json"]["items"][0]["version"] is None


@patch("check_changelog.API_KEY", "test-key")
class TestSaveError:
    @patch("check_changelog.requests.request")
    def test_posts_the_error(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        executed_at = datetime.now(tz=timezone.utc)
        save_error(5, "something went wrong", executed_at)
        body = mock_request.call_args.kwargs["json"]
        assert body == {"repositoryId": 5, "error": "something went wrong", "executedAt": executed_at.isoformat()}

    @patch("check_changelog.requests.request")
    def test_accepts_none_repository_id(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        save_error(None, "error", datetime.now(tz=timezone.utc))
        assert mock_request.call_args.kwargs["json"]["repositoryId"] is None


@patch("check_changelog.API_KEY", "test-key")
class TestCleanupOldEntries:
    @patch("check_changelog.requests.request")
    def test_sends_retention_days_as_a_query_param(self, mock_request):
        mock_request.return_value = mock_response({"success": True})
        cleanup_old_entries(7, 30)
        method, url = mock_request.call_args[0]
        assert method == "DELETE"
        assert url.endswith("/connector-api/changelog/sources/7/old-items")
        assert mock_request.call_args.kwargs["params"] == {"retentionDays": 30}


class TestDisplayTemplate:
    def test_round_trips_through_json_unchanged(self):
        assert json.loads(json.dumps(DISPLAY_TEMPLATE)) == DISPLAY_TEMPLATE

    def test_ships_a_self_describing_icon(self):
        icon = DISPLAY_TEMPLATE["display"]["icon"]
        assert isinstance(icon, dict)
        assert icon["paths"]
        assert all(p[:1] in ("M", "m") for p in icon["paths"])
        assert icon["viewBox"] == "0 0 24 24"

    def test_text_detail_with_open_on_github(self):
        assert DISPLAY_TEMPLATE["version"] == 1
        assert DISPLAY_TEMPLATE["detail"]["mode"] == "text"
        assert DISPLAY_TEMPLATE["detail"]["openLabel"] == "Open on GitHub"
        assert "{repo}" in DISPLAY_TEMPLATE["detail"]["openUrl"]


# ---------------------------------------------------------------------------
# process_repository — end to end, stayup-api and GitHub/git mocked
# ---------------------------------------------------------------------------


@patch("check_changelog.API_KEY", "test-key")
class TestProcessRepository:
    @patch("check_changelog.save_error")
    @patch("check_changelog.save_entry")
    @patch("check_changelog.get_saved_versions")
    @patch("check_changelog.get_releases")
    def test_first_run_via_release_stores_only_latest(self, mock_releases, mock_saved, mock_save, mock_save_error):
        mock_releases.return_value = [
            ("v1.1.0", "newer", datetime(2024, 6, 1, tzinfo=timezone.utc)),
            ("v1.0.0", "older", datetime(2024, 1, 1, tzinfo=timezone.utc)),
        ]
        mock_saved.return_value = set()
        executed_at = datetime.now(tz=timezone.utc)
        process_repository(1, "https://github.com/user/repo", executed_at, {})

        mock_save.assert_called_once_with(1, "v1.1.0", "newer", datetime(2024, 6, 1, tzinfo=timezone.utc), executed_at)
        mock_save_error.assert_not_called()

    @patch("check_changelog.save_error")
    @patch("check_changelog.save_entry")
    @patch("check_changelog.get_saved_versions")
    @patch("check_changelog.get_releases")
    def test_no_insert_when_the_known_version_is_first(self, mock_releases, mock_saved, mock_save, _err):
        mock_releases.return_value = [("v1.0.0", "body", None)]
        mock_saved.return_value = {"v1.0.0"}
        process_repository(1, "https://github.com/user/repo", datetime.now(tz=timezone.utc), {})
        mock_save.assert_not_called()

    @patch("check_changelog.save_error")
    @patch("check_changelog.save_entry")
    @patch("check_changelog.get_saved_versions")
    @patch("check_changelog.get_releases")
    def test_saves_new_releases_until_the_known_one(self, mock_releases, mock_saved, mock_save, _err):
        mock_releases.return_value = [
            ("v1.2.0", "b", None),
            ("v1.1.0", "b", None),
            ("v1.0.0", "b", None),
        ]
        mock_saved.return_value = {"v1.0.0"}
        process_repository(1, "https://github.com/user/repo", datetime.now(tz=timezone.utc), {})

        saved_versions = [call.args[1] for call in mock_save.call_args_list]
        assert saved_versions == ["v1.2.0", "v1.1.0"]

    @patch("check_changelog.save_error")
    @patch("check_changelog.save_entry")
    @patch("check_changelog.get_saved_versions")
    @patch("check_changelog.get_releases")
    def test_capped_at_max_iterations(self, mock_releases, mock_saved, mock_save, _err):
        mock_releases.return_value = [(f"v{i}", "b", None) for i in range(5, 0, -1)]
        mock_saved.return_value = {"v0"}  # never matched: all 5 are "new"
        process_repository(1, "https://github.com/user/repo", datetime.now(tz=timezone.utc), {"max_iterations": 2})
        assert mock_save.call_count == 2

    @patch("check_changelog.save_error")
    @patch("check_changelog.get_latest_version")
    @patch("check_changelog.save_entry")
    @patch("check_changelog.get_changelog_from_repo")
    @patch("check_changelog.get_releases")
    def test_falls_back_to_file_when_no_releases(self, mock_releases, mock_from_repo, mock_save, mock_get_latest, _err):
        mock_releases.return_value = []
        mock_from_repo.return_value = (None, "## Changelog\n- v1", None)
        mock_get_latest.return_value = None
        process_repository(1, "https://github.com/user/repo", datetime.now(tz=timezone.utc), {})

        mock_save.assert_called_once()
        assert mock_save.call_args[0][2] == "## Changelog\n- v1"

    @patch("check_changelog.save_error")
    @patch("check_changelog.get_latest_version")
    @patch("check_changelog.save_entry")
    @patch("check_changelog.get_changelog_from_repo")
    @patch("check_changelog.get_releases")
    def test_file_fallback_no_insert_when_content_unchanged(
        self, mock_releases, mock_from_repo, mock_save, mock_get_latest, _err
    ):
        content = "## Changelog\n- v1"
        mock_releases.return_value = []
        mock_from_repo.return_value = (None, content, None)
        mock_get_latest.return_value = _content_hash(content)
        process_repository(1, "https://github.com/user/repo", datetime.now(tz=timezone.utc), {})
        mock_save.assert_not_called()

    @patch("check_changelog.save_error")
    @patch("check_changelog.get_releases")
    def test_logs_error_on_failure(self, mock_releases, mock_save_error):
        mock_releases.side_effect = Exception("network error")
        executed_at = datetime.now(tz=timezone.utc)
        process_repository(1, "https://github.com/user/repo", executed_at, {})
        mock_save_error.assert_called_once_with(1, "network error", executed_at)
