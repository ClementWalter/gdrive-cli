#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click",
#     "google-api-python-client",
#     "google-auth-httplib2",
#     "google-auth-oauthlib",
#     "pymupdf",
#     "rich",
# ]
# ///
"""Google Drive gateway CLI — docs, sheets, files, with multi-account support.

Provides authenticated access to Google Drive, Docs, and Sheets via OAuth2.
Supports multiple Google accounts with per-account token storage.
"""

import json
import logging
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import click
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account as sa_module
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from rich.console import Console
from rich.table import Table

logger = logging.getLogger(__name__)
console = Console()

# Scopes: full Drive access (covers Docs export) + full Sheets access
SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

# Set in cli() — when True, never open a browser for OAuth.
_NON_INTERACTIVE = False

DEFAULT_FILE_FIELDS = (
    "id, name, mimeType, modifiedTime, createdTime, "
    "owners(displayName,emailAddress,me), sharedWithMeTime, webViewLink, parents"
)

MIME_ALIASES = {
    "spreadsheet": "application/vnd.google-apps.spreadsheet",
    "pdf": "application/pdf",
    "doc": "application/vnd.google-apps.document",
    "folder": "application/vnd.google-apps.folder",
}

# Repo root (PEP 723 script). Do not store secrets here — it is a git checkout.
SKILL_DIR = Path(__file__).resolve().parent
# Per-user config: tokens, account list, default account
CONFIG_DIR = Path.home() / ".config" / "gdrive-cli"
LEGACY_CONFIG_DIRS = (
    Path.home() / ".config" / "gdrive",
    Path.home() / ".config" / "gdrive-sheets-compute",
)

# Export formats for Google Docs
DOC_EXPORT_FORMATS = {
    "text": "text/plain",
    "markdown": "text/markdown",
    "html": "text/html",
}


# ---------------------------------------------------------------------------
# URL / ID parsing
# ---------------------------------------------------------------------------


def _parse_google_id(url_or_id: str) -> str:
    """Extract a Google Drive/Docs/Sheets file ID from a URL or bare ID.

    Supports common Google URL patterns:
      - https://docs.google.com/document/d/{ID}/edit
      - https://docs.google.com/spreadsheets/d/{ID}/edit
      - https://docs.google.com/presentation/d/{ID}/edit
      - https://drive.google.com/file/d/{ID}/view
      - https://drive.google.com/open?id={ID}
      - Plain ID string (no slashes)
    """
    if "/" not in url_or_id and "?" not in url_or_id:
        # Already a bare ID — no URL structure to parse
        return url_or_id

    # Most Google URLs use /d/{ID}/ pattern
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", url_or_id)
    if match:
        return match.group(1)

    # Fallback: drive.google.com/open?id=... pattern
    parsed = urlparse(url_or_id)
    qs = parse_qs(parsed.query)
    if "id" in qs:
        return qs["id"][0]

    raise click.ClickException(f"Cannot extract file ID from: {url_or_id}")


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _get_config() -> dict:
    """Read the config.json file, returning defaults if missing."""
    config_path = CONFIG_DIR / "config.json"
    if config_path.exists():
        return json.loads(config_path.read_text())
    return {"default_account": "default"}


