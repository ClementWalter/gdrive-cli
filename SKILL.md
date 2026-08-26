---
name: gdrive-cli
description: >
  Google Drive gateway. Use when the user wants to read Google Docs, find/search
  files in Google Drive, read spreadsheets, read PDFs, extract data, or
  fill/compute Google Sheets formulas. Triggers: "read google doc", "find
  spreadsheet", "search drive", "fill sheet from PDF", "compute sheet", "extract
  PDF to sheets", "gdrive", "read sheet", "update sheet", "google docs", "google
  drive".
allowed-tools:
  - Bash
  - Read
  - Write
  - Grep
  - Glob
---

# Google Drive Gateway

Read Google Docs, search Drive, extract PDFs, and read/write Google Sheets.
Supports multiple Google accounts.

**This CLI is the only Drive access path.** Do not use a Google Drive MCP
connector, a copied `drive_search.py`, or a bare `python3` that imports `google`
/ `googleapiclient` against the token. Search (including raw Drive v3 `q=`,
`fullText`, `modifiedTime`, shared drives), metadata, comments, reads,
downloads, renames, and Sheets all go through this CLI.

## How to invoke

Invoke it as **`gdrive`** — on `$PATH` via a symlink in `~/.local/bin` onto this
repo's `bin/gdrive`, so it always runs the current checkout: a `git pull`, or
even an uncommitted edit, takes effect immediately with nothing to reinstall.

```bash
gdrive --account foo --non-interactive auth status
```

Examples in this doc are written that way. If `gdrive` is not on `$PATH`, run
the bundled launcher `bin/gdrive` resolved against this skill's own directory
(PEP 723 — `uv` resolves deps inline on first run), or link it once:

```bash
ln -sfn <skill-dir>/bin/gdrive ~/.local/bin/gdrive
```

## Authentication

Uses OAuth2 with a desktop app flow. Supports multiple Google accounts.

```bash
# First-time setup: opens browser for Google OAuth consent.
# Bare login always writes the `default` slot (not auth set-default).
gdrive auth login

# Login with a named account (pre-selects Google account in browser)
gdrive auth login --account foo --login-hint foo@example.com

# List all authenticated accounts
gdrive auth list

# Google identity for the current (or --account) token
gdrive whoami
gdrive --account foo whoami --json

# Check auth status (default or specific account)
gdrive auth status
gdrive --account foo auth status

# Set default account
gdrive auth set-default foo

# Remove credentials for an account
gdrive --account foo auth logout
```

Credentials live in `~/.config/gdrive-cli/accounts/<name>/token.json`. The
first run copies any existing `~/.config/gdrive/` accounts into that directory.

### Multi-Account Usage

The `--account` flag goes on the top-level command (before the subcommand):

```bash
gdrive --account foo docs read --url "https://..."
gdrive --account foo drive search "budget"
gdrive --account bar sheets read --spreadsheet-id ID --range 'A1:D10'
```

Use a named account per Google identity. Tokens are not interchangeable
across Cloud projects or consent screens.

### Required OAuth Scopes

- `https://www.googleapis.com/auth/drive`
- `https://www.googleapis.com/auth/spreadsheets`

### Setting Up Google Cloud Credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create or select a project
3. Enable **Google Drive API** and **Google Sheets API**
4. Create **OAuth 2.0 Client ID** (Desktop application)
5. Download the JSON and save it under the per-user config dir — **never inside
   the skill directory, which is a git repo**:
   - default account: `~/.config/gdrive-cli/accounts/default/client_secret.json`
   - named account `<name>`:
     `~/.config/gdrive-cli/accounts/<name>/client_secret.json`

   The resolver also accepts `~/.config/gdrive-cli/client_secret.json` (shared)
   or `~/.config/gdrive-cli/client_secret_<name>.json`. Keeping secrets in
   `~/.config/gdrive-cli/` guarantees they can never be committed.

## Commands

### Docs Operations

