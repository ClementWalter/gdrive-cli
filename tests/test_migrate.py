import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gdrive_cli as g


def test_migrates_multi_account_gdrive_dir(tmp_path, monkeypatch):
    new = tmp_path / "gdrive-cli"
    old = tmp_path / "gdrive"
    foo = old / "accounts" / "foo"
    foo.mkdir(parents=True)
    (foo / "token.json").write_text('{"token": "foo"}')
    (foo / "client_secret.json").write_text('{"installed": {}}')
    (old / "config.json").write_text('{"default_account": "foo"}')

    monkeypatch.setattr(g, "CONFIG_DIR", new)
    monkeypatch.setattr(g, "LEGACY_CONFIG_DIRS", (old, tmp_path / "missing"))
    g._maybe_migrate_legacy_config()

    assert (new / "accounts" / "foo" / "token.json").read_text() == '{"token": "foo"}'
    assert (new / "accounts" / "foo" / "client_secret.json").exists()
    assert json.loads((new / "config.json").read_text())["default_account"] == "foo"


def test_migrates_flat_legacy_token(tmp_path, monkeypatch):
    new = tmp_path / "gdrive-cli"
    old = tmp_path / "gdrive-sheets-compute"
    old.mkdir()
    (old / "token.json").write_text('{"token": "legacy"}')

    monkeypatch.setattr(g, "CONFIG_DIR", new)
    monkeypatch.setattr(g, "LEGACY_CONFIG_DIRS", (tmp_path / "missing", old))
    g._maybe_migrate_legacy_config()

    assert (new / "accounts" / "default" / "token.json").read_text() == '{"token": "legacy"}'
    assert json.loads((new / "config.json").read_text())["default_account"] == "default"


def test_does_not_overwrite_existing_accounts(tmp_path, monkeypatch):
    new = tmp_path / "gdrive-cli"
    dest = new / "accounts" / "bar"
    dest.mkdir(parents=True)
    (dest / "token.json").write_text('{"token": "keep"}')

    old = tmp_path / "gdrive"
    foo = old / "accounts" / "foo"
    foo.mkdir(parents=True)
    (foo / "token.json").write_text('{"token": "new"}')

    monkeypatch.setattr(g, "CONFIG_DIR", new)
    monkeypatch.setattr(g, "LEGACY_CONFIG_DIRS", (old,))
    g._maybe_migrate_legacy_config()

    assert (dest / "token.json").read_text() == '{"token": "keep"}'
    assert not (new / "accounts" / "foo").exists()


class _FakeAbout:
    def get(self, fields=None):
        return self

    def execute(self):
        return {
            "user": {
                "displayName": "Foo Bar",
                "emailAddress": "foo@example.com",
                "permissionId": "123",
                "photoLink": "https://example.com/foo.png",
            }
        }


class _FakeService:
    def about(self):
        return _FakeAbout()


def test_whoami_maps_drive_about(monkeypatch):
    monkeypatch.setattr(g, "_drive_service", lambda account: _FakeService())
    monkeypatch.setattr(g, "_get_default_account", lambda: "foo")
    info = g._whoami("foo")
    assert info == {
        "account": "foo",
        "default": True,
        "email": "foo@example.com",
        "displayName": "Foo Bar",
        "permissionId": "123",
        "photoLink": "https://example.com/foo.png",
    }
    info = g._whoami("bar")
    assert info["account"] == "bar"
    assert info["default"] is False


def test_friendly_http_error_access_not_configured(monkeypatch):
    class _Resp:
        status = 403
        reason = "Forbidden"

    payload = json.dumps(
        {
            "error": {
                "message": "Google Drive API has not been used in project 123 before or it is disabled.",
                "errors": [{"reason": "accessNotConfigured"}],
            }
        }
    ).encode()
    exc = g.HttpError(_Resp(), payload)
    monkeypatch.setattr(g, "_list_accounts", lambda: ["default", "foo"])
    monkeypatch.setattr(g, "_get_default_account", lambda: "default")
    msg = g._friendly_http_error(exc, "default")
    assert "account 'default'" in msg
    assert "gdrive --account foo whoami" in msg