def _save_config(config: dict) -> None:
    """Persist config.json to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / "config.json").write_text(json.dumps(config, indent=2))


def _get_default_account() -> str:
    """Return the name of the default account from config."""
    return _get_config().get("default_account", "default")


def _token_path(account: str) -> Path:
    """Return the token file path for a named account."""
    return CONFIG_DIR / "accounts" / account / "token.json"


def _list_accounts() -> list[str]:
    """Return sorted list of account names that have a token.json."""
    accounts_dir = CONFIG_DIR / "accounts"
    if not accounts_dir.exists():
        return []
    return sorted(
        d.name
        for d in accounts_dir.iterdir()
        if d.is_dir() and (d / "token.json").exists()
    )


def _has_account_tokens(root: Path) -> bool:
    """True if root/accounts/<name>/token.json exists for any name."""
    accounts = root / "accounts"
    if not accounts.is_dir():
        return False
    return any(
        d.is_dir() and (d / "token.json").exists()
        for d in accounts.iterdir()
    )


def _copy_if_absent(src: Path, dst: Path) -> None:
    if not src.exists() or dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst, follow_symlinks=True)


def _maybe_migrate_legacy_config() -> None:
    """Copy tokens from older config dirs into ~/.config/gdrive-cli.

    Sources, in order:
    1. ~/.config/gdrive/ (multi-account layout)
    2. ~/.config/gdrive-sheets-compute/ (single token.json)

    No-op once gdrive-cli already has at least one account token.
    """
    if _has_account_tokens(CONFIG_DIR):
        return

    for legacy in LEGACY_CONFIG_DIRS:
        if _has_account_tokens(legacy):
            logger.info("Migrating config from %s to %s", legacy, CONFIG_DIR)
            shutil.copytree(
                legacy / "accounts",
                CONFIG_DIR / "accounts",
                dirs_exist_ok=True,
            )
            _copy_if_absent(legacy / "config.json", CONFIG_DIR / "config.json")
            for src in legacy.glob("client_secret*.json"):
                _copy_if_absent(src, CONFIG_DIR / src.name)
            _copy_if_absent(
                legacy / "service_account.json",
                CONFIG_DIR / "service_account.json",
            )
            console.print(
                f"[green]Migrated credentials from {legacy} to {CONFIG_DIR}[/green]"
            )
            return

        legacy_token = legacy / "token.json"
        if legacy_token.exists():
            logger.info("Migrating config from %s to %s", legacy, CONFIG_DIR)
            dest = CONFIG_DIR / "accounts" / "default"
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy_token, dest / "token.json")
            if not (CONFIG_DIR / "config.json").exists():
                _save_config({"default_account": "default"})
            console.print(
                f"[green]Migrated credentials from {legacy} to {CONFIG_DIR}[/green]"
            )
            return


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _find_file(name: str) -> Path | None:
    """Find a credential file in skill dir or config dir."""
    for d in (SKILL_DIR, CONFIG_DIR):
        p = d / name
        if p.exists():
            return p
    return None


def _find_client_secret(account: str) -> Path | None:
    """Find the OAuth client secret to use for a named account.

    Each account may carry its own OAuth client, so different accounts can live
    in different Google Cloud projects (with their own enabled APIs and consent
    screens) without clobbering each other. Account-specific secrets take
    precedence over the shared client_secret.json.

    Resolution order:
    1. ~/.config/gdrive-cli/accounts/{account}/client_secret.json (per-account)
    2. client_secret_{account}.json in the config dir (or the repo, last resort)
    3. shared client_secret.json in the config dir (or the repo, last resort)
    """
    per_account = CONFIG_DIR / "accounts" / account / "client_secret.json"
    if per_account.exists():
        return per_account
    named = _find_file(f"client_secret_{account}.json")
    if named:
        return named
    return _find_file("client_secret.json")


def _get_credentials(
    account: str,
    login_hint: str | None = None,
    *,
    interactive: bool = False,
    force: bool = False,
) -> Credentials:
    """Load credentials for a named account.

    Resolution order:
    1. Cached OAuth2 token at ~/.config/gdrive-cli/accounts/{account}/token.json
    2. Interactive OAuth2 flow via client_secret.json (opens browser) — only
       when ``interactive=True`` (``gdrive auth login``)
    3. Service account fallback (GOOGLE_APPLICATION_CREDENTIALS env or local file)

    The login_hint parameter pre-selects the Google account in the browser
    consent screen, useful when the user has multiple Google accounts.
    """
    token_file = _token_path(account)
    creds = None

    # `gdrive auth login` always re-consents. Other commands reuse a valid token.
    if not force and token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if creds and creds.expired and creds.refresh_token:
        # Token expired but we have a refresh token — try silent refresh
        try:
            creds.refresh(Request())
            _save_token(creds, account)
            return creds
        except Exception:
            logger.warning("Token refresh failed for account '%s'", account)
            creds = None
    elif creds and creds.valid:
        return creds

    if interactive and not _NON_INTERACTIVE:
        # Only `gdrive auth login` opens a browser. Other commands fail-loud.
        client_secret = _find_client_secret(account)
        if client_secret:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secret), SCOPES
            )
            kwargs = {}
            if login_hint:
                kwargs["login_hint"] = login_hint
            creds = flow.run_local_server(port=0, **kwargs)
            _save_token(creds, account)
            return creds

    # Fall back to service account (limited to explicitly shared files)
    env_sa = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if env_sa and Path(env_sa).exists():
        logger.debug("Using service account from GOOGLE_APPLICATION_CREDENTIALS")
        return sa_module.Credentials.from_service_account_file(env_sa, scopes=SCOPES)

    sa_file = _find_file("service_account.json")
    if sa_file:
        logger.debug("Using service account from %s (limited to shared files)", sa_file)
        return sa_module.Credentials.from_service_account_file(
            str(sa_file), scopes=SCOPES
        )

    raise click.ClickException(
        f"Account '{account}' is not authenticated.\n"
        f"  Run in a real terminal:\n"
        f"  gdrive auth login --account {account}"
    )


def _save_token(creds: Credentials, account: str) -> None:
    """Persist OAuth2 token to disk for a named account."""
    token_file = _token_path(account)
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(creds.to_json())


def _drive_service(account: str):
    """Build an authenticated Google Drive API v3 client."""
    return build("drive", "v3", credentials=_get_credentials(account))


def _friendly_http_error(exc: HttpError, account: str | None = None) -> str:
    """Turn a Drive/Sheets HttpError into a one-screen CLI message."""
    message = getattr(exc, "reason", None) or str(exc)
    try:
        payload = json.loads(exc.content.decode("utf-8")) if exc.content else {}
        err = (payload.get("error") or {})
        details = err.get("details") or err.get("errors") or []
        reasons = [d.get("reason") for d in details if isinstance(d, dict)]
        message = err.get("message") or message
    except Exception:
        reasons = []
    account = account or _get_default_account()
    others = [a for a in _list_accounts() if a != account]
    if "accessNotConfigured" in reasons or "has not been used" in str(message):
        extra = ""
        if others:
            extra = (
                f"\n  This CLI is using account '{account}'. Other accounts:\n"
                + "\n".join(f"    gdrive --account {a} whoami" for a in others)
            )
        return (
            f"Google Drive API is not enabled for the OAuth project behind "
            f"account '{account}'.\n"
            f"  {message}\n"
            f"{extra}"
        )
    return f"Drive API error: {message}"


def _whoami(account: str) -> dict:
    """Return the local account name plus the Google identity for that token."""
    service = _drive_service(account)
    try:
        about = (
            service.about()
            .get(fields="user(displayName,emailAddress,permissionId,photoLink)")
            .execute()
        )
    except HttpError as exc:
        raise click.ClickException(_friendly_http_error(exc, account)) from exc
    user = about.get("user") or {}
    return {
        "account": account,
        "default": account == _get_default_account(),
        "email": user.get("emailAddress"),
        "displayName": user.get("displayName"),
        "permissionId": user.get("permissionId"),
        "photoLink": user.get("photoLink"),
    }


def _sheets_service(account: str):
    """Build an authenticated Google Sheets API v4 client."""
    return build("sheets", "v4", credentials=_get_credentials(account))


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------


def _escape_q(value: str) -> str:
    """Escape a value for a Drive v3 q= string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _normalize_modified_after(value: str) -> str:
    """Accept a date or datetime and return a Drive-legal ISO-8601 UTC string."""
    s = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return f"{s}T00:00:00Z"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", s):
        return s.replace(" ", "T") + "Z"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z", s):
        return s
    raise click.ClickException(
        f"Invalid --modified-after '{value}'. Use ISO-8601 UTC with Z, "
        f"e.g. 2026-08-22T07:10:19Z"
    )


def _all_drives_kwargs(all_drives: bool) -> dict:
    if not all_drives:
        return {}
    return {
        "includeItemsFromAllDrives": True,
        "supportsAllDrives": True,
        "corpora": "allDrives",
    }


