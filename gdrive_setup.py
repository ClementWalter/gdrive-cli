"""First-time Google setup without a terminal: the Cloud Console guide, OAuth client sources, and the loopback consent.

Each user owns the Google Cloud project and the "Desktop app" OAuth client the
CLI signs in with. One project and one client serve both gdrive-cli and
email-cli; only the scopes they request differ.
"""

import json
import os
import secrets
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from google_auth_oauthlib.flow import InstalledAppFlow

AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
CLIENT_SUFFIX = ".apps.googleusercontent.com"
# Extension settings arrive as these variables; a file path is the alternative.
ENV_ID, ENV_SECRET, ENV_FILE = "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_CLIENT_FILE"
CONSENT_SECONDS = 300

SCOPE_LABELS = {
    "https://www.googleapis.com/auth/drive": "See, edit, create, and delete all of your Google Drive files",
    "https://www.googleapis.com/auth/spreadsheets": "See, edit, create, and delete all your Google Sheets spreadsheets",
}


class SetupError(Exception):
    """A setup problem whose message names the next step."""


def installed_client(client_id: str, client_secret: str, project_id: str | None = None) -> dict:
    """The client-secrets document Google's "Download JSON" produces for a Desktop app."""
    client_id, client_secret = client_id.strip(), client_secret.strip()
    if not client_id.endswith(CLIENT_SUFFIX):
        raise SetupError(f"Client ID must end with {CLIENT_SUFFIX}; copy it from Google Auth Platform → Clients.")
    if not client_secret:
        raise SetupError("Client secret is empty; copy it from Google Auth Platform → Clients → your Desktop client.")
    body = {"client_id": client_id, "client_secret": client_secret, "auth_uri": AUTH_URI,
            "token_uri": TOKEN_URI, "redirect_uris": ["http://localhost"]}
    if project_id:
        body["project_id"] = project_id
    return {"installed": body}


def parse_client(document: dict) -> dict:
    """Validate a downloaded client JSON; only Desktop app clients can use the loopback sign-in."""
    if "web" in document:
        raise SetupError("This OAuth client is a \"Web application\". Create one of type \"Desktop app\" "
                         "(Google Auth Platform → Clients → Create client) and download its JSON.")
    body = document.get("installed")
    if not isinstance(body, dict) or not body.get("client_id") or not body.get("client_secret"):
        raise SetupError("Not a Google OAuth client file: expected the JSON from \"Download JSON\" on a Desktop app client.")
    return installed_client(body["client_id"], body["client_secret"], body.get("project_id"))


def read_client_file(path: Path) -> dict:
    try:
        return parse_client(json.loads(Path(path).expanduser().read_text()))
    except (OSError, ValueError) as exc:
        raise SetupError(f"Cannot read the client file {path}: {exc.__class__.__name__}.") from exc


def env_client(env=os.environ) -> tuple[dict, str] | None:
    """The client from the extension settings, with a label naming where it came from."""
    if env.get(ENV_ID) or env.get(ENV_SECRET):
        return installed_client(env.get(ENV_ID, ""), env.get(ENV_SECRET, "")), "extension settings"
    if env.get(ENV_FILE):
        return read_client_file(Path(env[ENV_FILE])), env[ENV_FILE]
    return None


def newest_download(home: Path) -> Path | None:
    """The most recent client JSON Chrome saved, named client_secret_<client id>.json by Google."""
    found = sorted((home / "Downloads").glob("client_secret_*.json"), key=lambda p: p.stat().st_mtime)
    return found[-1] if found else None


