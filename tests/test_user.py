"""Creating a mail-users person also creates their Stalwart mailbox."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.user import mailbox_sync_command, stalwart_kit_root, sync_mailbox, wants_mailbox


def test_mailbox_sync_follows_mail_users_only():
    assert wants_mailbox(["opencloud-user", "mail-users"])
    assert wants_mailbox(["Mail-Users"])
    assert not wants_mailbox(["opencloud-user"])


def test_stalwart_kit_root_requires_a_sibling_deploy(tmp_path: Path):
    kanidm = tmp_path / "kanidm-easy-deploy"
    kanidm.mkdir()
    assert stalwart_kit_root(kanidm) is None
    stalwart = tmp_path / "stalwart-easy-deploy"
    (stalwart / "scripts").mkdir(parents=True)
    (stalwart / "deploy.yaml").write_text("stalwart: {}\n")
    (stalwart / "scripts" / "groupware.py").write_text("")
    assert stalwart_kit_root(kanidm) == stalwart


def test_sync_command_waits_so_a_new_person_is_not_skipped(tmp_path: Path):
    command = mailbox_sync_command(tmp_path)
    assert command[-2:] == ["scripts.groupware", "--wait"]
    assert "python" in Path(command[0]).name or command[0].endswith("uv") or "uv" in command[0]


def test_sync_uses_the_kit_venv_when_present(tmp_path: Path):
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    assert mailbox_sync_command(tmp_path) == [str(python), "-m", "scripts.groupware", "--wait"]


def test_sync_mailbox_reports_a_failed_stalwart_sync(tmp_path: Path):
    def fail(command, cwd, check):
        assert cwd == tmp_path
        assert "--wait" in command
        return subprocess.CompletedProcess(command, 1)

    with pytest.raises(RuntimeError, match="did not create the mailbox"):
        sync_mailbox(tmp_path, runner=fail)


def test_sync_mailbox_skips_when_stalwart_is_not_installed(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("scripts.user.stalwart_kit_root", lambda: None)
    called = []
    sync_mailbox(runner=lambda *args, **kwargs: called.append(args))
    assert called == []