def _build_search_query(
    name: str | None,
    raw_q: str | None,
    full_text: str | None,
    modified_after: str | None,
    mime_type: str | None,
    include_trashed: bool,
) -> str:
    """Compose a Drive v3 q= filter from CLI flags.

    `--q` is ANDed with the other flags. `trashed = false` is added unless the
    raw query already mentions `trashed` or `--include-trashed` is set.
    """
    parts: list[str] = []
    if raw_q:
        parts.append(f"({raw_q})")
    if name:
        parts.append(f"name contains '{_escape_q(name)}'")
    if full_text:
        parts.append(f"fullText contains '{_escape_q(full_text)}'")
    if modified_after:
        parts.append(f"modifiedTime > '{_normalize_modified_after(modified_after)}'")
    if mime_type:
        parts.append(f"mimeType = '{mime_type}'")
    if not (raw_q or name or full_text):
        raise click.ClickException(
            "Provide a name, --q, or --full-text.\n"
            "  gdrive drive search budget\n"
            "  gdrive drive search "
            "--q \"fullText contains 'foo' and trashed = false\" --json --limit 0"
        )
    if not include_trashed and not (raw_q and "trashed" in raw_q):
        parts.append("trashed = false")
    return " and ".join(parts)


def _iter_files(
    service,
    q: str,
    *,
    all_drives: bool = True,
    max_results: int | None = None,
    fields: str = DEFAULT_FILE_FIELDS,
) -> list[dict]:
    """Page through files.list. max_results=None means unlimited."""
    results: list[dict] = []
    page_token = None
    extra = _all_drives_kwargs(all_drives)
    while True:
        page_size = 100
        if max_results is not None:
            remaining = max_results - len(results)
            if remaining <= 0:
                break
            page_size = min(100, remaining)
        resp = (
            service.files()
            .list(
                q=q,
                fields=f"nextPageToken, files({fields})",
                pageToken=page_token,
                pageSize=page_size,
                **extra,
            )
            .execute()
        )
        results.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
        if max_results is not None and len(results) >= max_results:
            break
    if max_results is not None:
        return results[:max_results]
    return results


