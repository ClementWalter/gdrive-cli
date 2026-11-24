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
"""Google Chat commands against a stub Chat service. No network."""

import json
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner
from google.oauth2.credentials import Credentials

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gdrive_cli as g


class _Call:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class StubChat:
    """The slice of the Chat v1 client the CLI calls, over in-memory spaces and messages.

    Pages hold two items so pagination is exercised; `filter` and `orderBy` follow the API's semantics.
    """

    PAGE = 2

    def __init__(self, spaces, messages=None):
        self._spaces = spaces
        self._messages = messages or {}
        self._mode = None

    def spaces(self):
        self._mode = "spaces"
        return self

    def messages(self):
        self._mode = "messages"
        return self

    def _page(self, items, token):
        start = int(token or 0)
        end = start + self.PAGE
        return {"page": items[start:end], "next": str(end) if end < len(items) else None}

    def list(self, pageToken=None, parent=None, filter=None, orderBy=None, pageSize=None):
        if self._mode == "spaces":
            page = self._page(self._spaces, pageToken)
            return _Call({"spaces": page["page"], "nextPageToken": page["next"]})
        items = list(self._messages.get(parent, []))
        if filter:
            cutoff = filter.split('"')[1]
            items = [m for m in items if m["createTime"] > cutoff]
        if orderBy == "createTime desc":
            items.reverse()
        page = self._page(items, pageToken)
        return _Call({"messages": page["page"], "nextPageToken": page["next"]})

    def get(self, name):
        return _Call(next(s for s in self._spaces if s["name"] == name))


def _space(sid, name, active, space_type="SPACE"):
    return {"name": f"spaces/{sid}", "displayName": name, "spaceType": space_type, "lastActiveTime": active,
            "membershipCount": {"joinedDirectHumanUserCount": 3}, "spaceUri": f"https://chat.google.com/room/{sid}"}


def _message(n, minute):
    return {"name": f"spaces/A/messages/{n}", "createTime": f"2026-11-24T10:{minute:02d}:00Z",
            "sender": {"displayName": f"Person {n}", "email": f"p{n}@example.com"}, "text": f"msg {n}",
            "thread": {"name": f"spaces/A/threads/{n}"}}


@pytest.fixture
def spaces():
    return [
        _space("OLD", "Weekly Sync - Nov 12", "2026-11-12T10:00:00Z"),
        _space("NEW", "Weekly Sync - Nov 17", "2026-11-17T10:00:00Z"),
        _space("TEAM", "Engineering", "2026-11-20T10:00:00Z"),
        _space("A", "Planning Review - Nov 24", "2026-11-24T11:00:00Z"),
    ]


@pytest.fixture
def stub(spaces):
    return StubChat(spaces, {"spaces/A": [_message(n, n) for n in range(5)]})


@pytest.fixture
def run(stub, monkeypatch):
    """Invoke the CLI with the stub as the Chat service."""
    monkeypatch.setattr(g, "_chat_service", lambda account: stub)
    monkeypatch.setattr(g, "_get_default_account", lambda: "default")
    monkeypatch.setattr(g, "_maybe_migrate_legacy_config", lambda: None)
    runner = CliRunner()
    return lambda *args: runner.invoke(g.cli, ["--non-interactive", *args])


@pytest.mark.parametrize("name", [
    "Team Weekly - Nov 18",
    "Sprint Demo - 20 Nov",
    "Sprint Demo - 25 Sept",
    "Backlog grooming - 4 Sept",
    "Weekly - 4 nov.",
    "Weekly sync  - Sep 8",
])
def test_meet_space_names_match(name):
    assert g._is_meet_space({"spaceType": "SPACE", "displayName": name})


@pytest.mark.parametrize("space", [
    {"spaceType": "SPACE", "displayName": "Engineering"},
    {"spaceType": "SPACE", "displayName": "Release - v2"},
    {"spaceType": "DIRECT_MESSAGE"},
    {"spaceType": "DIRECT_MESSAGE", "displayName": "Standup - Nov 18"},
])
def test_other_spaces_are_not_meet(space):
    assert not g._is_meet_space(space)


