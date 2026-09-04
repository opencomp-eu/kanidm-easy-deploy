"""Tests for scripts/apply.py."""

from __future__ import annotations

import base64
import subprocess
from pathlib import Path
import pytest
import yaml

from scripts.apply import (
    COMPOSE_PROJECT_NAME,
    DEFAULT_LOGO_PATH,
    admin_ui_caddy_block,
    admin_ui_domain,
    admin_ui_enabled,
    admin_ui_external_url,
    apply_oauth2_claim_maps,
    apply_portal_branding,
    branding_disabled,
    build_client_toml,
    build_server_toml,
    cli_exists,
    cli_json_field,
    default_display_name,
    discover_favicon_urls,
    ensure_admin_ui_admin_members,
    ensure_admin_ui_cookie_secret,
    ensure_admin_ui_service_account,
    image_type_for_path,
    oauth2_icons_enabled,
    remove_stale_oauth2_clients,
    render_caddyfile,
    resolve_image_source,
    resolve_oauth2_client_image,
    validate_image_file,
    write_compose_env,
    cli_ok,
    configured_groups,
    create_enrollment_link,
    derive_compose_files,
    ensure_kanidm_cli_state,
    ensure_ldap_mail_read,
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
    assert files == ["docker-compose.yml", "integrate.yml", "admin-ui.yml", "admin-ui-integrate.yml"]


def test_derive_compose_files_standalone():
    files = derive_compose_files(_base_config())
    assert files == ["docker-compose.yml", "caddy.yml", "admin-ui.yml"]


def test_derive_compose_files_omits_admin_ui_when_disabled():
    files = derive_compose_files(_base_config(admin_ui={"enabled": False}))
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


def test_ensure_ldap_mail_read_adds_service_account_to_mail_groups(monkeypatch):
    from scripts import apply as apply_module

    calls: list[tuple[str, ...]] = []

    def fake_cli(*args: str):
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(apply_module, "kanidm_cli", fake_cli)
    ensure_ldap_mail_read()
    assert (
        "group",
        "add-members",
        "idm_mail_servers",
        "stalwart-ldap",
        "--name",
        "idm_admin",
    ) in calls
    assert (
        "group",
        "add-members",
        "idm_people_pii_read",
        "stalwart-ldap",
        "--name",
        "idm_admin",
    ) in calls


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

    calls: list[tuple[str, ...]] = []

    def fake_cli(*args: str, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="This link: https://kanidm:8443/ui/reset?token=abc\n",
            stderr="",
        )

    monkeypatch.setattr(apply_module, "kanidm_cli", fake_cli)
    assert create_enrollment_link("alice", "auth.example.com") == (
        "https://auth.example.com/ui/reset?token=abc"
    )
    assert calls[0] == (
        "person",
        "credential",
        "create-reset-token",
        "alice",
        "--ttl",
        "86400",
        "--name",
        "idm_admin",
    )


def test_create_enrollment_link_falls_back_to_positional_ttl(monkeypatch):
    from scripts import apply as apply_module

    calls: list[tuple[str, ...]] = []

    def fake_cli(*args: str, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return subprocess.CompletedProcess(
                args=args,
                returncode=1,
                stdout="",
                stderr="unsupported",
            )
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="This link: https://kanidm:8443/ui/reset?token=xyz\n",
            stderr="",
        )

    monkeypatch.setattr(apply_module, "kanidm_cli", fake_cli)
    assert create_enrollment_link("alice", "auth.example.com") == (
        "https://auth.example.com/ui/reset?token=xyz"
    )
    assert calls[1] == (
        "person",
        "credential",
        "create-reset-token",
        "alice",
        "86400",
        "--name",
        "idm_admin",
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
    remove_stale_oauth2_clients({"stalwart", apply_module.ADMIN_UI_CLIENT_ID})
    assert calls == []


def test_default_display_name_strips_identity_host():
    assert default_display_name("auth.opencomp.eu") == "Opencomp"
    assert default_display_name("idm.example.com") == "Example"


def test_default_logo_is_small_svg():
    assert DEFAULT_LOGO_PATH.is_file()
    validate_image_file(DEFAULT_LOGO_PATH)
    assert image_type_for_path(DEFAULT_LOGO_PATH) == "svg"


def test_login_branding_assets_are_mounted():
    from scripts.apply import PROJECT_ROOT

    override_css = PROJECT_ROOT / "assets" / "branding" / "override.css"
    background = PROJECT_ROOT / "assets" / "branding" / "background.jpg"
    compose = (PROJECT_ROOT / "compose" / "docker-compose.yml").read_text()
    css = override_css.read_text()

    assert override_css.is_file()
    assert background.is_file()
    assert "../assets/branding/override.css:/hpkg/override.css:ro" in compose
    assert "../assets/branding/background.jpg:/hpkg/img/background.jpg:ro" in compose
    assert 'url("/pkg/img/background.jpg")' in css
    assert "filter: blur(" in css
    assert "main.form-signin .btn" in css
    assert "width: 100%" in css


def test_branding_disabled_values():
    assert branding_disabled(False)
    assert branding_disabled("off")
    assert not branding_disabled(True)
    assert not branding_disabled("yes")


def test_oauth2_icons_enabled_defaults_true():
    assert oauth2_icons_enabled({})
    assert oauth2_icons_enabled({"branding": {}})
    assert not oauth2_icons_enabled({"branding": {"oauth2_icons": False}})


def test_resolve_image_source_local_path(tmp_path: Path, monkeypatch):
    from scripts import apply as apply_module

    branding_dir = tmp_path / "branding"
    branding_dir.mkdir()
    logo = branding_dir / "logo.svg"
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
    monkeypatch.setattr(apply_module, "PROJECT_ROOT", tmp_path)
    resolved = resolve_image_source("branding/logo.svg", cache_name="test")
    assert resolved == logo.resolve()


def test_discover_favicon_urls_includes_origin_candidates():
    urls = discover_favicon_urls("https://cloud.example.com/app")
    assert "https://cloud.example.com/favicon.svg" in urls
    assert "https://cloud.example.com/favicon.ico" in urls


def test_resolve_image_source_skips_ico_downloads(tmp_path: Path, monkeypatch):
    from scripts import apply as apply_module

    branding_dir = tmp_path / "branding"
    branding_dir.mkdir()
    monkeypatch.setattr(apply_module, "BRANDING_DIR", branding_dir)

    def fake_download(url: str, dest: Path, *, max_bytes: int = 0) -> None:
        dest.write_bytes(b"\x00\x00\x01\x00" + b"\x00" * 16)

    monkeypatch.setattr(apply_module, "_download_url", fake_download)
    with pytest.raises(ValueError, match="ICO favicons are not supported"):
        resolve_image_source("https://matrix.example.com/favicon.ico", cache_name="matrix-ico")


def test_resolve_oauth2_client_image_honours_explicit_path(tmp_path: Path, monkeypatch):
    from scripts import apply as apply_module

    icon = tmp_path / "opencloud.png"
    icon.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    monkeypatch.setattr(apply_module, "PROJECT_ROOT", tmp_path)
    config = _base_config()
    client = {
        "client_id": "opencloud",
        "landing_url": "https://cloud.example.com",
        "image": "opencloud.png",
    }
    resolved = resolve_oauth2_client_image(config, client)
    assert resolved == icon.resolve()


def test_resolve_oauth2_client_image_skips_when_disabled(tmp_path: Path, monkeypatch):
    from scripts import apply as apply_module

    monkeypatch.setattr(apply_module, "fetch_landing_favicon", lambda *args, **kwargs: tmp_path / "x.png")
    config = _base_config(branding={"oauth2_icons": False})
    client = {"client_id": "opencloud", "landing_url": "https://cloud.example.com"}
    assert resolve_oauth2_client_image(config, client) is None


def test_apply_portal_branding_uses_defaults(monkeypatch):
    from scripts import apply as apply_module

    calls: list[tuple[str, ...]] = []

    def fake_cli(*args: str, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(apply_module, "kanidm_cli", fake_cli)
    monkeypatch.setattr(apply_module, "set_kanidm_domain_image", lambda path: calls.append(("set-image", str(path))))
    apply_portal_branding(_base_config())
    assert ("system", "domain", "set-displayname", "Test", "--name", "admin") in calls
    assert any(call[0] == "set-image" for call in calls)


def test_apply_portal_branding_skips_logo_when_disabled(monkeypatch):
    from scripts import apply as apply_module

    image_calls: list[Path] = []

    def fake_cli(*args: str, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(apply_module, "kanidm_cli", fake_cli)
    monkeypatch.setattr(apply_module, "set_kanidm_domain_image", lambda path: image_calls.append(path))
    apply_portal_branding(_base_config(branding={"display_name": "Acme", "logo": False}))
    assert image_calls == []


def test_admin_ui_enabled_by_default():
    assert admin_ui_enabled(_base_config())
    assert not admin_ui_enabled(_base_config(admin_ui={"enabled": False}))


def test_admin_ui_domain_defaults_to_org_subdomain():
    assert admin_ui_domain(_base_config()) == "admin.test.example"
    assert admin_ui_domain(_base_config(admin_ui={"domain": "console.example.com"})) == (
        "console.example.com"
    )


def test_admin_ui_external_url_is_https_origin():
    assert admin_ui_external_url(_base_config()) == "https://admin.test.example"


def test_oidc_clients_injects_confidential_admin_ui_client():
    clients = {item["client_id"]: item for item in oidc_clients(_base_config())}
    admin = clients["kanidm_admin_ui"]
    assert admin["landing_url"] == "https://admin.test.example"
    assert admin["redirect_uris"] == ["https://admin.test.example/api/auth/callback"]
    assert not admin.get("public")


def test_oidc_clients_admin_ui_disabled():
    config = _base_config(admin_ui={"enabled": False})
    assert all(item.get("client_id") != "kanidm_admin_ui" for item in oidc_clients(config))


def test_operator_client_overrides_admin_ui_defaults():
    config = _base_config(
        oidc={
            "enabled": True,
            "clients": [{"client_id": "kanidm_admin_ui", "landing_url": "https://console.other"}],
        }
    )
    admin = next(item for item in oidc_clients(config) if item["client_id"] == "kanidm_admin_ui")
    assert admin["landing_url"] == "https://console.other"
    assert admin["redirect_uris"] == ["https://admin.test.example/api/auth/callback"]


def test_validate_config_rejects_admin_ui_on_kanidm_domain():
    with pytest.raises(ValueError, match="differ"):
        validate_config(_base_config(admin_ui={"domain": "idm.test.example"}))


def test_admin_ui_caddy_block_proxies_to_container():
    block = admin_ui_caddy_block("admin.test.example")
    assert "admin.test.example" in block
    assert "reverse_proxy admin-ui:8080" in block


def test_render_caddyfile_toggles_admin_block(tmp_path, monkeypatch):
    from scripts import apply as apply_module

    template = tmp_path / "Caddyfile.template"
    template.write_text("{{IDM_DOMAIN_BLOCK}}\n{{ADMIN_UI_DOMAIN_BLOCK}}\n")
    output = tmp_path / "Caddyfile"
    monkeypatch.setattr(apply_module, "CADDY_TEMPLATE", template)
    monkeypatch.setattr(apply_module, "CADDYFILE", output)

    render_caddyfile(_base_config())
    text = output.read_text()
    assert "idm.test.example" in text
    assert "admin.test.example" in text
    assert "reverse_proxy admin-ui:8080" in text

    render_caddyfile(_base_config(admin_ui={"enabled": False}))
    assert "admin.test.example" not in output.read_text()


def test_caddyfile_template_keeps_both_placeholders():
    from scripts.apply import CADDY_TEMPLATE

    text = CADDY_TEMPLATE.read_text()
    assert "{{IDM_DOMAIN_BLOCK}}" in text
    assert "{{ADMIN_UI_DOMAIN_BLOCK}}" in text

def test_integration_fragment_includes_admin_block(tmp_path, monkeypatch):
    from scripts import apply as apply_module

    fragment = tmp_path / "caddy.caddy"
    monkeypatch.setattr(apply_module, "INTEGRATION_DIR", tmp_path)
    monkeypatch.setattr(apply_module, "INTEGRATION_CADDY_FRAGMENT", fragment)
    config = _base_config(proxy={"type": "caddy", "mode": "integrate"})
    apply_module.render_integration_fragment(config)
    text = fragment.read_text()
    assert "idm.test.example {" in text
    assert "admin.test.example {" in text

    apply_module.render_integration_fragment(_base_config(admin_ui={"enabled": False}))
    assert "admin.test.example" not in fragment.read_text()


def test_write_compose_env_emits_admin_ui_vars(tmp_path, monkeypatch):
    from scripts import apply as apply_module

    env_path = tmp_path / "compose.env"
    monkeypatch.setattr(apply_module, "COMPOSE_ENV_PATH", env_path)
    secrets = {
        "ADMIN_UI_API_TOKEN": "ui-token",
        "ADMIN_UI_COOKIE_SECRET": "cookie-secret",
        "OIDC_SECRET_KANIDM_ADMIN_UI": "oidc-secret",
    }

    write_compose_env(_base_config(), secrets)
    env = env_path.read_text()
    assert "ADMIN_UI_IMAGE=ghcr.io/opencomp-eu/kanidm-admin-ui:v0.1.1" in env
    assert "ADMIN_UI_KANIDM_URL=https://kanidm:8443" in env
    assert "ADMIN_UI_KANIDM_PUBLIC_URL=https://idm.test.example" in env
    assert "ADMIN_UI_EXTERNAL_URL=https://admin.test.example" in env
    assert "ADMIN_UI_ADMIN_GROUP=idm_admins" in env
    assert (
        "ADMIN_UI_OIDC_ISSUER_URL=https://idm.test.example/oauth2/openid/kanidm_admin_ui" in env
    )
    assert "ADMIN_UI_API_TOKEN=ui-token" in env
    assert "ADMIN_UI_COOKIE_SECRET=cookie-secret" in env
    assert "ADMIN_UI_OIDC_SECRET=oidc-secret" in env

    write_compose_env(_base_config(admin_ui={"enabled": False}), secrets)
    assert "ADMIN_UI" not in env_path.read_text()


def test_validate_config_rejects_admin_ui_on_kanidm_domain():
    with pytest.raises(ValueError, match="differ"):
        validate_config(
            _base_config(
                users=[
                    {
                        "username": "operator",
                        "display_name": "Admin",
                        "email": "admin@test.example",
                    }
                ],
                admin_ui={"domain": "idm.test.example"},
            )
        )


def test_ensure_admin_ui_service_account_creates_account_and_token(monkeypatch, tmp_path):
    from scripts import apply as apply_module

    monkeypatch.setattr(apply_module, "SECRETS_PATH", tmp_path / "secrets.yaml")
    calls: list[tuple[str, ...]] = []

    def fake_cli(*args: str):
        calls.append(args)
        if "service-account" in args and "get" in args:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="",
                stderr="ERROR kanidm_cli: Http(404, Some(NoMatchingEntries), \"abc\")",
            )
        if "generate" in args:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout='{"status":"Success","result":"ui-api-token"}\n',
                stderr="",
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(apply_module, "kanidm_cli", fake_cli)
    secrets: dict[str, str] = {}
    ensure_admin_ui_service_account(secrets)
    assert secrets["ADMIN_UI_API_TOKEN"] == "ui-api-token"
    assert (
        "service-account",
        "create",
        "admin_ui_svc",
        "Kanidm Admin UI",
        "idm_admins",
        "--name",
        "idm_admin",
    ) in calls
    assert ("group", "add-members", "idm_admins", "admin_ui_svc", "--name", "idm_admin") in calls
    generate = next(call for call in calls if "generate" in call)
    assert "--readwrite" in generate
    assert "admin_ui_svc" in generate


def test_ensure_admin_ui_service_account_skips_when_token_exists(monkeypatch):
    from scripts import apply as apply_module

    def fail_cli(*args: str):
        raise AssertionError("kanidm_cli should not be called when a token already exists")

    monkeypatch.setattr(apply_module, "kanidm_cli", fail_cli)
    secrets = {"ADMIN_UI_API_TOKEN": "existing-token"}
    ensure_admin_ui_service_account(secrets)
    assert secrets["ADMIN_UI_API_TOKEN"] == "existing-token"


def test_ensure_admin_ui_cookie_secret_is_stable_32_bytes(monkeypatch, tmp_path):
    from scripts import apply as apply_module

    monkeypatch.setattr(apply_module, "SECRETS_PATH", tmp_path / "secrets.yaml")
    secrets: dict[str, str] = {}
    ensure_admin_ui_cookie_secret(secrets)
    first = secrets["ADMIN_UI_COOKIE_SECRET"]
    assert len(base64.b64decode(first)) == 32
    ensure_admin_ui_cookie_secret(secrets)
    assert secrets["ADMIN_UI_COOKIE_SECRET"] == first


def test_ensure_admin_ui_admin_members_enrolls_initial_people(monkeypatch):
    from scripts import apply as apply_module

    calls: list[tuple[str, ...]] = []

    def fake_cli(*args: str):
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(apply_module, "kanidm_cli", fake_cli)
    ensure_admin_ui_admin_members(_base_config())
    assert ("group", "add-members", "idm_admins", "admin", "--name", "idm_admin") in calls


def test_disabled_admin_ui_client_is_removed_as_stale(tmp_path, monkeypatch):
    from scripts import apply as apply_module

    clients_dir = tmp_path / "oidc-clients.d"
    clients_dir.mkdir()
    (clients_dir / "kanidm_admin_ui.yaml").write_text("client_id: kanidm_admin_ui\n")
    calls: list[tuple[str, ...]] = []

    def fake_cli(*args: str):
        calls.append(args)
        if args[:3] == ("system", "oauth2", "get"):
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="name: kanidm_admin_ui\n", stderr=""
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(apply_module, "INTEGRATION_DIR", tmp_path)
    monkeypatch.setattr(apply_module, "kanidm_cli", fake_cli)
    remove_stale_oauth2_clients({"opencloud", "stalwart-webui"})
    assert not (clients_dir / "kanidm_admin_ui.yaml").exists()
    assert ("system", "oauth2", "delete", "kanidm_admin_ui", "--name", "idm_admin") in calls


def _write_legacy_self_signed_certificate(tmp_path: Path) -> None:
    """The original generator shape: one self-signed certificate, no local CA."""
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "30",
            "-nodes",
            "-keyout", str(tmp_path / "key.pem"),
            "-out", str(tmp_path / "chain.pem"),
            "-subj", "/CN=idm.test.example",
        ],
        check=True,
        capture_output=True,
    )


def _cert_text(path: Path) -> str:
    return subprocess.run(
        ["openssl", "x509", "-in", str(path), "-noout", "-text"],
        check=True,
        capture_output=True,
    ).stdout.decode()


def test_generate_tls_material_creates_ca_signed_certificate(tmp_path: Path):
    from scripts.apply import generate_tls_material

    assert generate_tls_material(tmp_path, "idm.test.example") is False
    assert (tmp_path / "ca.pem").is_file()
    assert (tmp_path / "ca-key.pem").stat().st_mode & 0o777 == 0o600
    text = _cert_text(tmp_path / "chain.pem")
    assert "CA:FALSE" in text
    assert "DNS:kanidm" in text
    assert "DNS:idm.test.example" in text
    assert subprocess.run(
        ["openssl", "verify", "-CAfile", str(tmp_path / "ca.pem"), str(tmp_path / "chain.pem")],
        check=True,
        capture_output=True,
    ).returncode == 0


def test_generate_tls_material_migrates_legacy_self_signed_certificate(tmp_path: Path):
    from scripts.apply import generate_tls_material

    _write_legacy_self_signed_certificate(tmp_path)
    assert generate_tls_material(tmp_path, "idm.test.example") is True
    assert "CA:FALSE" in _cert_text(tmp_path / "chain.pem")
    assert subprocess.run(
        ["openssl", "verify", "-CAfile", str(tmp_path / "ca.pem"), str(tmp_path / "chain.pem")],
        check=True,
        capture_output=True,
    ).returncode == 0


def test_generate_tls_material_keeps_existing_trusted_certificate(tmp_path: Path):
    from scripts.apply import generate_tls_material

    assert generate_tls_material(tmp_path, "idm.test.example") is False
    before = (tmp_path / "chain.pem").read_bytes()
    assert generate_tls_material(tmp_path, "idm.test.example") is False
    assert (tmp_path / "chain.pem").read_bytes() == before
