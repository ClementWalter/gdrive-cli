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
"""`sheets read` output flags, against a stub Sheets service."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gdrive_cli as g


def stub_service(values):
    """Mimic service.spreadsheets().values().get(...).execute()."""
    get = lambda **kw: SimpleNamespace(execute=lambda: {"values": values})
    return SimpleNamespace(spreadsheets=lambda: SimpleNamespace(values=lambda: SimpleNamespace(get=get)))


def read(monkeypatch, flag, values):
    monkeypatch.setattr(g, "_sheets_service", lambda account: stub_service(values))
    args = ["--account", "zama", "sheets", "read", "--spreadsheet-id", "x", "--range", "A1:B2", flag]
    return CliRunner().invoke(g.cli, args)


@pytest.mark.parametrize("flag", ["--json", "--json-output"])
def test_json_flags_emit_values(monkeypatch, flag):
    assert json.loads(read(monkeypatch, flag, [["a", "b"]]).output) == [["a", "b"]]


def test_json_on_empty_range_is_empty_list(monkeypatch):
    assert json.loads(read(monkeypatch, "--json", []).output) == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, *sys.argv[1:]]))
