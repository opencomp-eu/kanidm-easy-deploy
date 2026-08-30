"""Tests for scripts/apply.py."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.apply import (
    COMPOSE_PROJECT_NAME,
    apply_oauth2_claim_maps,
    remove_stale_oauth2_clients,
    build_client_toml,
    build_server_toml,
    cli_exists,
    cli_json_field,
    cli_ok,
    configured_groups,
    create_enrollment_link,
    derive_compose_files,
    ensure_kanidm_cli_state,
    ensure_ldap_token,
    kanidm_client_config_path,
    kanidm_issuer_url,
    kanidm_origin,
    kanidm_portal_caddy_block,
    kanidm_tokens_path,
    ldap_base_dn,
    merge_oidc_clients,
    oidc_clients,
    org_mail_domain,
    parse_api_token,
    person_mail_address,
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


def test_org_mail_domain_strips_identity_host():
    assert org_mail_domain("auth.opencomp.eu") == "opencomp.eu"
    assert org_mail_domain("idm.example.com") == "example.com"
    assert org_mail_domain("opencomp.eu") == "opencomp.eu"


def test_person_mail_defaults_to_org_domain():
    assert person_mail_address("thomas", "", "auth.opencomp.eu") == "thomas@opencomp.eu"
    assert person_mail_address("thomas", "thomas@other.example", "auth.opencomp.eu") == (
        "thomas@other.example"
    )


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


def test_kanidm_cli_state_files(tmp_path: Path):
    ensure_kanidm_cli_state(tmp_path)
    assert kanidm_tokens_path(tmp_path).is_file()
    assert kanidm_tokens_path(tmp_path).read_text() == '{"instances": {}}\n'
    assert kanidm_client_config_path(tmp_path).name == "kanidm-client-config"


def test_kanidm_cli_state_repairs_old_empty_object(tmp_path: Path):
    kanidm_tokens_path(tmp_path).write_text("{}\n")
    ensure_kanidm_cli_state(tmp_path)
    assert kanidm_tokens_path(tmp_path).read_text() == '{"instances": {}}\n'


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


def test_cli_ok_rejects_logged_error_with_zero_exit_code():
    result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="",
        stderr="2026-08-30 ERROR kanidm_cli: Item not found",
    )
    assert not cli_ok(result)


def test_cli_exists_treats_nomatchingentries_as_missing():
    result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="",
        stderr=(
            "2026-08-30T14:19:15.455102Z ERROR kanidm_cli::serviceaccount: "
            "Error generating service account api token -> "
            'Http(404, Some(NoMatchingEntries), "6731ca60-d55d-409b-a5d3-87d3a8a693cb")'
        ),
    )
    assert not cli_exists(result)
    assert not cli_ok(result)


def test_cli_exists_accepts_existing_entry():
    result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="name: stalwart-ldap\nclass: service_account\n",
        stderr="",
    )
    assert cli_exists(result)


def test_parse_api_token_from_json_result():
    result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='{"status":"Success","result":"ldap-token-value"}\n',
        stderr="",
    )
    assert parse_api_token(result) == "ldap-token-value"


def test_ensure_ldap_token_creates_missing_account_before_generate(monkeypatch, tmp_path):
    from scripts import apply as apply_module

    secrets_path = tmp_path / "secrets.yaml"
    monkeypatch.setattr(apply_module, "SECRETS_PATH", secrets_path)
    calls: list[tuple[str, ...]] = []

    def fake_cli(*args: str):
        calls.append(args)
        joined = " ".join(args)
        if "service-account" in args and "get" in args:
            if any(call[:3] == ("service-account", "create", "stalwart-ldap") for call in calls):
                return subprocess.CompletedProcess(
                    args=args, returncode=0, stdout="name: stalwart-ldap\n", stderr=""
                )
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="",
                stderr="ERROR kanidm_cli::serviceaccount: Http(404, Some(NoMatchingEntries), \"abc\")",
            )
        if "service-account" in args and "create" in args:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")
        if "api-token" in args and "generate" in args:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout='{"status":"Success","result":"generated-ldap-token"}\n',
                stderr="",
            )
        raise AssertionError(f"unexpected kanidm_cli call: {joined}")

    monkeypatch.setattr(apply_module, "kanidm_cli", fake_cli)
    secrets: dict[str, str] = {}
    ensure_ldap_token(secrets)
    assert secrets["LDAP_TOKEN"] == "generated-ldap-token"
    assert ("service-account", "create", "stalwart-ldap", "Stalwart LDAP", "idm_admins") in [
        call[:5] for call in calls
    ]
    generate = next(call for call in calls if "generate" in call)
    assert generate[:3] == ("-o", "json", "service-account") or generate[0:2] == ("-o", "json")
    assert "stalwart-ldap" in generate
    assert yaml.safe_load(secrets_path.read_text())["LDAP_TOKEN"] == "generated-ldap-token"


def test_cli_ok_accepts_expected_idempotency_error():
    result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="",
        stderr="ERROR kanidm_cli: AttributeUniqueness",
    )
    assert cli_ok(result, "attributeuniqueness")


def test_cli_json_field_parses_api_token_result():
    result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='{"status":"Success","result":"token-value"}\n',
        stderr="WARN verify_ca set to false",
    )
    assert cli_json_field(result, "result", "token") == "token-value"


def test_cli_json_field_parses_multiline_nested_result():
    result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='{\n  "status": "Success",\n  "result": {\n    "token": "nested-token"\n  }\n}\n',
        stderr="",
    )
    assert cli_json_field(result, "result", "token") == "nested-token"


def test_cli_json_field_parses_oauth_secret():
    result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout='{"secret":"client-secret"}\n',
        stderr="",
    )
    assert cli_json_field(result, "secret", "result") == "client-secret"


def test_validate_config_rejects_reserved_person_name():
    with pytest.raises(ValueError, match="reserved"):
        validate_config(
            _base_config(
                users=[
                    {
                        "username": "admin",
                        "display_name": "Admin",
                        "email": "admin@test.example",
                    }
                ]
            )
        )


def test_create_enrollment_link_uses_public_origin(monkeypatch):
    from scripts import apply as apply_module

    result = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="This link: https://kanidm:8443/ui/reset?token=abc\n",
        stderr="",
    )
    monkeypatch.setattr(apply_module, "kanidm_cli", lambda *args: result)
    assert create_enrollment_link("alice", "auth.example.com") == (
        "https://auth.example.com/ui/reset?token=abc"
    )


def test_kanidm_cli_uses_password_env(monkeypatch):
    from scripts import apply as apply_module

    captured: dict[str, list] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(apply_module.subprocess, "run", fake_run)
    monkeypatch.setattr(
        apply_module,
        "load_config",
        lambda: _base_config(kanidm={"data_dir": str(Path("/var/lib/kanidm"))}),
    )
    monkeypatch.setattr(apply_module, "kanidm_cli_volume_mounts", lambda _dir: [])
    apply_module.kanidm_cli("login", "--name", "idm_admin", password="secret-pass")
    assert "-e" in captured["cmd"]
    assert "KANIDM_PASSWORD=secret-pass" in captured["cmd"]
    assert "-c" not in captured["cmd"]


def test_recover_account_parses_modern_scripting_output(monkeypatch):
    from scripts import apply as apply_module

    def fake_admin(name: str, subcommand: str, *, scripting=False, use_exec=True):
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"output":"generated-modern-password"}\n',
            stderr="",
        )

    monkeypatch.setattr(apply_module, "_kanidmd_admin", fake_admin)
    assert apply_module.recover_account("idm_admin") == "generated-modern-password"


def test_recover_account_retries_legacy_with_disable(monkeypatch):
    from scripts import apply as apply_module

    calls: list[tuple[str, str, bool]] = []

    def fake_admin(name: str, subcommand: str, *, scripting=False, use_exec=True):
        calls.append((subcommand, name, scripting))
        if scripting:
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="unsupported")
        legacy_recover_count = sum(
            command == "recover-account" and not scripted
            for command, _, scripted in calls
        )
        if subcommand == "recover-account" and legacy_recover_count == 1:
            return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="schema error")
        if subcommand == "disable-account":
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        if subcommand == "recover-account":
            return subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout='new_password: "generated-legacy-password"\n',
                stderr="",
            )
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")

    monkeypatch.setattr(apply_module, "_kanidmd_admin", fake_admin)
    assert apply_module.recover_account("idm_admin") == "generated-legacy-password"
    assert calls == [
        ("recover-account", "idm_admin", True),
        ("recover-account", "idm_admin", False),
        ("disable-account", "idm_admin", False),
        ("recover-account", "idm_admin", False),
    ]


def test_apply_oauth2_claim_maps_opencloud_defaults(monkeypatch):
    from scripts import apply as apply_module

    calls: list[tuple[str, ...]] = []

    def fake_cli(*args: str):
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(apply_module, "kanidm_cli", fake_cli)
    apply_oauth2_claim_maps("opencloud", {})
    assert (
        "system",
        "oauth2",
        "update-claim-map-join",
        "opencloud",
        "opencloudRoles",
        "array",
    ) in [call[:6] for call in calls]
    mapped_groups = {
        call[5] for call in calls if call[:4] == ("system", "oauth2", "update-claim-map", "opencloud")
    }
    assert mapped_groups == {"opencloud-admin", "opencloud-user", "opencloud-guest"}
    admin = next(
        call
        for call in calls
        if call[:6] == ("system", "oauth2", "update-claim-map", "opencloud", "opencloudRoles", "opencloud-admin")
    )
    assert "admin" in admin


def test_remove_stale_oauth2_clients_deletes_leftover_stalwart(tmp_path, monkeypatch):
    from scripts import apply as apply_module

    clients_dir = tmp_path / "oidc-clients.d"
    clients_dir.mkdir()
    (clients_dir / "stalwart.yaml").write_text("client_id: stalwart\n")
    calls: list[tuple[str, ...]] = []

    def fake_cli(*args: str):
        calls.append(args)
        if args[:3] == ("system", "oauth2", "get"):
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="name: stalwart\n", stderr=""
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(apply_module, "INTEGRATION_DIR", tmp_path)
    monkeypatch.setattr(apply_module, "kanidm_cli", fake_cli)
    remove_stale_oauth2_clients({"stalwart-webui", "opencloud"})
    assert not (clients_dir / "stalwart.yaml").exists()
    assert ("system", "oauth2", "delete", "stalwart", "--name", "idm_admin") in calls


def test_remove_stale_oauth2_clients_keeps_active_stalwart(tmp_path, monkeypatch):
    from scripts import apply as apply_module

    calls: list[tuple[str, ...]] = []

    def fake_cli(*args: str):
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(apply_module, "INTEGRATION_DIR", tmp_path)
    monkeypatch.setattr(apply_module, "kanidm_cli", fake_cli)
    remove_stale_oauth2_clients({"stalwart"})
    assert calls == []
