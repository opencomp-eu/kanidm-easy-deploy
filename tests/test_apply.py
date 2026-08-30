"""Tests for scripts/apply.py."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.apply import (
    COMPOSE_PROJECT_NAME,
    build_client_toml,
    build_server_toml,
    configured_groups,
    derive_compose_files,
    kanidm_issuer_url,
    kanidm_origin,
    kanidm_portal_caddy_block,
    ldap_base_dn,
    merge_oidc_clients,
    oidc_clients,
    render_template,
    validate_config,
)


def _base_config(**overrides) -> dict:
    config = {
        "kanidm": {
            "domain": "idm.test.example",
            "image": "docker.io/kanidm/server",
            "tag": "1.7.3",
            "data_dir": "/var/lib/kanidm",
            "ldap": True,
        },
        "proxy": {"type": "caddy", "mode": "standalone", "integrate": {"network": "easydeploy-net"}},
        "users": [
            {
                "username": "admin",
                "display_name": "Admin",
                "email": "admin@test.example",
                "groups": ["opencloud-admin"],
            }
        ],
        "groups": ["opencloud-admin", "mail-users"],
        "oidc": {"enabled": True, "clients": []},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and key in config and isinstance(config[key], dict):
            config[key].update(value)
        else:
            config[key] = value
    return config


def test_validate_config_rejects_placeholder_domain():
    with pytest.raises(ValueError, match="kanidm.domain"):
        validate_config(_base_config(kanidm={"domain": "idm.example.com", "data_dir": "/x"}))


def test_compose_project_name_is_unique():
    assert COMPOSE_PROJECT_NAME == "kanidm-easy-deploy"
    assert COMPOSE_PROJECT_NAME != "compose"


def test_kanidm_issuer_is_per_client():
    assert kanidm_origin("idm.example") == "https://idm.example"
    assert kanidm_issuer_url("idm.example", "opencloud") == "https://idm.example/oauth2/openid/opencloud"
    assert kanidm_issuer_url("idm.example", "matrix") == "https://idm.example/oauth2/openid/matrix"


def test_ldap_base_dn_from_domain():
    assert ldap_base_dn("idm.example.com") == "dc=idm,dc=example,dc=com"
    assert ldap_base_dn("auth.opencomp.eu") == "dc=auth,dc=opencomp,dc=eu"


def test_build_server_toml_includes_ldap_and_origin():
    text = build_server_toml(_base_config())
    assert 'domain = "idm.test.example"' in text
    assert 'origin = "https://idm.test.example"' in text
    assert 'ldapbindaddress = "[::]:3636"' in text
    assert "trust_x_forward_for = false" in text


def test_build_client_toml_points_at_container():
    text = build_client_toml("idm.test.example")
    assert "https://kanidm:8443" in text
    assert "verify_ca = false" in text


def test_derive_compose_files_integrate():
    files = derive_compose_files(_base_config(proxy={"type": "caddy", "mode": "integrate"}))
    assert files == ["docker-compose.yml", "integrate.yml"]


def test_derive_compose_files_standalone():
    files = derive_compose_files(_base_config())
    assert files == ["docker-compose.yml", "caddy.yml"]


def test_merge_oidc_clients_operator_overrides_engine():
    engine = [{"client_id": "opencloud", "public": True, "landing_url": "https://cloud.example"}]
    operator = [{"client_id": "opencloud", "landing_url": "https://cloud.other"}]
    merged = merge_oidc_clients(engine, operator)
    assert len(merged) == 1
    assert merged[0]["public"] is True
    assert merged[0]["landing_url"] == "https://cloud.other"


def test_oidc_clients_includes_engine_sidecars(tmp_path, monkeypatch):
    from scripts import apply as apply_module

    clients_dir = tmp_path / "oidc-clients.d"
    clients_dir.mkdir()
    (clients_dir / "opencloud.yaml").write_text(
        yaml.safe_dump({"client_id": "opencloud", "public": True, "landing_url": "https://cloud.test"})
    )
    monkeypatch.setattr(apply_module, "INTEGRATION_DIR", tmp_path)
    clients = oidc_clients(_base_config())
    assert any(item.get("client_id") == "opencloud" for item in clients)


def test_configured_groups_includes_defaults_and_user_groups():
    groups = configured_groups(_base_config())
    assert "opencloud-admin" in groups
    assert "mail-users" in groups
    assert "opencloud-user" in groups


def test_caddy_block_proxies_https_to_kanidm():
    block = kanidm_portal_caddy_block("idm.test.example")
    assert "idm.test.example" in block
    assert "https://kanidm:8443" in block
    assert "tls_insecure_skip_verify" in block
    assert "header_up -X-Forwarded-For" in block
    assert "X-Forwarded-For {{remote" not in block


def test_render_template_requires_placeholders():
    with pytest.raises(ValueError, match="Unresolved"):
        render_template("{{MISSING}}", {})


def test_recover_account_retries_with_disable(monkeypatch):
    from scripts import apply as apply_module

    calls: list[tuple[str, str]] = []

    def fake_admin(name: str, subcommand: str, *, password=None, use_exec=True):
        calls.append((subcommand, name))
        if subcommand == "recover-account" and len(calls) == 1:
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="schema error")
        if subcommand == "disable-account":
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        if subcommand == "recover-account":
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")

    monkeypatch.setattr(apply_module, "_kanidmd_admin", fake_admin)
    apply_module.recover_account("idm_admin", "secret-pass")
    assert calls == [
        ("recover-account", "idm_admin"),
        ("disable-account", "idm_admin"),
        ("recover-account", "idm_admin"),
    ]