def guide(client_label: str | None, project_id: str | None, accounts: list[str], email_client: Path | None) -> str:
    """Steps for Claude to walk the user through in Chrome, starting from what already exists."""
    lines = ["# Set up Google Drive access", "", "## Current state"]
    if client_label:
        lines.append(f"- OAuth client: configured ({client_label}" + (f", project `{project_id}`" if project_id else "") + ").")
    elif email_client:
        lines.append(f"- OAuth client: none for Drive yet, but the Email extension has one ({email_client}); "
                     "`connect` reuses it, so skip to step 7 after enabling the Drive and Sheets APIs in that same project (step 2).")
    else:
        lines.append("- OAuth client: not configured.")
    lines.append(f"- Connected Google accounts: {', '.join(accounts) if accounts else 'none'}.")
    if client_label:
        lines += ["", "The client is in place: call `connect` (step 7). Steps 1-6 are only needed for a new project."]
    lines += [
        "",
        "## How to run this",
        "Use Claude in Chrome, in the Chrome profile where the user is signed in to the Google account to connect. "
        "Do the clicks in Google Cloud Console for the user, step by step, saying what each step is for. "
        "Let the user do the Google sign-in, the \"unverified app\" warning and the consent checkboxes themselves: "
        "those are their decisions. If the Gmail / Email extension was set up before, reuse its Google Cloud project "
        "(pick it in the project picker at the top of the console) and its OAuth client instead of creating new ones: "
        "one project and one Desktop app client serve both extensions.",
        "",
        "## Steps",
        "1. Create the project: open https://console.cloud.google.com/projectcreate . A first-time Cloud user is asked "
        "to pick a country and accept the Terms of Service first. Project name: `Claude Google access` (any name works). "
        "Location: leave as is. Click **Create**, wait for the notification, then click **Select project** "
        "so the project picker at the top shows it. Note the project ID shown under the name; the links below use it as PROJECT_ID.",
        "2. Enable the APIs, one page each, clicking **Enable** (a page showing **Manage** is already enabled):",
        "   - https://console.cloud.google.com/apis/library/drive.googleapis.com?project=PROJECT_ID (Google Drive API)",
        "   - https://console.cloud.google.com/apis/library/sheets.googleapis.com?project=PROJECT_ID (Google Sheets API)",
        "   - https://console.cloud.google.com/apis/library/gmail.googleapis.com?project=PROJECT_ID (Gmail API: only the "
        "Email extension uses it; enabling it now lets that extension reuse this project without coming back here)",
        "   Google Docs are read through the Drive API; the Docs API is not needed.",
        "3. Configure the consent screen: open https://console.cloud.google.com/auth/overview?project=PROJECT_ID "
        "(menu ☰ → **Google Auth Platform**). It says \"Google Auth Platform not configured yet\": click **Get started**.",
        "   - **App Information**: App name `Claude Google access`; User support email: the user's address. **Next**.",
        "   - **Audience**: **External** for a personal @gmail.com account. A company Google Workspace account whose "
        "project belongs to the company organization may pick **Internal**: then skip step 4. **Next**.",
        "   - **Contact Information**: the user's address. **Next**.",
        "   - **Finish**: tick **I agree to the Google API Services: User Data Policy**, click **Continue**, then **Create**.",
        "   Nothing is needed under **Data Access**: the app asks for its scopes at sign-in.",
        "4. Audience (External only): open https://console.cloud.google.com/auth/audience?project=PROJECT_ID .",
        "   - Under **Test users**, click **Add users**, enter the user's Google address, **Save**.",
        "   - Under **Publishing status** (\"Testing\"), click **Publish app** and confirm, so the status reads "
        "**In production**. In \"Testing\", Google expires the sign-in after 7 days and the user would have to "
        "reconnect every week. In production without verification, sign-in shows a \"Google hasn't verified this app\" "
        "warning and is capped at 100 users; Google exempts personal-use apps (under 100 users) from verification, "
        "so there is nothing to submit. If the user prefers to stay in Testing, it works but needs `connect` again every 7 days.",
        "5. Create the OAuth client: open https://console.cloud.google.com/auth/clients?project=PROJECT_ID , click "
        "**Create client**. Application type: **Desktop app**. Name: `Claude Desktop`. Leave "
        "**Use this client for an AI-powered agent** unticked (it cannot be changed later and is not needed). Click **Create**.",
        "6. Save the client: the **OAuth client created** dialog shows the Client ID and Client secret. Click "
        "**Download JSON**: Chrome saves `client_secret_<...>.apps.googleusercontent.com.json` in Downloads, "
        "and `connect` finds it there on its own. Google shows the secret only now, so download it before closing the "
        "dialog. Alternatives: pass `client_file` (the JSON's path) to `connect`, or have the user paste Client ID and "
        "Client secret in Claude Desktop → Settings → Extensions → Google Drive → Configure. Never paste the secret in the chat.",
        "7. Connect: call the `connect` tool (`account` names the slot, e.g. `work` for a second Google account; "
        "`login_hint` pre-selects the address). A Google sign-in page opens in the browser:",
        "   - choose the account;",
        "   - on \"Google hasn't verified this app\" (only in production): **Advanced** → **Go to Claude Google access (unsafe)**;",
        "   - on the access page, tick every box: "
        + "; ".join(f"\"{label}\"" for label in SCOPE_LABELS.values()) + ". Click **Continue**.",
        "   The page then says Google Drive is connected. Call `whoami` to confirm; `connect` with `wait` set "
        "(seconds) blocks until the user finishes instead.",
        "",
        "## Afterwards",
        "- More Google accounts: `connect` with another `account` name, using the same client. `accounts` lists them; "
        "`set_default` picks the one tools use without `account`.",
        "- \"Access blocked: ... has not completed the Google verification process\" means the address is not a test "
        "user and the app is still in Testing: add it in step 4.",
        "- \"... API has not been used in project ... or it is disabled\": enable that API (step 2) in the client's project.",
        "- `invalid_grant` or \"Token has been expired or revoked\": call `connect` again.",
    ]
    return "\n".join(lines) + "\n"