@pytest.mark.parametrize("ref, expected", [
    ("spaces/AAAAbCdEfGh", "spaces/AAAAbCdEfGh"),
    ("https://chat.google.com/room/AAAAbCdEfGh?cls=11", "spaces/AAAAbCdEfGh"),
    ("https://mail.google.com/chat/u/0/#chat/space/AAAAbCdEfGh", "spaces/AAAAbCdEfGh"),
    ("https://chat.google.com/u/1/app/chat/AAAAbCdEfGh", "spaces/AAAAbCdEfGh"),
    ("Planning Review", None),
])
def test_space_id(ref, expected):
    assert g._space_id(ref) == expected


def test_list_spaces_reads_every_page_most_recent_first(stub):
    assert [s["name"] for s in g._list_spaces(stub)] == ["spaces/A", "spaces/TEAM", "spaces/NEW", "spaces/OLD"]


def test_resolve_space_by_name_part(stub):
    assert g._resolve_space(stub, "planning review", latest=False)["name"] == "spaces/A"


def test_resolve_space_ambiguous_name_names_the_candidates(stub):
    with pytest.raises(click.ClickException, match="2 spaces match"):
        g._resolve_space(stub, "Weekly Sync", latest=False)


def test_resolve_space_latest_picks_most_recent(stub):
    assert g._resolve_space(stub, "Weekly Sync", latest=True)["name"] == "spaces/NEW"


def test_resolve_space_exact_name_wins_over_partial(stub):
    assert g._resolve_space(stub, "Weekly Sync - Nov 12", latest=False)["name"] == "spaces/OLD"


def test_resolve_space_unknown_name(stub):
    with pytest.raises(click.ClickException, match="No Chat space name contains"):
        g._resolve_space(stub, "nothing like it", latest=False)


def test_list_messages_all_oldest_first(stub):
    assert [m["text"] for m in g._list_messages(stub, "spaces/A", None, 0)] == [f"msg {n}" for n in range(5)]


def test_list_messages_limit_keeps_newest_in_order(stub):
    assert [m["text"] for m in g._list_messages(stub, "spaces/A", None, 3)] == ["msg 2", "msg 3", "msg 4"]


def test_list_messages_since(stub):
    assert [m["text"] for m in g._list_messages(stub, "spaces/A", "2026-11-24T10:02:00Z", 0)] == ["msg 3", "msg 4"]


def test_message_row_drive_attachment():
    message = {"attachment": [{"source": "DRIVE_FILE", "driveDataRef": {"driveFileId": "DOC1"}}]}
    assert g._message_row(message)["attachments"] == [{"name": None, "driveFileId": "DOC1"}]


def test_message_row_sender():
    message = {"sender": {"name": "users/1", "displayName": "Ada", "email": "ada@example.com"}}
    assert (g._message_row(message)["sender"], g._message_row(message)["email"]) == ("Ada", "ada@example.com")


def test_chat_service_refuses_token_without_chat_scopes(monkeypatch):
    creds = Credentials(token="t", scopes=["https://www.googleapis.com/auth/drive"])
    monkeypatch.setattr(g, "_get_credentials", lambda account: creds)
    with pytest.raises(click.ClickException, match="Connect it again"):
        g._chat_service("work")


def test_spaces_meet_json(run):
    result = run("chat", "spaces", "--meet", "--json")
    assert [s["name"] for s in json.loads(result.output)] == ["spaces/A", "spaces/NEW", "spaces/OLD"]


def test_spaces_query_and_since(run):
    result = run("chat", "spaces", "--query", "weekly", "--since", "2026-11-15", "--json")
    assert [s["name"] for s in json.loads(result.output)] == ["spaces/NEW"]


def test_read_json_by_name(run):
    result = run("chat", "read", "Planning Review", "--limit", "2", "--json")
    assert json.loads(result.output)["n"] == 2


def test_read_text_shows_sender_and_text(run):
    result = run("chat", "read", "spaces/A", "--limit", "1")
    assert "10:04 Person 4: msg 4" in result.output
