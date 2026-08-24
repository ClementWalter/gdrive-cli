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

OAuth2 desktop flow. Per-account tokens live under `~/.config/gdrive/`
(that directory name is historical — do not rename it; existing tokens are
there). The `zama` account is the work Google account; `default` is a
personal Budget project and cannot see work files.

```bash
# First-time setup: opens browser for Google OAuth consent
gdrive auth login --account zama --login-hint you@company.com

gdrive auth list
gdrive --account zama auth status
gdrive --account zama --non-interactive auth status
```

Non-interactive sessions (no TTY, or `--non-interactive`) never open a
browser. A lapsed token fails with the `auth login` command to run in a
real terminal.

## Usage

```bash
# Name search. Shared drives included by default.
gdrive drive search budget
gdrive drive search "tax 2024" --type spreadsheet --json

# Raw Drive v3 q= / fullText / modifiedTime
gdrive drive search --q "(name contains 'Notes by Gemini' or name contains 'Notes from') and modifiedTime > '2026-08-22T07:10:19Z' and trashed = false" --json --limit 0
gdrive drive search --full-text Vault --type doc --modified-after 2026-08-22T07:10:19Z --json --limit 0

# Read / metadata / comments / rename
gdrive docs read --url "https://docs.google.com/document/d/ID/edit" --format markdown
gdrive drive get ID --json --permissions
gdrive drive comments ID --json
gdrive drive rename ID "New title" --json
```