def missing_scopes(granted, wanted: list[str]) -> list[str]:
    """Google's consent page lets the user untick scopes; the token then carries fewer than asked."""
    if isinstance(granted, str):
        granted = granted.split()
    return [scope for scope in wanted if scope not in set(granted or [])]


def _page(title: str, body: str) -> bytes:
    return (f"<!doctype html><meta charset=utf-8><title>{title}</title>"
            f"<body style='font-family:system-ui;max-width:36em;margin:4em auto'>"
            f"<h2>{title}</h2><p>{body}</p>").encode()


def consent(client: dict, scopes: list[str], report, *, login_hint: str | None = None,
            timeout: float = CONSENT_SECONDS, open_browser=webbrowser.open):
    """Run Google's installed-app loopback consent and return the credentials.

    `report(status, **fields)` records progress for the command that spawned
    this helper: "waiting" with the URL once the page is up, then "connected"
    or "error". The `state` parameter ties the redirect to this request, and the
    flow's PKCE verifier to this process.
    """
    # Google may grant a subset of the scopes; that is checked below, not raised mid-exchange.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    flow = InstalledAppFlow.from_client_config(client, scopes)
    received: dict = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            if not ({"code", "error"} & query.keys()):
                self.send_error(404)
                return
            received.update({k: v[0] for k, v in query.items()})
            ok = "code" in query and received.get("state") == expected_state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_page("Google Drive is connected", "You can close this tab and go back to Claude.") if ok
                             else _page("Google Drive is not connected", "Go back to Claude and ask it to connect again."))

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Callback)
    server.timeout = 0.5
    flow.redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    extra = {"login_hint": login_hint} if login_hint else {}
    url, expected_state = flow.authorization_url(prompt="consent", state=secrets.token_urlsafe(24), **extra)
    report("waiting", url=url)
    open_browser(url)
    deadline = time.monotonic() + timeout
    try:
        while not received and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not received:
        raise SetupError(f"Nobody finished the Google sign-in within {int(timeout)} s; call `connect` again.")
    if received.get("error"):
        raise SetupError(f"Google sign-in was not completed ({received['error']}); call `connect` again.")
    if received.get("state") != expected_state:
        raise SetupError("The sign-in answer did not match this request; call `connect` again.")
    flow.fetch_token(code=received["code"])
    missing = missing_scopes(flow.oauth2session.token.get("scope"), scopes)
    if missing:
        raise SetupError("Some access boxes were left unticked (" + "; ".join(SCOPE_LABELS.get(s, s) for s in missing)
                         + "). Call `connect` again and tick every box.")
    return flow.credentials
