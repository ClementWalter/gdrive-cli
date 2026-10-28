#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pytest==9.1.1",
#     "click==8.5.0",
#     "google-api-python-client==2.201.0",
#     "google-auth-httplib2==0.4.4",
#     "google-auth-oauthlib==1.5.0",
#     "pymupdf==1.28.2",
#     "rich==15.0.0",
# ]
#
# [tool.uv]
# exclude-newer = "2026-10-27T00:00:00Z"
# ///
"""Terminal-free setup: client sources, the setup guide, and the loopback consent against a stub token endpoint."""

import json
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from click.testing import CliRunner
from google.oauth2.credentials import Credentials

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gdrive_cli as cli
import gdrive_setup as setup

CLIENT_ID = "123-abc.apps.googleusercontent.com"


@pytest.fixture
def client_doc():
    return {"installed": {"client_id": CLIENT_ID, "client_secret": "s3cret", "project_id": "claude-google-access",
                          "auth_uri": setup.AUTH_URI, "token_uri": setup.TOKEN_URI, "redirect_uris": ["http://localhost"]}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty HOME with this CLI's and email-cli's config dirs inside it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (setup.ENV_ID, setup.ENV_SECRET, setup.ENV_FILE):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path / ".config" / "gdrive-cli")
    monkeypatch.setattr(cli, "EMAIL_CONFIG_DIR", tmp_path / ".config" / "email-cli")
    monkeypatch.setattr(cli, "SKILL_DIR", tmp_path / "repo")
    monkeypatch.setattr(cli, "LEGACY_CONFIG_DIRS", ())
    return tmp_path


@pytest.fixture(autouse=True)
def no_helper_process(monkeypatch):
    """`connect` must never start a real sign-in helper (it would open a browser) from a test."""
    def refuse(*args, **kwargs):
        raise AssertionError("connect tried to spawn its helper")
    monkeypatch.setattr(cli.subprocess, "Popen", refuse)


def write_json(path: Path, document) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))
    return path


# -- client documents --------------------------------------------------------


def test_client_id_must_be_a_google_client_id():
    with pytest.raises(setup.SetupError, match="apps.googleusercontent.com"):
        setup.installed_client("not-a-client", "secret")


def test_web_client_is_refused_with_the_desktop_app_fix():
    with pytest.raises(setup.SetupError, match="Desktop app"):
        setup.parse_client({"web": {"client_id": CLIENT_ID, "client_secret": "x"}})


def test_downloaded_client_keeps_its_project(client_doc):
    assert setup.parse_client(client_doc)["installed"]["project_id"] == "claude-google-access"


def test_settings_client_is_labelled_extension_settings():
    env = {setup.ENV_ID: CLIENT_ID, setup.ENV_SECRET: "s3cret"}
    assert setup.env_client(env)[1] == "extension settings"


def test_no_settings_means_no_env_client():
    assert setup.env_client({}) is None


def test_newest_download_wins(tmp_path, client_doc):
    old = write_json(tmp_path / "Downloads" / "client_secret_old.json", client_doc)
    new = write_json(tmp_path / "Downloads" / "client_secret_new.json", client_doc)
    import os
    os.utime(old, (1, 1))
    assert setup.newest_download(tmp_path) == new


@pytest.mark.parametrize("granted", ["https://www.googleapis.com/auth/drive", ["https://www.googleapis.com/auth/drive"]])
def test_missing_scopes_reads_space_separated_or_list(granted):
    assert setup.missing_scopes(granted, cli.SCOPES) == ["https://www.googleapis.com/auth/spreadsheets"]


# -- guide -------------------------------------------------------------------


def test_guide_skips_to_connect_when_a_client_exists():
    assert "call `connect` (step 7)" in setup.guide("extension settings", "p", [], None)


def test_guide_points_to_the_email_extension_client(tmp_path):
    assert "Email extension has one" in setup.guide(None, None, [], tmp_path / "client_secret.json")


@pytest.mark.parametrize("needle", ["**Desktop app**", "**Publish app**", "**Add users**", "**Download JSON**",
                                    "gmail.googleapis.com", "**External**"])
def test_guide_names_each_console_step(needle):
    assert needle in setup.guide(None, None, [], None)


# -- client resolution in the CLI -------------------------------------------


def test_settings_win_over_the_stored_client(home, client_doc, monkeypatch):
    write_json(cli.CONFIG_DIR / "client_secret.json", client_doc)
    monkeypatch.setenv(setup.ENV_ID, "999-other.apps.googleusercontent.com")
    monkeypatch.setenv(setup.ENV_SECRET, "other")
    assert cli._resolve_client("default")[0]["installed"]["client_id"].startswith("999-")


def test_email_extension_client_is_reused(home, client_doc):
    write_json(cli.EMAIL_CONFIG_DIR / "client_secret.json", client_doc)
    assert cli._resolve_client("default")[1].startswith("Email extension")


def test_downloads_are_searched_only_for_connect(home, client_doc):
    write_json(home / "Downloads" / f"client_secret_{CLIENT_ID}.json", client_doc)
    with pytest.raises(setup.SetupError):
        cli._resolve_client("default")


def test_connect_finds_the_downloaded_client(home, client_doc):
    download = write_json(home / "Downloads" / f"client_secret_{CLIENT_ID}.json", client_doc)
    assert cli._resolve_client("default", downloads=True)[1] == str(download)


def test_stored_client_is_private(home, client_doc):
    assert cli._store_client(client_doc).stat().st_mode & 0o777 == 0o600


# -- commands ----------------------------------------------------------------


def test_connect_without_client_names_google_setup(home):
    result = CliRunner().invoke(cli.cli, ["connect"])
    assert "`google_setup`" in result.output