```bash
# Read a Google Doc by URL (most common)
gdrive docs read --url "https://docs.google.com/document/d/ID/edit"

# Read by ID with specific format
gdrive docs read --doc-id ID --format text
gdrive docs read --doc-id ID --format markdown
gdrive docs read --doc-id ID --format html
```

### Drive Operations

```bash
# Name search (case-insensitive). Shared drives included by default.
gdrive drive search budget
gdrive drive search "tax 2024" --type spreadsheet
gdrive drive search invoices --type pdf

# Raw Drive v3 q=
gdrive drive search --q "(name contains 'foo' or name contains 'bar') and modifiedTime > '2026-01-01T00:00:00Z' and trashed = false" --json --limit 0

# fullText + modifiedTime + type flags
gdrive drive search --full-text foo --type doc --modified-after 2026-01-01T00:00:00Z --json --limit 0
gdrive drive search --full-text bar --type doc --modified-after 2026-01-01T00:00:00Z --json --limit 0

# Metadata / ACL / comments (ID or URL)
gdrive drive get 1AbC... --json
gdrive drive get "https://docs.google.com/document/d/1AbC.../edit" --permissions --json
gdrive drive comments 1AbC... --json

# List files in a folder
gdrive drive ls --folder-id <FOLDER_ID>
gdrive drive ls --folder-id <FOLDER_ID> --mime-type application/pdf --json

# Download a file (binary) to local path
gdrive drive download --file-id <FILE_ID> --output /tmp/file.pdf

# Rename in place (same file ID / share URL)
gdrive drive rename <FILE_ID> "New title" --json

# Extract text from a PDF on Drive (downloads + extracts)
gdrive drive extract-pdf --file-id <FILE_ID>

# Extract text from all PDFs in a folder
gdrive drive extract-pdfs --folder-id <FOLDER_ID>
```

`--json` on `search` / `ls` emits `{q, n, files}` and is empty-safe (`n: 0`).
`--limit 0` pages until empty. `--modified-after` accepts a date, a
`YYYY-MM-DD HH:MM:SS`, or a full `…Z` ISO timestamp. Only `gdrive auth login`
opens a browser. A lapsed token on any other command fails with the
`auth login --account <name>` command to run in a real terminal.

### Sheets Operations

```bash
# Read a range from a spreadsheet
gdrive sheets read --spreadsheet-id <ID> --range 'Sheet1!A1:D10'

# Write values to a range
gdrive sheets write --spreadsheet-id <ID> --range 'Sheet1!A1' \
  --values '[["Name","Amount"],["Alice",100]]'

# Write a formula to a cell
gdrive sheets write-formula --spreadsheet-id <ID> --range 'Sheet1!B2' \
  --formula '=SUM(C2:C10)'

# Batch update multiple ranges
gdrive sheets batch-write --spreadsheet-id <ID> \
  --data '[{"range":"Sheet1!A1","values":[["hello"]]},{"range":"Sheet1!B1","values":[[42]]}]'

# Get spreadsheet metadata (sheet names, grid properties)
gdrive sheets info --spreadsheet-id <ID>

# Formatting: wrapping, vertical alignment, row heights
gdrive sheets format --spreadsheet-id <ID> --range 'Sheet1!G2:G50' --wrap --valign TOP
gdrive sheets format --spreadsheet-id <ID> --range 'Sheet1!A2:G50' --row-height 21
gdrive sheets format --spreadsheet-id <ID> --range 'Sheet1!A7:G7' --row-height 71
```

A `\n` inside a written value only shows up if the cell wraps, so pair
multi-line writes with `--wrap`. **`--autofit-rows` does not grow rows** —
Sheets' `autoResizeDimensions` leaves them at the default height and the content
stays clipped; use an explicit `--row-height` (roughly 21px per line) instead.
Heights persist, so reset a block to its baseline before growing individual
rows, or a row that loses content stays tall. `format` prints the resulting
pixel heights, so the outcome is verified rather than assumed.

