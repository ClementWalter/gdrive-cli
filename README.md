# gdrive-cli

Terminal access to Google Drive, Docs, and Sheets using a desktop OAuth token.
Search (including raw Drive v3 `q=`, `fullText`, `modifiedTime`, shared drives),
read docs, download files, rename, extract PDFs, and read/write Sheets.

The tool is exposed both as a standalone CLI and as a
[Claude Code](https://claude.com/claude-code) skill (see
[`SKILL.md`](SKILL.md)).

**This CLI is the only Drive access path.** Do not use a Google Drive MCP
connector, a copied `drive_search.py`, or a bare `python3` that imports
`google` / `googleapiclient` against the token.

## Prerequisites

[`uv`](https://docs.astral.sh/uv/) runs the script and resolves its Python
dependencies on demand:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# or via Homebrew
brew install uv
```

The `npx skills` install path additionally requires Node.js (for `npx`).

## Install as a Claude Code skill

Recommended path. Uses [`npx skills`](https://github.com/vercel-labs/skills)
to drop the skill into `~/.claude/skills/gdrive-cli/`:

```bash
npx skills add ClementWalter/gdrive-cli
```

After install, Claude Code picks it up automatically — see
[`SKILL.md`](SKILL.md) for what the skill exposes.

To use it from any directory, put the launcher on `$PATH` — the symlink points
at the checkout, so a `git pull` is all an upgrade takes:

```bash
ln -sfn ~/.claude/skills/gdrive-cli/bin/gdrive ~/.local/bin/gdrive
```

## Install as a standalone CLI

The CLI is a single-file Python script with
[PEP 723](https://peps.python.org/pep-0723/) inline metadata, so
[`uv`](https://docs.astral.sh/uv/) handles dependencies on the fly:

```bash
gdrive --help
```

To use it from any directory, put the launcher on `$PATH` — the symlink points
at the checkout, so a `git pull` is all an upgrade takes:

```bash
ln -sfn /path/to/gdrive-cli/bin/gdrive ~/.local/bin/gdrive
```

## Authentication

OAuth2 desktop flow. Per-account tokens live under `~/.config/gdrive-cli/`
(same shape as `notion-cli` / `slack-user-cli`). The first run copies any
existing `~/.config/gdrive/` accounts into that directory.

```bash
# First-time setup: opens browser for Google OAuth consent
gdrive auth login --account foo --login-hint foo@example.com

gdrive auth list
gdrive whoami
gdrive --account foo whoami --json
gdrive --account foo auth status
gdrive --account foo --non-interactive auth status
gdrive --account bar drive search "quarterly report"
```

Non-interactive sessions (no TTY, or `--non-interactive`) never open a
browser. A lapsed token fails with the `auth login` command to run in a
real terminal.

## Usage

```bash
# Name search. Shared drives included by default.
gdrive --account foo drive search budget
gdrive --account foo drive search "tax 2024" --type spreadsheet --json

# Raw Drive v3 q= / fullText / modifiedTime
gdrive --account foo drive search --q "(name contains 'foo' or name contains 'bar') and modifiedTime > '2026-01-01T00:00:00Z' and trashed = false" --json --limit 0
gdrive --account foo drive search --full-text foo --type doc --modified-after 2026-01-01T00:00:00Z --json --limit 0

# Read / metadata / comments / rename
gdrive --account foo docs read --url "https://docs.google.com/document/d/ID/edit" --format markdown
gdrive --account foo drive get ID --json --permissions
gdrive --account foo drive comments ID --json
gdrive --account foo drive rename ID "New title" --json
```
