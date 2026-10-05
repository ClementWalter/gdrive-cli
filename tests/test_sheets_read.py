"""`sheets read` output flags, against a stub Sheets service."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

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