def _list_comments(service, file_id: str) -> list[dict]:
    comments: list[dict] = []
    page_token = None
    while True:
        resp = (
            service.comments()
            .list(
                fileId=file_id,
                fields=(
                    "nextPageToken, comments(id,content,author,createdTime,"
                    "modifiedTime,resolved,quotedFileContent,"
                    "replies(id,content,author,createdTime,modifiedTime))"
                ),
                pageToken=page_token,
                pageSize=100,
                includeDeleted=False,
            )
            .execute()
        )
        comments.extend(resp.get("comments", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return comments


def _list_files(folder_id: str, account: str, mime_type: str | None = None) -> list[dict]:
    """List files in a Drive folder, optionally filtered by MIME type."""
    service = _drive_service(account)
    query = f"'{folder_id}' in parents and trashed = false"
    if mime_type:
        query += f" and mimeType = '{mime_type}'"
    return _iter_files(
        service,
        query,
        fields="id, name, mimeType, modifiedTime, size",
    )


def _download_file(file_id: str, output_path: str, account: str) -> str:
    """Download a Drive file to a local path.

    Returns the output path for chaining.
    """
    service = _drive_service(account)
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)

    with open(output_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

    return output_path


def _extract_pdf_text(pdf_path: str) -> str:
    """Extract text from a local PDF using pymupdf.

    pymupdf handles most text-based PDFs well. For scanned/image PDFs,
    OCR (e.g. pytesseract) would be needed as a separate step.
    """
    import pymupdf

    doc = pymupdf.open(pdf_path)
    pages = []
    for page_num, page in enumerate(doc, start=1):
        text = page.get_text()
        if text.strip():
            pages.append(f"--- Page {page_num} ---\n{text}")
    doc.close()
    return "\n\n".join(pages)


def _download_and_extract_pdf(file_id: str, account: str) -> str:
    """Download a PDF from Drive and extract its text content."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        _download_file(file_id, tmp_path, account)
        return _extract_pdf_text(tmp_path)
    finally:
        os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Docs helpers
# ---------------------------------------------------------------------------


def _export_google_doc(file_id: str, mime_type: str, account: str) -> str:
    """Export a Google Docs document to the specified format via Drive API.

    Uses files.export() which works for any Google Workspace document
    (Docs, Sheets, Slides). The mime_type determines the output format.
    """
    service = _drive_service(account)
    content = service.files().export(fileId=file_id, mimeType=mime_type).execute()

    if isinstance(content, bytes):
        return content.decode("utf-8")
    return content


# ---------------------------------------------------------------------------
# CLI structure
# ---------------------------------------------------------------------------


@click.group()
@click.option("--debug", is_flag=True, help="Enable debug logging")
@click.option("--account", default=None, help="Account name (default: from config.json)")
@click.option(
    "--non-interactive/--interactive",
    "non_interactive",
    default=None,
    help="Never open a browser for OAuth (default: on when stdin is not a TTY)",
)
@click.pass_context
def cli(ctx, debug: bool, account: str | None, non_interactive: bool | None) -> None:
    """Google Drive gateway — docs, sheets, files, multi-account."""
    global _NON_INTERACTIVE
    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    # Migrate legacy config on first run
    _maybe_migrate_legacy_config()

    if non_interactive is None:
        _NON_INTERACTIVE = not sys.stdin.isatty()
    else:
        _NON_INTERACTIVE = non_interactive

    ctx.ensure_object(dict)
    ctx.obj["account_flag"] = account
    ctx.obj["account"] = account or _get_default_account()
    ctx.obj["non_interactive"] = _NON_INTERACTIVE


def _login_account(account_name: str | None, account_flag: str | None) -> str:
    """Account slot for `auth login`.

    Bare `gdrive auth login` always writes the `default` slot. A named
    account requires `--account` (top-level or on `auth login`).
    `auth set-default` only affects other commands, not login.
    """
    return account_name or account_flag or "default"


@cli.command("whoami")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
@click.pass_context
def whoami(ctx, as_json: bool) -> None:
    """Print the Google identity for the current (or --account) token.

    Examples:
        gdrive whoami
        gdrive --account foo whoami --json
    """
    info = _whoami(ctx.obj["account"])
    if as_json:
        click.echo(json.dumps(info, indent=2))
        return
    flag = " (default)" if info["default"] else ""
    email = info.get("email") or "?"
    name = info.get("displayName") or ""
    extra = f"  {name}" if name else ""
    console.print(f"{info['account']}{flag}  {email}{extra}")


# -- Auth subgroup ----------------------------------------------------------


@cli.group()
def auth() -> None:
    """Manage Google OAuth2 authentication."""


@auth.command("login")
@click.option("--account", "account_name", default=None, help="Account name to create/update")
@click.option("--login-hint", default=None, help="Email to pre-select in Google account chooser")
@click.pass_context
def auth_login(ctx, account_name: str | None, login_hint: str | None) -> None:
    """Authenticate with Google (opens browser).

    Creates a new named account or refreshes an existing one.
    Use --login-hint to pre-select the right Google account in the browser.
    Bare `gdrive auth login` always writes the `default` slot.
    """
    account = _login_account(account_name, ctx.obj.get("account_flag"))
    creds = _get_credentials(
        account, login_hint=login_hint, interactive=True, force=True
    )
    if not (creds and creds.valid):
        console.print("[red]Authentication failed.[/red]")
        return
    console.print(f"[green]Authenticated successfully (account: {account}).[/green]")
    try:
        info = _whoami(account)
    except click.ClickException as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        return
    email = info.get("email") or "?"
    name = info.get("displayName") or ""
    extra = f"  {name}" if name else ""
    flag = " (default)" if info["default"] else ""
    console.print(f"{info['account']}{flag}  {email}{extra}")


@auth.command("status")
@click.pass_context
def auth_status(ctx) -> None:
    """Check current authentication status."""
    account = ctx.obj["account"]
    token_file = _token_path(account)

    if token_file.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
            if creds.valid:
                console.print(f"[green]OAuth2 ({account}): authenticated (token valid)[/green]")
                return
            elif creds.expired and creds.refresh_token:
                console.print(f"[yellow]OAuth2 ({account}): token expired (will auto-refresh)[/yellow]")
                return
            else:
                console.print(f"[red]OAuth2 ({account}): token invalid. Run: auth login --account {account}[/red]")
        except Exception as exc:
            console.print(f"[red]OAuth2 ({account}): error reading token: {exc}[/red]")

    # Check service account fallback
    sa_file = _find_file("service_account.json")
    if sa_file:
        console.print(f"[yellow]Service account: {sa_file} (only sees shared files)[/yellow]")
        return

    console.print(f"[red]Account '{account}' not authenticated. Run: auth login --account {account}[/red]")


@auth.command("list")
def auth_list() -> None:
    """List all authenticated accounts."""
    accounts = _list_accounts()
    default = _get_default_account()

    if not accounts:
        console.print("[yellow]No authenticated accounts. Run: auth login[/yellow]")
        return

    table = Table(title="Authenticated Accounts")
    table.add_column("Account", style="cyan")
    table.add_column("Default", style="green")
    table.add_column("Token", style="dim")

    for name in accounts:
        is_default = "yes" if name == default else ""
        table.add_row(name, is_default, str(_token_path(name)))

    console.print(table)


@auth.command("set-default")
@click.argument("account_name")
def auth_set_default(account_name: str) -> None:
    """Set the default account."""
    token_file = _token_path(account_name)
    if not token_file.exists():
        raise click.ClickException(
            f"Account '{account_name}' not found. Authenticate first: auth login --account {account_name}"
        )
    config = _get_config()
    config["default_account"] = account_name
    _save_config(config)
    console.print(f"[green]Default account set to '{account_name}'.[/green]")


@auth.command("logout")
@click.pass_context
def auth_logout(ctx) -> None:
    """Remove stored credentials for the current account."""
    account = ctx.obj["account"]
    token_file = _token_path(account)
    if token_file.exists():
        token_file.unlink()
        console.print(f"[green]Credentials removed for account '{account}'.[/green]")
    else:
        console.print(f"[yellow]No stored credentials for account '{account}'.[/yellow]")


# -- Docs subgroup ----------------------------------------------------------


@cli.group()
def docs() -> None:
    """Google Docs operations."""


@docs.command("read")
@click.option("--doc-id", default=None, help="Google Doc ID")
@click.option("--url", default=None, help="Full Google Docs URL")
@click.option(
    "--format",
    "output_format",
    default="text",
    type=click.Choice(["text", "markdown", "html"]),
    help="Export format (default: text)",
)
@click.pass_context
def docs_read(ctx, doc_id: str | None, url: str | None, output_format: str) -> None:
    """Read a Google Doc's content.

    Accepts either --doc-id or --url (which extracts the ID automatically).
    Uses the Drive API's export endpoint to retrieve the document content.
    """
    if not doc_id and not url:
        raise click.ClickException("Provide either --doc-id or --url")

    file_id = _parse_google_id(url) if url else doc_id
    account = ctx.obj["account"]
    mime_type = DOC_EXPORT_FORMATS[output_format]

    try:
        content = _export_google_doc(file_id, mime_type, account)
    except Exception:
        # Markdown export may not be available — fall back to plain text
        if output_format == "markdown":
            logger.warning("Markdown export failed, falling back to plain text")
            content = _export_google_doc(file_id, "text/plain", account)
        else:
            raise

    console.print(content)


# -- Drive subgroup ---------------------------------------------------------


@cli.group()
def drive() -> None:
    """Google Drive operations."""


@drive.command("ls")
@click.option("--folder-id", required=True, help="Drive folder ID")
@click.option("--mime-type", default=None, help="Filter by MIME type (e.g. application/pdf)")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
@click.pass_context
def drive_ls(ctx, folder_id: str, mime_type: str | None, as_json: bool) -> None:
    """List files in a Drive folder.

    Examples:
        drive ls --folder-id 1AbC...
        drive ls --folder-id 1AbC... --mime-type application/pdf --json
    """
    account = ctx.obj["account"]
    files = _list_files(folder_id, account, mime_type)

    if as_json:
        click.echo(json.dumps({"q": f"'{folder_id}' in parents", "n": len(files), "files": files}, indent=2))
        return

    if not files:
        console.print("[yellow]No files found.[/yellow]")
        return

    table = Table(title=f"Files in {folder_id}")
    table.add_column("Name", style="cyan")
    table.add_column("ID", style="dim")
    table.add_column("Type", style="green")
    table.add_column("Modified", style="yellow")

    for f in files:
        table.add_row(
            f["name"],
            f["id"],
            f.get("mimeType", ""),
            f.get("modifiedTime", ""),
        )

    console.print(table)


@drive.command("search")
@click.argument("query", required=False, default=None)
@click.option(
    "--q",
    "raw_q",
    default=None,
    help="Raw Drive v3 q= filter. Combined with other flags via AND.",
)
@click.option("--full-text", default=None, help="Adds fullText contains '…'")
@click.option(
    "--modified-after",
    default=None,
    help="ISO-8601 UTC (Z required or normalized). Adds modifiedTime > '…'",
)
@click.option(
    "--type",
    "file_type",
    default=None,
    type=click.Choice(["spreadsheet", "pdf", "doc", "folder", "any"]),
    help="Filter by file type (default: any)",
)
@click.option("--mime-type", default=None, help="Raw MIME type (overrides --type)")
@click.option("--include-trashed", is_flag=True, help="Do not add trashed = false")
@click.option(
    "--all-drives/--no-all-drives",
    default=True,
    help="Search My Drive + shared drives (default: on)",
)
@click.option(
    "--limit",
    "max_results",
    default=20,
    help="Max results. 0 = unlimited",
)
@click.option("--json", "as_json", is_flag=True, help="Emit {q, n, files} JSON")
@click.pass_context
def drive_search(
    ctx,
    query: str | None,
    raw_q: str | None,
    full_text: str | None,
    modified_after: str | None,
    file_type: str | None,
    mime_type: str | None,
    include_trashed: bool,
    all_drives: bool,
    max_results: int,
    as_json: bool,
) -> None:
    """Search Drive files by name, body, or a raw Drive v3 q= filter.

    Shared drives are included by default. `--limit 0` pages until empty.

    Examples:
        drive search budget
        drive search "tax 2024" --type spreadsheet
        drive search --full-text foo --type doc --modified-after 2026-01-01T00:00:00Z --json --limit 0
        drive search --q "(name contains 'foo' or name contains 'bar') and modifiedTime > '2026-01-01T00:00:00Z' and trashed = false" --json --limit 0
    """
    resolved_mime = mime_type
    if not resolved_mime and file_type and file_type != "any":
        resolved_mime = MIME_ALIASES[file_type]

    q = _build_search_query(
        name=query,
        raw_q=raw_q,
        full_text=full_text,
        modified_after=modified_after,
        mime_type=resolved_mime,
        include_trashed=include_trashed,
    )
    account = ctx.obj["account"]
    service = _drive_service(account)
    limit = None if max_results == 0 else max_results
    results = _iter_files(service, q, all_drives=all_drives, max_results=limit)

    if as_json:
        click.echo(json.dumps({"q": q, "n": len(results), "files": results}, indent=2))
        return

    if not results:
        console.print(f"[yellow]No files matching '{query or q}' found.[/yellow]")
        return

    table = Table(title=f"Search: '{query or q}' ({len(results)} results)")
    table.add_column("Name", style="cyan")
    table.add_column("ID", style="dim")
    table.add_column("Type", style="green")
    table.add_column("Modified", style="yellow")

    type_labels = {
        "application/vnd.google-apps.spreadsheet": "Sheet",
        "application/vnd.google-apps.document": "Doc",
        "application/vnd.google-apps.folder": "Folder",
        "application/vnd.google-apps.presentation": "Slides",
        "application/pdf": "PDF",
    }

    for f in results:
        mime = f.get("mimeType", "")
        table.add_row(
            f["name"],
            f["id"],
            type_labels.get(mime, mime.split("/")[-1] if mime else ""),
            f.get("modifiedTime", "")[:10],
        )

    console.print(table)


@drive.command("get")
@click.argument("file")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
@click.option("--permissions", "with_permissions", is_flag=True, help="Include sharing ACL")
@click.pass_context
def drive_get(ctx, file: str, as_json: bool, with_permissions: bool) -> None:
    """Get metadata (and optional permissions) for a file ID or URL.

    Examples:
        drive get 1AbC... --json
        drive get "https://docs.google.com/document/d/1AbC.../edit" --permissions --json
    """
    file_id = _parse_google_id(file)
    account = ctx.obj["account"]
    service = _drive_service(account)
    fields = DEFAULT_FILE_FIELDS + ", size, md5Checksum, sharingUser, capabilities"
    if with_permissions:
        fields += ", permissions(id,type,role,emailAddress,domain,displayName,allowFileDiscovery)"
    meta = (
        service.files()
        .get(fileId=file_id, fields=fields, supportsAllDrives=True)
        .execute()
    )
    if as_json:
        click.echo(json.dumps(meta, indent=2))
        return

    table = Table(title=meta.get("name") or file_id)
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    owners = ", ".join(
        o.get("emailAddress") or o.get("displayName") or "?"
        for o in meta.get("owners", [])
    )
    rows = [
        ("id", meta.get("id", "")),
        ("mimeType", meta.get("mimeType", "")),
        ("modifiedTime", meta.get("modifiedTime", "")),
        ("owners", owners),
        ("sharedWithMeTime", meta.get("sharedWithMeTime", "")),
        ("webViewLink", meta.get("webViewLink", "")),
    ]
    for key, value in rows:
        table.add_row(key, str(value))
    console.print(table)
    if with_permissions:
        perms = meta.get("permissions") or []
        ptable = Table(title="Permissions")
        ptable.add_column("email/type")
        ptable.add_column("role")
        for p in perms:
            who = p.get("emailAddress") or p.get("domain") or p.get("type", "")
            ptable.add_row(who, p.get("role", ""))
        console.print(ptable)


@drive.command("comments")
@click.argument("file")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
@click.pass_context
def drive_comments(ctx, file: str, as_json: bool) -> None:
    """List comments and replies on a file ID or URL.

    Examples:
        drive comments 1AbC... --json
        drive comments "https://docs.google.com/document/d/1AbC.../edit"
    """
    file_id = _parse_google_id(file)
    account = ctx.obj["account"]
    service = _drive_service(account)
    comments = _list_comments(service, file_id)
    if as_json:
        click.echo(json.dumps({"id": file_id, "n": len(comments), "comments": comments}, indent=2))
        return
    if not comments:
        console.print("[yellow]No comments.[/yellow]")
        return
    for c in comments:
        author = (c.get("author") or {}).get("displayName", "?")
        resolved = " [resolved]" if c.get("resolved") else ""
        console.print(f"[cyan]{author}[/cyan] {c.get('createdTime', '')}{resolved}")
        console.print(c.get("content") or "")
        for r in c.get("replies") or []:
            rauthor = (r.get("author") or {}).get("displayName", "?")
            console.print(f"  [dim]{rauthor}[/dim] {r.get('content') or ''}")
        console.print()


@drive.command("download")
@click.option("--file-id", required=True, help="Drive file ID")
@click.option("--output", "-o", required=True, help="Local output path")
@click.pass_context
def drive_download(ctx, file_id: str, output: str) -> None:
    """Download a file from Drive."""
    account = ctx.obj["account"]
    path = _download_file(file_id, output, account)
    console.print(f"[green]Downloaded to {path}[/green]")


@drive.command("rename")
@click.argument("file_id")
@click.argument("new_title")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON")
@click.pass_context
def drive_rename(ctx, file_id: str, new_title: str, as_json: bool) -> None:
    """Rename a Drive file in place (changes its title/name, same file ID/URL).

    Only the title metadata changes — the file, its ID, and its share links are
    unchanged. Requires write access to the file.

    Examples:
        drive rename 1AbC... "foo bar notes"
    """
    account = ctx.obj["account"]
    service = _drive_service(account)
    before = (
        service.files()
        .get(fileId=file_id, fields="id, name", supportsAllDrives=True)
        .execute()
    )
    updated = (
        service.files()
        .update(
            fileId=file_id,
            body={"name": new_title},
            fields="id, name",
            supportsAllDrives=True,
        )
        .execute()
    )
    if as_json:
        console.print_json(
            data={"id": updated["id"], "old": before.get("name"), "new": updated["name"]}
        )
    else:
        console.print(
            f"[green]Renamed[/green] {file_id}\n  [dim]{before.get('name')}[/dim]\n  → [cyan]{updated['name']}[/cyan]"
        )


@drive.command("import")
@click.option("--input", "-i", "input_path", required=True, help="Local file to import (html, md, docx, txt, csv)")
@click.option("--title", default=None, help="Title for the created file (defaults to the input filename)")
@click.option(
    "--type",
    "google_type",
    type=click.Choice(["doc", "sheet", "slides"]),
    default="doc",
    help="Target native Google type (default: doc)",
)
@click.option("--parent-id", default=None, help="Destination folder ID (default: My Drive root)")
@click.pass_context
def drive_import(ctx, input_path: str, title: str | None, google_type: str, parent_id: str | None) -> None:
    """Upload a local file and convert it to a native Google Doc/Sheet/Slides.

    Drive performs the format conversion on import, so a styled .html or .docx
    becomes a real Google Doc (headings, tables, shaded cells all preserved).
    """
    account = ctx.obj["account"]
    src = Path(input_path)
    if not src.exists():
        raise click.ClickException(f"Input file not found: {src}")

    # Source MIME by extension; Drive converts these into the target Google type.
    source_mimes = {
        ".html": "text/html",
        ".htm": "text/html",
        ".md": "text/markdown",
        ".txt": "text/plain",
        ".csv": "text/csv",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".rtf": "application/rtf",
    }
    target_mimes = {
        "doc": "application/vnd.google-apps.document",
        "sheet": "application/vnd.google-apps.spreadsheet",
        "slides": "application/vnd.google-apps.presentation",
    }
    source_mime = source_mimes.get(src.suffix.lower())
    if not source_mime:
        raise click.ClickException(
            f"Unsupported input extension '{src.suffix}'. Supported: {', '.join(sorted(source_mimes))}"
        )

    service = _drive_service(account)
    body = {"name": title or src.stem, "mimeType": target_mimes[google_type]}
    if parent_id:
        body["parents"] = [parent_id]
    media = MediaFileUpload(str(src), mimetype=source_mime, resumable=False)
    created = service.files().create(body=body, media_body=media, fields="id,name,mimeType").execute()
    url_kind = {"doc": "document", "sheet": "spreadsheets", "slides": "presentation"}[google_type]
    console.print(f"[green]Created {created['name']}[/green]")
    console.print(f"https://docs.google.com/{url_kind}/d/{created['id']}/edit")


@drive.command("extract-pdf")
@click.option("--file-id", required=True, help="Drive PDF file ID")
@click.pass_context
def drive_extract_pdf(ctx, file_id: str) -> None:
    """Download a PDF from Drive and extract its text."""
    account = ctx.obj["account"]
    text = _download_and_extract_pdf(file_id, account)
    console.print(text)


@drive.command("extract-pdfs")
@click.option("--folder-id", required=True, help="Drive folder ID containing PDFs")
@click.pass_context
def drive_extract_pdfs(ctx, folder_id: str) -> None:
    """Extract text from all PDFs in a Drive folder."""
    account = ctx.obj["account"]
    files = _list_files(folder_id, account, mime_type="application/pdf")

    if not files:
        console.print("[yellow]No PDFs found in folder.[/yellow]")
        return

    for f in files:
        console.rule(f"[bold]{f['name']}[/bold]")
        try:
            text = _download_and_extract_pdf(f["id"], account)
            console.print(text)
        except Exception as exc:
            console.print(f"[red]Error extracting {f['name']}: {exc}[/red]")


# -- Sheets subgroup --------------------------------------------------------


@cli.group()
def sheets() -> None:
    """Google Sheets operations."""


@sheets.command("info")
@click.option("--spreadsheet-id", required=True, help="Spreadsheet ID")
@click.pass_context
def sheets_info(ctx, spreadsheet_id: str) -> None:
    """Get spreadsheet metadata (sheet names, grid dimensions)."""
    account = ctx.obj["account"]
    service = _sheets_service(account)
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()

    table = Table(title=meta.get("properties", {}).get("title", "Spreadsheet"))
    table.add_column("Sheet", style="cyan")
    table.add_column("ID", style="dim")
    table.add_column("Rows", style="green")
    table.add_column("Cols", style="green")

    for sheet in meta.get("sheets", []):
        props = sheet.get("properties", {})
        grid = props.get("gridProperties", {})
        table.add_row(
            props.get("title", ""),
            str(props.get("sheetId", "")),
            str(grid.get("rowCount", "")),
            str(grid.get("columnCount", "")),
        )

    console.print(table)


@sheets.command("read")
@click.option("--spreadsheet-id", required=True, help="Spreadsheet ID")
@click.option("--range", "cell_range", required=True, help="A1 range notation (e.g. Sheet1!A1:D10)")
@click.option("--json-output", is_flag=True, help="Output as JSON instead of table")
@click.pass_context
def sheets_read(ctx, spreadsheet_id: str, cell_range: str, json_output: bool) -> None:
    """Read values from a spreadsheet range."""
    account = ctx.obj["account"]
    service = _sheets_service(account)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=cell_range)
        .execute()
    )

    values = result.get("values", [])

    if not values:
        console.print("[yellow]No data found.[/yellow]")
        return

    if json_output:
        console.print_json(json.dumps(values, indent=2))
        return

    # Render as rich table — first row as headers if it looks like headers
    table = Table(title=cell_range)
    if values:
        for col_idx in range(len(values[0])):
            table.add_column(values[0][col_idx] if col_idx < len(values[0]) else "")
        for row in values[1:]:
            # Pad short rows with empty strings
            padded = row + [""] * (len(values[0]) - len(row))
            table.add_row(*[str(v) for v in padded])

    console.print(table)


@sheets.command("write")
@click.option("--spreadsheet-id", required=True, help="Spreadsheet ID")
@click.option("--range", "cell_range", required=True, help="A1 range notation")
@click.option("--values", required=True, help="JSON 2D array of values")
@click.option(
    "--input-option",
    default="USER_ENTERED",
    type=click.Choice(["RAW", "USER_ENTERED"]),
    help="How to interpret input (USER_ENTERED parses formulas)",
)
@click.pass_context
def sheets_write(
    ctx, spreadsheet_id: str, cell_range: str, values: str, input_option: str
) -> None:
    """Write values to a spreadsheet range.

    Values starting with '=' are interpreted as formulas when input-option
    is USER_ENTERED (the default).
    """
    account = ctx.obj["account"]
    service = _sheets_service(account)
    parsed_values = json.loads(values)

    result = (
        service.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=cell_range,
            valueInputOption=input_option,
            body={"values": parsed_values},
        )
        .execute()
    )

    updated = result.get("updatedCells", 0)
    console.print(f"[green]Updated {updated} cells in {cell_range}[/green]")


@sheets.command("write-formula")
@click.option("--spreadsheet-id", required=True, help="Spreadsheet ID")
@click.option("--range", "cell_range", required=True, help="Target cell (e.g. Sheet1!B2)")
@click.option("--formula", required=True, help="Formula string (e.g. =SUM(A1:A10))")
@click.pass_context
def sheets_write_formula(ctx, spreadsheet_id: str, cell_range: str, formula: str) -> None:
    """Write a single formula to a cell."""
    account = ctx.obj["account"]
    service = _sheets_service(account)

    # USER_ENTERED tells Sheets to interpret '=' as formula
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=cell_range,
        valueInputOption="USER_ENTERED",
        body={"values": [[formula]]},
    ).execute()

    console.print(f"[green]Formula written to {cell_range}[/green]")


@sheets.command("batch-write")
@click.option("--spreadsheet-id", required=True, help="Spreadsheet ID")
@click.option(
    "--data",
    required=True,
    help='JSON array of {range, values} objects',
)
@click.option(
    "--input-option",
    default="USER_ENTERED",
    type=click.Choice(["RAW", "USER_ENTERED"]),
    help="How to interpret input",
)
@click.pass_context
def sheets_batch_write(ctx, spreadsheet_id: str, data: str, input_option: str) -> None:
    """Write to multiple ranges in a single API call.

    Batch operations are preferred over many single writes to stay
    within Google API rate limits.
    """
    account = ctx.obj["account"]
    service = _sheets_service(account)
    parsed_data = json.loads(data)

    result = (
        service.spreadsheets()
        .values()
        .batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={
                "valueInputOption": input_option,
                "data": parsed_data,
            },
        )
        .execute()
    )

    total = result.get("totalUpdatedCells", 0)
    console.print(f"[green]Batch updated {total} cells across {len(parsed_data)} ranges[/green]")


def _a1_to_grid_range(service, spreadsheet_id: str, cell_range: str) -> dict:
    """Turn "\'Tab\'!B2:D10" into the GridRange that batchUpdate requests need.

    The Sheets API exposes no A1 resolver, so the tab title is looked up against
    the spreadsheet's own metadata rather than assumed to be the first sheet.
    """
    tab, _, span = cell_range.rpartition("!")
    tab = tab.strip().strip("'")
    meta = service.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheets_meta = [s["properties"] for s in meta.get("sheets", [])]
    if tab:
        match = [s for s in sheets_meta if s["title"] == tab]
        if not match:
            raise click.ClickException(
                f"no sheet named {tab!r}; have {', '.join(s['title'] for s in sheets_meta)}"
            )
        props = match[0]
    else:
        props = sheets_meta[0]

    def split(ref: str) -> tuple[str, str]:
        letters = "".join(c for c in ref if c.isalpha())
        digits = "".join(c for c in ref if c.isdigit())
        return letters.upper(), digits

    def col_index(letters: str) -> int:
        index = 0
        for char in letters:
            index = index * 26 + (ord(char) - ord("A") + 1)
        return index - 1

    start_ref, _, end_ref = span.partition(":")
    end_ref = end_ref or start_ref
    start_col, start_row = split(start_ref)
    end_col, end_row = split(end_ref)

    grid = {"sheetId": props["sheetId"]}
    # An omitted bound means "to the edge of the grid", matching A1 semantics
    # like G23:G — so the key is left out rather than defaulted to zero.
    if start_row:
        grid["startRowIndex"] = int(start_row) - 1
    if end_row:
        grid["endRowIndex"] = int(end_row)
    if start_col:
        grid["startColumnIndex"] = col_index(start_col)
    if end_col:
        grid["endColumnIndex"] = col_index(end_col) + 1
    return grid


@sheets.command("format")
@click.option("--spreadsheet-id", required=True, help="Spreadsheet ID")
@click.option("--range", "cell_range", required=True, help="A1 range notation")
@click.option("--wrap/--no-wrap", default=None, help="Wrap cell text (shows embedded newlines)")
@click.option(
    "--valign",
    type=click.Choice(["TOP", "MIDDLE", "BOTTOM"], case_sensitive=False),
    default=None,
    help="Vertical alignment",
)
@click.option("--autofit-rows", is_flag=True, help="Resize the range's rows to fit their content")
@click.option("--row-height", type=int, default=None, help="Force an exact row height in pixels")
@click.pass_context
def sheets_format(
    ctx,
    spreadsheet_id: str,
    cell_range: str,
    wrap: bool | None,
    valign: str | None,
    autofit_rows: bool,
    row_height: int | None,
) -> None:
    """Format a range: text wrapping, vertical alignment, row heights.

    Wrapping is what makes a newline inside a cell visible, and --autofit-rows
    then grows each row to its content. Run it after the write, since autofit
    measures whatever is in the cells at the time.
    """
    account = ctx.obj["account"]
    service = _sheets_service(account)
    grid = _a1_to_grid_range(service, spreadsheet_id, cell_range)

    requests: list[dict] = []
    cell_format: dict = {}
    if wrap is not None:
        cell_format["wrapStrategy"] = "WRAP" if wrap else "OVERFLOW_CELL"
    if valign:
        cell_format["verticalAlignment"] = valign.upper()
    if cell_format:
        requests.append({
            "repeatCell": {
                "range": grid,
                "cell": {"userEnteredFormat": cell_format},
                "fields": ",".join(f"userEnteredFormat.{k}" for k in cell_format),
            }
        })

    rows = {
        "sheetId": grid["sheetId"],
        "dimension": "ROWS",
        **{k: grid[k] for k in ("startRowIndex", "endRowIndex") if k in grid},
    }
    rows["startIndex"] = rows.pop("startRowIndex", 0)
    if "endRowIndex" in rows:
        rows["endIndex"] = rows.pop("endRowIndex")

    if row_height is not None:
        requests.append({
            "updateDimensionProperties": {
                "range": rows,
                "properties": {"pixelSize": row_height},
                "fields": "pixelSize",
            }
        })
    elif autofit_rows:
        requests.append({"autoResizeDimensions": {"dimensions": rows}})

    if not requests:
        raise click.ClickException("nothing to do: pass --wrap, --valign, --autofit-rows or --row-height")

    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id, body={"requests": requests}
    ).execute()

    # Report the resulting heights: an accepted autoResize request is not proof
    # the rows actually grew, and that is the whole point of calling it.
    meta = service.spreadsheets().get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId,title),data(rowMetadata(pixelSize)))",
        ranges=[cell_range],
    ).execute()
    heights = [
        row.get("pixelSize")
        for sheet in meta.get("sheets", [])
        for block in sheet.get("data", [])
        for row in block.get("rowMetadata", [])
        if row.get("pixelSize")
    ]
    summary = f"[green]Formatted {cell_range} ({len(requests)} request(s))[/green]"
    if heights:
        summary += (
            f" — row heights {min(heights)}-{max(heights)}px"
            f" across {len(heights)} row(s)"
        )
    console.print(summary)


# -- Pipeline subgroup ------------------------------------------------------


@cli.group()
def pipeline() -> None:
    """Compound operations: PDF extraction -> Sheets."""


@pipeline.command("pdf-to-table")
@click.option("--file-id", required=True, help="Drive PDF file ID")
@click.pass_context
def pipeline_pdf_to_table(ctx, file_id: str) -> None:
    """Extract text from a PDF and display as structured output.

    Outputs the raw extracted text — Claude can then parse this
    and decide what to write to Sheets.
    """
    account = ctx.obj["account"]
    text = _download_and_extract_pdf(file_id, account)
    console.print(text)


@pipeline.command("folder-summary")
@click.option("--folder-id", required=True, help="Drive folder ID")
@click.pass_context
def pipeline_folder_summary(ctx, folder_id: str) -> None:
    """Extract text from all PDFs in a folder and display summaries."""
    account = ctx.obj["account"]
    files = _list_files(folder_id, account, mime_type="application/pdf")

    if not files:
        console.print("[yellow]No PDFs found.[/yellow]")
        return

    # Summary table first
    table = Table(title="PDFs Found")
    table.add_column("#", style="dim")
    table.add_column("Name", style="cyan")
    table.add_column("ID", style="dim")

    for i, f in enumerate(files, start=1):
        table.add_row(str(i), f["name"], f["id"])

    console.print(table)
    console.print()

    # Then extract each
    for f in files:
        console.rule(f"[bold]{f['name']}[/bold]")
        try:
            text = _download_and_extract_pdf(f["id"], account)
            # Show first 2000 chars as preview
            preview = text[:2000]
            if len(text) > 2000:
                preview += f"\n\n[dim]... ({len(text) - 2000} more characters)[/dim]"
            console.print(preview)
        except Exception as exc:
            console.print(f"[red]Error: {exc}[/red]")
        console.print()


if __name__ == "__main__":
    try:
        cli()
    except HttpError as exc:
        click.echo(f"Error: {_friendly_http_error(exc)}", err=True)
        raise SystemExit(1) from exc