def test_not_connected_error_names_the_connect_tool(home):
    result = CliRunner().invoke(cli.cli, ["whoami"])
    assert "call the `connect` tool" in result.output


def test_accounts_json_lists_the_default(home):
    write_json(cli.CONFIG_DIR / "accounts" / "work" / "token.json", {})
    write_json(cli.CONFIG_DIR / "config.json", {"default_account": "work"})
    result = CliRunner().invoke(cli.cli, ["accounts", "--json"])
    assert json.loads(result.output) == [{"account": "work", "default": True}]


def test_disconnect_without_yes_keeps_the_token(home):
    token = write_json(cli.CONFIG_DIR / "accounts" / "work" / "token.json", {})
    CliRunner().invoke(cli.cli, ["disconnect", "--account", "work"])
    assert token.exists()


def test_disconnect_with_yes_removes_the_token(home):
    token = write_json(cli.CONFIG_DIR / "accounts" / "work" / "token.json", {})
    CliRunner().invoke(cli.cli, ["disconnect", "--account", "work", "--yes"])
    assert not token.exists()


@pytest.fixture
def stub_consent(monkeypatch):
    creds = Credentials(token="t", refresh_token="r", client_id=CLIENT_ID, client_secret="s3cret",
                        token_uri=setup.TOKEN_URI, scopes=cli.SCOPES)

    def fake(client, scopes, report, **kwargs):
        report("waiting", url="https://accounts.google.com/o/oauth2/auth?x=1")
        return creds

    monkeypatch.setattr(setup, "consent", fake)
    monkeypatch.setattr(cli, "_whoami", lambda account: {"email": "me@example.com"})


def test_helper_reports_the_connected_address(home, client_doc, stub_consent):
    client = write_json(home / "client.json", client_doc)
    cli._connect_helper("work", str(client), None)
    assert json.loads(cli._connect_state("work").read_text())["email"] == "me@example.com"


def test_helper_stores_a_private_token(home, client_doc, stub_consent):
    client = write_json(home / "client.json", client_doc)
    cli._connect_helper("work", str(client), None)
    assert cli._token_path("work").stat().st_mode & 0o777 == 0o600


def test_first_connected_account_becomes_default(home, client_doc, stub_consent):
    client = write_json(home / "client.json", client_doc)
    cli._connect_helper("work", str(client), None)
    assert cli._get_default_account() == "work"


def test_helper_reports_setup_errors(home, client_doc, monkeypatch):
    client = write_json(home / "client.json", client_doc)

    def refuse(*args, **kwargs):
        raise setup.SetupError("Some access boxes were left unticked")

    monkeypatch.setattr(setup, "consent", refuse)
    cli._connect_helper("work", str(client), None)
    assert json.loads(cli._connect_state("work").read_text())["error"] == "Some access boxes were left unticked"


# -- loopback consent against a stub Google token endpoint -------------------


@pytest.fixture
def token_endpoint(monkeypatch):
    """A local stand-in for oauth2.googleapis.com/token; `scope` is what the user ticked."""
    monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "1")
    seen: dict = {"scope": " ".join(cli.SCOPES)}

    class Token(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["body"] = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            body = json.dumps({"access_token": "at", "refresh_token": "rt", "expires_in": 3600,
                               "token_type": "Bearer", "scope": seen["scope"]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Token)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    seen["uri"] = f"http://127.0.0.1:{server.server_port}/token"
    yield seen
    server.shutdown()


def browser(answer):
    """A browser that follows the consent URL straight to the redirect, with `answer(state)` as the query."""
    def open_browser(url):
        query = parse_qs(urlparse(url).query)
        target = query["redirect_uri"][0] + "?" + urlencode(answer(query["state"][0]))
        threading.Thread(target=lambda: urllib.request.urlopen(target, timeout=5).read(), daemon=True).start()
    return open_browser


def run_consent(client_doc, token_endpoint, answer):
    client_doc["installed"]["token_uri"] = token_endpoint["uri"]
    return setup.consent(client_doc, cli.SCOPES, lambda *a, **k: None, timeout=10, open_browser=browser(answer))


def test_consent_returns_the_refresh_token(client_doc, token_endpoint):
    creds = run_consent(client_doc, token_endpoint, lambda state: {"state": state, "code": "c0de"})
    assert creds.refresh_token == "rt"


def test_consent_sends_the_pkce_verifier(client_doc, token_endpoint):
    run_consent(client_doc, token_endpoint, lambda state: {"state": state, "code": "c0de"})
    assert len(token_endpoint["body"]["code_verifier"][0]) == 128


def test_consent_rejects_a_foreign_state(client_doc, token_endpoint):
    with pytest.raises(setup.SetupError, match="did not match"):
        run_consent(client_doc, token_endpoint, lambda state: {"state": "forged", "code": "c0de"})


def test_consent_reports_a_cancelled_sign_in(client_doc, token_endpoint):
    with pytest.raises(setup.SetupError, match="access_denied"):
        run_consent(client_doc, token_endpoint, lambda state: {"state": state, "error": "access_denied"})


def test_consent_refuses_unticked_scopes(client_doc, token_endpoint):
    token_endpoint["scope"] = cli.SCOPES[0]
    with pytest.raises(setup.SetupError, match="unticked"):
        run_consent(client_doc, token_endpoint, lambda state: {"state": state, "code": "c0de"})


def test_consent_reports_the_url_before_opening_it(client_doc, token_endpoint):
    reports = []
    client_doc["installed"]["token_uri"] = token_endpoint["uri"]
    setup.consent(client_doc, cli.SCOPES, lambda status, **fields: reports.append(status), timeout=10,
                  open_browser=browser(lambda state: {"state": state, "code": "c0de"}))
    assert reports == ["waiting"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