### Pipeline: PDF -> Sheets

```bash
# Extract data from a PDF and display as structured table
gdrive pipeline pdf-to-table --file-id <PDF_FILE_ID>

# Extract data from all PDFs in a folder, show summary
gdrive pipeline folder-summary --folder-id <FOLDER_ID>
```

## Key Details

- **Auth**: OAuth2 desktop flow with per-account refresh token persistence
- **Config dir**: `~/.config/gdrive-cli/`
- **Multi-account**: Each account stored in `~/.config/gdrive-cli/accounts/<name>/`
- **Migration**: Auto-copies `~/.config/gdrive/` (and the older
  `~/.config/gdrive-sheets-compute/` single-token layout) on first run if the
  new dir has no accounts yet
- **PDF extraction**: Uses `pymupdf` for text extraction
- **Sheets formulas**: Use `write-formula` for single formulas, or include
  formula strings in `write`/`batch-write` values (prefix with `=`)
- **Rate limits**: Google API quotas apply; batch operations preferred over many
  single calls

## Programmatic Access via gspread (Python)

This is **not** a Drive file gateway. It is a sheets compute pipeline
(service-account `gspread`) for creating/populating spreadsheets. Drive file
search, reads, and renames stay on the CLI above.

When writing Python scripts that create/populate Google Sheets (e.g. with
`uv run`), use the `gspread` library with a service account key:

### Auth

```python
import gspread
from pathlib import Path

# Service account key — never inside this repo
gc = gspread.service_account(
    filename=Path.home() / ".config/gdrive-cli/service_account.json"
)
```

- Sheets created by the SA are private — always call
  `spreadsheet.share("", perm_type="anyone", role="writer")` to make them
  accessible.

### CRITICAL: Formulas, Not Hardcoded Values

**NEVER write pre-computed aggregated values to Google Sheets.** The entire
point of a Google Sheet (vs CSV/plot) is that the user wants to do further
computation and trace any number back to raw data.

Architecture for data pipelines:

1. **Raw data sheet**: write values only (the source of truth)
2. **Helper formula columns** in raw data: e.g. `=LEFT(A2,10)` for date
   extraction
3. **Aggregation sheets**: ALL formulas, zero hardcoded values
   - Column A: `=SORT(UNIQUE('Raw Data'!G2:G))` to extract unique periods
   - Other columns: `=SUMIFS(...)` / `=COUNTIFS(...)` referencing raw data
   - Cumulative: running sum `=G2+F3`
   - Guard blanks: `=IFERROR(IF(A2="","", <formula>), "")`
4. **Charts**: reference the formula-driven ranges

```python
# Write formulas with value_input_option="USER_ENTERED" so Sheets evaluates them
ws.update(
    values=[headers, *rows_with_formulas],
    range_name="A1",
    value_input_option="USER_ENTERED",
)
```

### CRITICAL: Cell Formatting (Currency, Dates, etc.)

**ALWAYS set proper number formats via batchUpdate.** Cells containing monetary
values must be formatted as currency. Dates as dates. Counts as integers.

Common format patterns:

- **Currency (USD)**: `{"type": "CURRENCY", "pattern": "$#,##0.00"}`
- **Currency (EUR)**: `{"type": "CURRENCY", "pattern": "\u20ac#,##0.00"}`
- **Integer**: `{"type": "NUMBER", "pattern": "#,##0"}`
- **Percentage**: `{"type": "PERCENT", "pattern": "0.00%"}`
- **Date**: `{"type": "DATE", "pattern": "yyyy-mm-dd"}`

### Pitfalls

- **Grid size**: Chart `anchorCell` must be within the grid. Always allocate
  extra rows/cols.
- **Sharing**: Service account sheets are private by default. Always share after
  creation.
- **value_input_option**: Must be `"USER_ENTERED"` for formulas to be evaluated.
- **Array formula spill**: `SORT(UNIQUE(...))` spills into rows below. Leave
  those cells empty.
