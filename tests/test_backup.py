"""Tests for the shared backup/restore wiring (plan, entrypoints, apply)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from scripts import apply as apply_module
from scripts.apply import (
    PROJECT_ROOT,
    backup_secret_keys,
    load_or_create_secrets,
    validate_backup_config,
)
from scripts.backup_plan import resolve_plan


def _write_deploy(tmp_path: Path, config: dict) -> Path:
    deploy_path = tmp_path / "deploy.yaml"
    deploy_path.write_text(yaml.safe_dump(config))
    return deploy_path


# --------------------------------------------------------------- plan ---


def test_resolve_plan_defaults_data_dir(tmp_path: Path):
    plan = resolve_plan(tmp_path)

    assert plan["service"] == "kanidm"
    assert plan["state_dir"] == ".kanidm-easy-deploy"
    assert plan["secrets_file"] == ".kanidm-easy-deploy/secrets.yaml"
    assert plan["timer_name"] == "kanidm-easy-deploy-backup"
    assert plan["hooks"] == {"apply": "apply.sh", "stop": "stop.sh", "start": "start.sh"}
    assert plan["persistent_paths"] == [
        "deploy.yaml",
        ".kanidm-easy-deploy",
        {"path": "/var/lib/kanidm", "as": "data/kanidm"},
    ]


def test_resolve_plan_uses_configured_data_dir(tmp_path: Path):
    _write_deploy(tmp_path, {"kanidm": {"domain": "idm.example.com", "data_dir": "/srv/kanidm"}})

    plan = resolve_plan(tmp_path)

    assert plan["persistent_paths"][-1] == {"path": "/srv/kanidm", "as": "data/kanidm"}


def test_kit_plan_loads_through_shared_lib():
    sys.path.insert(0, str(PROJECT_ROOT / "easydeploy-lib" / "python"))
    try:
        import backup_plan
    finally:
        sys.path.pop(0)

    plan = backup_plan.load_plan(PROJECT_ROOT)

    assert plan["service"] == "kanidm"
    assert plan["archive_prefix"] == "kanidm"
    assert plan["timer_name"] == "kanidm-easy-deploy-backup"
    assert plan["hooks"] == {"apply": "apply.sh", "stop": "stop.sh", "start": "start.sh"}
    assert plan["databases"] == []
    assert plan["docker_volumes"] == []
    assert plan["persistent_paths"][-1]["as"] == "data/kanidm"


# ------------------------------------------------------------ secrets ---


def test_backup_secret_keys_only_when_enabled():
    assert backup_secret_keys({"backup": {"enabled": True}}) == ("BORG_PASSPHRASE",)
    assert backup_secret_keys({"backup": {"enabled": False}}) == ()
    assert backup_secret_keys({}) == ()


def test_load_or_create_secrets_generates_borg_passphrase(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_module, "SECRETS_PATH", tmp_path / "secrets.yaml")

    secrets = load_or_create_secrets({"backup": {"enabled": True}})

    assert secrets["BORG_PASSPHRASE"]
    assert secrets["IDM_ADMIN_PASSWORD"]

    # Existing value is preserved on the next run.
    again = load_or_create_secrets({"backup": {"enabled": True}})
    assert again["BORG_PASSPHRASE"] == secrets["BORG_PASSPHRASE"]


def test_load_or_create_secrets_skips_borg_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_module, "SECRETS_PATH", tmp_path / "secrets.yaml")

    secrets = load_or_create_secrets({})

    assert "BORG_PASSPHRASE" not in secrets


# ------------------------------------------------- config validation ---


def test_validate_backup_config_rejects_relative_local_path(tmp_path: Path):
    deploy_path = _write_deploy(
        tmp_path,
        {"backup": {"enabled": True, "repository": {"type": "local", "path": "rel/path"}}},
    )

    with pytest.raises(ValueError, match="absolute path"):
        validate_backup_config(deploy_path)


def test_validate_backup_config_rejects_unknown_repo_type(tmp_path: Path):
    deploy_path = _write_deploy(
        tmp_path,
        {"backup": {"enabled": True, "repository": {"type": "usb", "path": "/tmp"}}},
    )

    with pytest.raises(ValueError, match="type must be"):
        validate_backup_config(deploy_path)


def test_validate_backup_config_rejects_missing_sftp_key(tmp_path: Path):
    deploy_path = _write_deploy(
        tmp_path,
        {
            "backup": {
                "enabled": True,
                "repository": {
                    "type": "sftp",
                    "host": "backup.example.com",
                    "user": "borg",
                    "path": "/repos/kanidm",
                    "ssh_key_path": str(tmp_path / "no-such-key"),
                },
            }
        },
    )

    with pytest.raises(ValueError, match="ssh_key_path not found"):
        validate_backup_config(deploy_path)


def test_validate_backup_config_ignores_missing_or_disabled(tmp_path: Path):
    validate_backup_config(tmp_path / "absent.yaml")  # no deploy.yaml yet

    deploy_path = _write_deploy(tmp_path, {"backup": {"enabled": False}})
    validate_backup_config(deploy_path)  # disabled blocks skip path checks


# ------------------------------------------------------------ schedule ---


def test_reconcile_backup_schedule_reports_not_configured(tmp_path, monkeypatch):
    deploy_path = _write_deploy(
        tmp_path,
        {"kanidm": {"domain": "idm.example.com", "data_dir": "/var/lib/kanidm"}},
    )
    monkeypatch.setattr(apply_module, "DEPLOY_PATH", deploy_path)

    message = apply_module.reconcile_backup_schedule()

    assert message == "Automatic backup timer not configured."


# --------------------------------------------------------- entrypoints ---


def _run_script(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(PROJECT_ROOT / script), *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )


def test_backup_sh_defers_local_repo_creation_to_shared_lib():
    text = (PROJECT_ROOT / "backup.sh").read_text()

    assert "ensure_local_repo_dir" not in text
    assert 'mkdir -p "${BACKUP_REPO_PATH}"' not in text
    assert "easydeploy_backup_repo_create" in text


def test_backup_sh_help_exits_zero():
    result = _run_script("backup.sh", "--help")

    assert result.returncode == 0
    assert "Usage:" in result.stdout


def test_backup_sh_rejects_unknown_argument():
    result = _run_script("backup.sh", "--wat")

    assert result.returncode == 1
    assert "Unknown argument" in result.stderr


def test_restore_sh_requires_a_source():
    result = _run_script("restore.sh")

    assert result.returncode == 1
    assert "Provide --archive" in result.stderr


def test_restore_sh_rejects_archive_and_file_together():
    result = _run_script("restore.sh", "--archive", "some-archive", "--file", "backup.tar.gz")

    assert result.returncode == 1
    assert "not both" in result.stderr


def test_restore_sh_rejects_archive_and_latest_together():
    result = _run_script("restore.sh", "--archive", "some-archive", "--latest")

    assert result.returncode == 1
    assert "not both" in result.stderr


def test_restore_sh_requires_flag_values():
    result = _run_script("restore.sh", "--file")

    assert result.returncode == 1
    assert "--file requires a value" in result.stderr
