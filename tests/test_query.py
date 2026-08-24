import sys
from pathlib import Path

import click
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gdrive_cli as g


def test_normalize_modified_after_date():
    assert g._normalize_modified_after("2026-08-22") == "2026-08-22T00:00:00Z"


def test_normalize_modified_after_naive_datetime():
    assert g._normalize_modified_after("2026-08-22 07:10:19") == "2026-08-22T07:10:19Z"


def test_normalize_modified_after_zulu():
    assert g._normalize_modified_after("2026-08-22T07:10:19Z") == "2026-08-22T07:10:19Z"


def test_normalize_modified_after_rejects_garbage():
    with pytest.raises(click.ClickException):
        g._normalize_modified_after("not-a-date")


def test_escape_q():
    assert g._escape_q("O'Reilly") == r"O\'Reilly"


def test_build_search_query_name():
    assert g._build_search_query("Notes", None, None, None, None, False) == (
        "name contains 'Notes' and trashed = false"
    )


def test_build_search_query_raw_owns_trashed():
    q = g._build_search_query(
        None, "fullText contains 'foo' and trashed = false", None, None, None, False
    )
    assert q == "(fullText contains 'foo' and trashed = false)"


def test_build_search_query_fulltext_flags():
    q = g._build_search_query(
        None,
        None,
        "foo",
        "2026-01-01T00:00:00Z",
        "application/vnd.google-apps.document",
        False,
    )
    assert q == (
        "fullText contains 'foo' and modifiedTime > '2026-01-01T00:00:00Z' "
        "and mimeType = 'application/vnd.google-apps.document' and trashed = false"
    )


def test_build_search_query_requires_selector():
    with pytest.raises(click.ClickException):
        g._build_search_query(None, None, None, None, None, False)
