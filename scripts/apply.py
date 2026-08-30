#!/usr/bin/env python3
"""kanidm-easy-deploy configuration engine."""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "easydeploy-lib" / "python"))
import hostfs  # noqa: E402

COMPOSE_DIR = PROJECT_ROOT / "compose"
COMPOSE_PROJECT_NAME = "kanidm-easy-deploy"
STATE_DIR = PROJECT_ROOT / ".kanidm-easy-deploy"
SECRETS_PATH = STATE_DIR / "secrets.yaml"
COMPOSE_ENV_PATH = STATE_DIR / "compose.env"
DEPLOY_PATH = PROJECT_ROOT / "deploy.yaml"
CADDY_TEMPLATE = PROJECT_ROOT / "caddy" / "Caddyfile.template"
CADDYFILE = PROJECT_ROOT / "caddy" / "Caddyfile"
INTEGRATION_DIR = STATE_DIR / "integration"
INTEGRATION_CADDY_FRAGMENT = INTEGRATION_DIR / "caddy.caddy"
DEFAULT_INTEGRATE_NETWORK = "easydeploy-net"

SECRET_KEYS = (
    "IDM_ADMIN_PASSWORD",
    "ADMIN_PASSWORD",
    "LDAP_TOKEN",
)

DEFAULT_GROUPS = (
    "opencloud-admin",
    "opencloud-user",
    "opencloud-guest",
    "matrix-admins",
    "mail-users",
)

DEFAULT_OIDC_SCOPES = ["openid", "profile", "email", "groups", "groups_name"]


def to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def managed_is_false(section: dict | None) -> bool:
    value = (section or {}).get("managed")
    if value is False:
        return True
    return str(value or "").strip().lower() in {"false", "no", "0"}


def load_yaml(path: Path) -> dict:
    with path.open() as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be a mapping")
    return data


def save_yaml(path: Path, data: dict) -> None:
    path = hostfs.prepare_writable_file(path) if path.exists() or path.parent.exists() else path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        yaml.safe_dump(data, handle, default_flow_style=False, sort_keys=False)


def proxy_mode(config: dict) -> str:
    mode = str((config.get("proxy") or {}).get("mode") or "standalone").strip().lower()
    if mode not in {"standalone", "integrate"}:
        raise ValueError("proxy.mode must be 'standalone' or 'integrate'")
    return mode


def integrate_network_name(config: dict) -> str:
    integrate = (config.get("proxy") or {}).get("integrate") or {}
    name = str(integrate.get("network") or DEFAULT_INTEGRATE_NETWORK).strip()
    return name or DEFAULT_INTEGRATE_NETWORK


def load_config(path: Path = DEPLOY_PATH) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path.name}. Copy deploy.yaml.example to deploy.yaml or run wizard.sh."
        )
    return load_yaml(path)


def validate_config(config: dict) -> None:
    kanidm = config.get("kanidm") or {}
    if not isinstance(kanidm, dict):
        raise ValueError("kanidm section must be a mapping")

    domain = str(kanidm.get("domain") or "").strip()
    if not domain or domain == "idm.example.com":
        raise ValueError("kanidm.domain must be set to your real identity domain")

    data_dir = str(kanidm.get("data_dir") or "").strip()
    if not data_dir:
        raise ValueError("kanidm.data_dir must be set")

    proxy_type = (config.get("proxy") or {}).get("type", "caddy")
    if proxy_type != "caddy":
        raise ValueError("proxy.type must be 'caddy' in v1")

    users = config.get("users") or []
    if not isinstance(users, list) or not users:
        raise ValueError("users must contain at least one person for first-boot bootstrap")

    proxy_mode(config)


def kanidm_origin(domain: str) -> str:
    return f"https://{str(domain).strip().rstrip('/')}"


def kanidm_issuer_url(domain: str, client_id: str) -> str:
    """Kanidm uses a per-client OpenID issuer."""
    return f"{kanidm_origin(domain)}/oauth2/openid/{client_id}"


def ldap_base_dn(domain: str) -> str:
    labels = [part for part in str(domain).strip().lower().split(".") if part]
    if not labels:
        raise ValueError("cannot derive LDAP base DN from an empty domain")
    return ",".join(f"dc={label}" for label in labels)


def load_engine_oidc_clients() -> list[Any]:
    directory = INTEGRATION_DIR / "oidc-clients.d"
    if not directory.is_dir():
        return []
    clients: list[Any] = []
    for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
        data = load_yaml(path)
        if isinstance(data, dict) and data.get("client_id"):
            clients.append(data)
    return clients


def merge_oidc_clients(engine_clients: list[Any], operator_clients: list[Any]) -> list[Any]:
    by_id: dict[str, dict[str, Any]] = {}
    for client in engine_clients:
        if not isinstance(client, dict):
            continue
        client_id = str(client.get("client_id") or "").strip()
        if client_id:
            by_id[client_id] = dict(client)
    for client in operator_clients:
        if not isinstance(client, dict):
            continue
        client_id = str(client.get("client_id") or "").strip()
        if not client_id:
            continue
        if client_id in by_id:
            merged = dict(by_id[client_id])
            merged.update({key: value for key, value in client.items() if value not in (None, "")})
            by_id[client_id] = merged
        else:
            by_id[client_id] = dict(client)
    return list(by_id.values())


def oidc_clients(config: dict) -> list[Any]:
    oidc = config.get("oidc") or {}
    operator: list[Any] = []
    if to_bool(oidc.get("enabled", True)) or (oidc.get("clients") or []):
        clients = oidc.get("clients") or []
        if isinstance(clients, list):
            operator = clients
    engine = [] if managed_is_false(oidc) else load_engine_oidc_clients()
    return merge_oidc_clients(engine, operator)


def configured_groups(config: dict) -> list[str]:
    groups: list[str] = []
    seen: set[str] = set()
    for name in list(config.get("groups") or []) + list(DEFAULT_GROUPS):
        group = str(name or "").strip()
        if group and group not in seen:
            seen.add(group)
            groups.append(group)
    for user in config.get("users") or []:
        if not isinstance(user, dict):
            continue
        for name in user.get("groups") or []:
            group = str(name or "").strip()
            if group and group not in seen:
                seen.add(group)
                groups.append(group)
    return groups


def compose_file_paths(config: dict) -> list[Path]:
    files = [COMPOSE_DIR / "docker-compose.yml"]
    if proxy_mode(config) == "integrate":
        files.append(COMPOSE_DIR / "integrate.yml")
    else:
        files.append(COMPOSE_DIR / "caddy.yml")
    return files


def derive_compose_files(config: dict) -> list[str]:
    files = ["docker-compose.yml"]
    if proxy_mode(config) == "integrate":
        files.append("integrate.yml")
    else:
        files.append("caddy.yml")
    return files


def load_or_create_secrets() -> dict:
    if SECRETS_PATH.is_file():
        data = load_yaml(SECRETS_PATH)
    else:
        data = {}
    for key in SECRET_KEYS:
        if key not in data or not str(data.get(key) or "").strip():
            data[key] = secrets.token_urlsafe(16 if key == "ADMIN_PASSWORD" else 24)
    save_yaml(SECRETS_PATH, data)
    SECRETS_PATH.chmod(0o600)
    return data


def render_template(template: str, values: dict[str, str]) -> str:
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    if "{{" in rendered:
        missing = sorted({part.split("}")[0] for part in rendered.split("{{")[1:]})
        raise ValueError(f"Unresolved template placeholders: {', '.join(missing)}")
    return rendered


def build_server_toml(config: dict) -> str:
    domain = str(config["kanidm"]["domain"]).strip()
    origin = kanidm_origin(domain)
    ldap_bind = "[::]:3636" if to_bool((config.get("kanidm") or {}).get("ldap", True)) else ""
    lines = [
        'bindaddress = "[::]:8443"',
        'db_path = "/data/kanidm.db"',
        'tls_chain = "/data/chain.pem"',
        'tls_key = "/data/key.pem"',
        f'domain = "{domain}"',
        f'origin = "{origin}"',
        "trust_x_forward_for = false",
        'log_level = "info"',
    ]
    if ldap_bind:
        lines.append(f'ldapbindaddress = "{ldap_bind}"')
    return "\n".join(lines) + "\n"


def build_client_toml(domain: str) -> str:
    return (
        f'uri = "https://kanidm:8443"\n'
        f'verify_ca = false\n'
        f'# Public origin is {kanidm_origin(domain)}\n'
    )


def generate_tls_material(data_dir: Path, domain: str) -> None:
    chain = data_dir / "chain.pem"
    key = data_dir / "key.pem"
    if chain.is_file() and key.is_file() and chain.stat().st_size > 0:
        return
    hostfs.ensure_writable_directory(data_dir)
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-days",
            "3650",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(chain),
            "-subj",
            f"/CN={domain}",
            "-addext",
            f"subjectAltName=DNS:{domain},DNS:kanidm,DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    chain.chmod(0o644)


def kanidm_portal_caddy_block(domain: str) -> str:
    return f"""# kanidm-easy-deploy — identity portal
{domain} {{
    reverse_proxy https://kanidm:8443 {{
        transport http {{
            tls_insecure_skip_verify
        }}
        header_up Host {{host}}
        header_up X-Forwarded-Host {{host}}
        header_up X-Forwarded-Proto {{scheme}}
        # Kanidm 1.7.x rejects malformed X-Forwarded-For; use the proxy TCP
        # address instead (trust_x_forward_for = false in server.toml).
        header_up -X-Forwarded-For
    }}
    encode gzip
    log
}}"""


def render_caddyfile(config: dict) -> None:
    domain = str(config["kanidm"]["domain"])
    block = kanidm_portal_caddy_block(domain)
    rendered = render_template(CADDY_TEMPLATE.read_text(), {"IDM_DOMAIN_BLOCK": block.strip()})
    CADDYFILE.parent.mkdir(parents=True, exist_ok=True)
    CADDYFILE.write_text(rendered + "\n")


def render_integration_fragment(config: dict) -> None:
    domain = str(config["kanidm"]["domain"])
    INTEGRATION_DIR.mkdir(parents=True, exist_ok=True)
    INTEGRATION_CADDY_FRAGMENT.write_text(kanidm_portal_caddy_block(domain) + "\n")


def write_compose_env(config: dict) -> None:
    kanidm = config["kanidm"]
    image = f"{kanidm.get('image', 'docker.io/kanidm/server')}:{kanidm.get('tag', '1.7.3')}"
    tools = f"{kanidm.get('tools_image', 'docker.io/kanidm/tools')}:{kanidm.get('tools_tag', kanidm.get('tag', '1.7.3'))}"
    lines = [
        f"KANIDM_IMAGE={image}",
        f"KANIDM_TOOLS_IMAGE={tools}",
        f"KANIDM_DATA_DIR={kanidm['data_dir']}",
    ]
    if proxy_mode(config) == "standalone":
        lines.append(f"KED_CADDYFILE={CADDYFILE.resolve()}")
    COMPOSE_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    COMPOSE_ENV_PATH.write_text("\n".join(lines) + "\n")
    COMPOSE_ENV_PATH.chmod(0o600)


def render_runtime_artifacts(config: dict, secrets: dict) -> None:
    kanidm = config["kanidm"]
    data_dir = hostfs.ensure_writable_directory(kanidm["data_dir"])
    generate_tls_material(data_dir, str(kanidm["domain"]))
    (data_dir / "server.toml").write_text(build_server_toml(config))
    (data_dir / "client.toml").write_text(build_client_toml(str(kanidm["domain"])))
    if proxy_mode(config) == "integrate":
        render_integration_fragment(config)
    else:
        render_caddyfile(config)
    write_compose_env(config)
    identity = {
        "provider": "kanidm",
        "domain": str(kanidm["domain"]),
        "origin": kanidm_origin(str(kanidm["domain"])),
        "ldap_url": "ldaps://kanidm:3636",
        "ldap_base_dn": ldap_base_dn(str(kanidm["domain"])),
        "ldap_bind_dn": "dn=token",
        "account_url": kanidm_origin(str(kanidm["domain"])) + "/",
    }
    write_sidecar(INTEGRATION_DIR / "identity.yaml", identity)


def write_sidecar(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Generated by kanidm-easy-deploy. Re-apply the kit after domain changes.\n"
    )
    path.write_text(header + yaml.safe_dump(data, default_flow_style=False, sort_keys=False))


def stop_standalone_caddy() -> None:
    if subprocess.run(["docker", "inspect", "kanidm_caddy"], capture_output=True).returncode == 0:
        print("Stopping standalone kanidm_caddy (integrate mode uses easydeploy-engine Caddy)…")
        subprocess.run(["docker", "stop", "kanidm_caddy"], check=False)
        subprocess.run(["docker", "rm", "kanidm_caddy"], check=False)


def ensure_docker_network(name: str) -> None:
    if subprocess.run(["docker", "network", "inspect", name], capture_output=True).returncode != 0:
        subprocess.run(["docker", "network", "create", name], check=True)


def docker_compose_cmd() -> list[str]:
    if shutil.which("docker"):
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return ["docker", "compose"]
    compose = shutil.which("docker-compose")
    if compose:
        return [compose]
    raise RuntimeError("Docker Compose v2 is required (docker compose)")


def run_compose(*args: str) -> None:
    cmd = docker_compose_cmd()
    for compose_file in compose_file_paths(load_config()):
        cmd.extend(["-f", str(compose_file)])
    cmd.extend(args)
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = COMPOSE_PROJECT_NAME
    if COMPOSE_ENV_PATH.is_file():
        for line in COMPOSE_ENV_PATH.read_text().splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    subprocess.run(cmd, cwd=COMPOSE_DIR, check=True, env=env)


def kanidm_cli(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    config = load_config()
    kanidm = config["kanidm"]
    tools = f"{kanidm.get('tools_image', 'docker.io/kanidm/tools')}:{kanidm.get('tools_tag', kanidm.get('tag', '1.7.3'))}"
    cmd = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--network",
        "kanidm-net",
        "-v",
        f"{kanidm['data_dir']}/client.toml:/data/client.toml:ro",
        tools,
        "kanidm",
        *args,
        "-c",
        "/data/client.toml",
    ]
    return subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )


def _kanidmd_admin(
    name: str,
    subcommand: str,
    *,
    password: str | None = None,
    use_exec: bool = True,
) -> subprocess.CompletedProcess[str]:
    config = load_config()
    image = f"{config['kanidm'].get('image', 'docker.io/kanidm/server')}:{config['kanidm'].get('tag', '1.7.3')}"
    base_cmd = ["kanidmd", subcommand, name, "-c", "/data/server.toml"]
    if use_exec:
        cmd = ["docker", "exec", "-i", "kanidm", *base_cmd]
    else:
        cmd = [
            "docker",
            "run",
            "--rm",
            "-i",
            "-v",
            f"{config['kanidm']['data_dir']}:/data",
            image,
            *base_cmd,
        ]
    input_text = f"{password}\n{password}\n" if password else None
    return subprocess.run(
        cmd,
        input=input_text,
        capture_output=True,
        text=True,
        check=False,
    )


def recover_account(name: str, password: str) -> None:
    """Reset a break-glass account password (idm_admin / admin service account)."""
    result = _kanidmd_admin(name, "recover-account", password=password, use_exec=True)
    if result.returncode == 0:
        return

    detail = (result.stderr or result.stdout or "").strip()
    # Kanidm 1.7.3: repeat recover-account can duplicate account_valid_from; the
    # release notes say to disable then recover when recover fails.
    disable = _kanidmd_admin(name, "disable-account", use_exec=True)
    if disable.returncode == 0:
        retry = _kanidmd_admin(name, "recover-account", password=password, use_exec=True)
        if retry.returncode == 0:
            return
        detail = (retry.stderr or retry.stdout or detail).strip()

    fallback = _kanidmd_admin(name, "recover-account", password=password, use_exec=False)
    if fallback.returncode != 0:
        if _kanidmd_admin(name, "disable-account", use_exec=False).returncode == 0:
            fallback = _kanidmd_admin(name, "recover-account", password=password, use_exec=False)
    if fallback.returncode == 0:
        return

    raise RuntimeError(
        f"Failed to recover Kanidm account {name!r}:\n{detail}\n{fallback.stderr or fallback.stdout}"
    )


def cli_ok(result: subprocess.CompletedProcess[str], *ok_fragments: str) -> bool:
    combined = f"{result.stdout}\n{result.stderr}".lower()
    if result.returncode == 0:
        return True
    return any(fragment in combined for fragment in ok_fragments)


def bootstrap_identity(config: dict, secrets: dict) -> None:
    """Create the first person, groups, OIDC clients, and LDAP token."""
    print("Bootstrapping Kanidm identity (idm_admin, groups, OIDC, LDAP)…")
    try:
        recover_account("idm_admin", secrets["IDM_ADMIN_PASSWORD"])
    except RuntimeError as exc:
        print(f"Warning: could not recover idm_admin: {exc}", file=sys.stderr)
        print(
            "Recover manually:\n"
            "  docker exec -i kanidm kanidmd disable-account idm_admin -c /data/server.toml\n"
            "  docker exec -i kanidm kanidmd recover-account idm_admin -c /data/server.toml",
            file=sys.stderr,
        )
        return

    # Optional: Kanidm server-config service account (not the person in deploy.yaml).
    try:
        recover_account("admin", secrets["ADMIN_PASSWORD"])
    except RuntimeError:
        print(
            "Warning: recover-account for service account 'admin' failed (optional). "
            "Use idm_admin for the web UI and person accounts for apps.",
            file=sys.stderr,
        )

    login = kanidm_cli("login", "--name", "idm_admin", input_text=secrets["IDM_ADMIN_PASSWORD"] + "\n")
    if login.returncode != 0:
        print(
            "Warning: kanidm CLI login as idm_admin failed. "
            f"{(login.stderr or login.stdout or '').strip()[:400]}",
            file=sys.stderr,
        )
        print("Create people, groups, and OAuth2 clients with the Kanidm CLI after the portal is up.", file=sys.stderr)
        return

    for group in configured_groups(config):
        created = kanidm_cli("group", "create", group)
        if not cli_ok(created, "already exists", "duplicate"):
            print(f"Warning: could not create group {group!r}: {(created.stderr or created.stdout or '')[:200]}", file=sys.stderr)

    for user in config.get("users") or []:
        if not isinstance(user, dict):
            continue
        username = str(user.get("username") or "").strip()
        if not username:
            continue
        display = str(user.get("display_name") or username)
        email = str(user.get("email") or "")
        created = kanidm_cli("person", "create", username, display)
        if not cli_ok(created, "already exists", "duplicate"):
            print(f"Warning: could not create person {username!r}: {(created.stderr or created.stdout or '')[:200]}", file=sys.stderr)
        if email:
            kanidm_cli("person", "update", username, "--mail", email)
        password = str(user.get("password") or "").strip() or secrets["ADMIN_PASSWORD"]
        kanidm_cli("person", "posix", "set", username)
        kanidm_cli("person", "posix", "set-password", username, input_text=f"{password}\n{password}\n")
        for group in user.get("groups") or []:
            name = str(group or "").strip()
            if name:
                kanidm_cli("group", "add-members", name, username)

    kanidm_cli("system", "domain", "set-ldap-allow-unix-password-bind", "true")
    ensure_ldap_token(secrets)
    apply_oauth2_clients(config, secrets)
    write_stalwart_identity_secrets(secrets)


def ensure_ldap_token(secrets: dict) -> None:
    if str(secrets.get("LDAP_TOKEN") or "").strip() and str(secrets.get("LDAP_TOKEN_CREATED") or ""):
        return
    account = kanidm_cli("service-account", "create", "stalwart-ldap", "Stalwart LDAP")
    if not cli_ok(account, "already exists", "duplicate"):
        print(
            f"Warning: could not create stalwart-ldap service account: {(account.stderr or account.stdout or '')[:200]}",
            file=sys.stderr,
        )
    token = kanidm_cli("service-account", "api-token", "generate", "stalwart-ldap", "stalwart")
    if token.returncode == 0:
        value = (token.stdout or "").strip().splitlines()
        secret = next((line.strip() for line in reversed(value) if line.strip() and " " not in line.strip()), "")
        if secret:
            secrets["LDAP_TOKEN"] = secret
            secrets["LDAP_TOKEN_CREATED"] = "1"
            save_yaml(SECRETS_PATH, secrets)


def apply_oauth2_clients(config: dict, secrets: dict) -> None:
    domain = str(config["kanidm"]["domain"])
    for client in oidc_clients(config):
        if not isinstance(client, dict):
            continue
        client_id = str(client.get("client_id") or "").strip()
        if not client_id:
            continue
        name = str(client.get("client_name") or client_id)
        landing = str(client.get("landing_url") or "").strip()
        redirects = [str(item).strip() for item in (client.get("redirect_uris") or []) if str(item).strip()]
        if not landing and redirects:
            landing = redirects[0].rsplit("/", 1)[0] if redirects[0].count("/") > 2 else redirects[0]
        if not landing:
            print(f"Warning: OIDC client {client_id!r} has no landing_url/redirect_uris; skipped.", file=sys.stderr)
            continue
        public = to_bool(client.get("public"))
        if public:
            created = kanidm_cli("system", "oauth2", "create-public", client_id, name, landing)
        else:
            created = kanidm_cli("system", "oauth2", "create", client_id, name, landing)
        if not cli_ok(created, "already exists", "duplicate"):
            print(
                f"Warning: could not create OAuth2 client {client_id!r}: {(created.stderr or created.stdout or '')[:240]}",
                file=sys.stderr,
            )
        for url in redirects:
            kanidm_cli("system", "oauth2", "add-redirect-url", client_id, url)
        scopes = [str(item) for item in (client.get("scopes") or DEFAULT_OIDC_SCOPES) if str(item).strip()]
        if scopes:
            kanidm_cli("system", "oauth2", "update-scope-map", client_id, "idm_all_persons", *scopes)
        if to_bool(client.get("prefer_short_username", True)):
            kanidm_cli("system", "oauth2", "prefer-short-username", client_id)
        if to_bool(client.get("legacy_crypto")):
            kanidm_cli("system", "oauth2", "warning-enable-legacy-crypto", client_id)
        if to_bool(client.get("disable_pkce")):
            kanidm_cli("system", "oauth2", "warning-insecure-client-disable-pkce", client_id)
        if not public:
            shown = kanidm_cli("system", "oauth2", "show-basic-secret", client_id)
            secret = (shown.stdout or "").strip().splitlines()
            value = next((line.strip() for line in reversed(secret) if line.strip() and not line.lower().startswith("name")), "")
            if value:
                secrets[f"OIDC_SECRET_{client_id.upper()}"] = value
                save_yaml(SECRETS_PATH, secrets)
                write_consumer_secret(client_id, value, domain)


def write_consumer_secret(client_id: str, secret: str, domain: str) -> None:
    """Publish confidential-client secrets so sibling kits can finish OIDC wiring."""
    write_sidecar(INTEGRATION_DIR / "oidc-secrets.d" / f"{client_id}.yaml", {"client_secret": secret})
    if client_id == "matrix" or client_id.startswith("matrix-"):
        sibling = PROJECT_ROOT.parent / "matrix-easy-deploy" / ".matrix-easy-deploy" / "integration" / "oidc-provider.yaml"
        if sibling.parent.is_dir() or sibling.is_file():
            existing = load_yaml(sibling) if sibling.is_file() else {}
            existing.setdefault("provider", "kanidm")
            existing.setdefault("name", "Kanidm")
            existing.setdefault("issuer", kanidm_issuer_url(domain, client_id))
            existing.setdefault("client_id", client_id)
            existing["client_secret"] = secret
            write_sidecar(sibling, existing)
    if client_id == "stalwart" or client_id.startswith("stalwart-"):
        sibling = (
            PROJECT_ROOT.parent
            / "stalwart-easy-deploy"
            / ".stalwart-easy-deploy"
            / "integration"
            / "identity-provider.yaml"
        )
        if sibling.is_file():
            existing = load_yaml(sibling)
            existing.setdefault("oidc", {})
            if isinstance(existing["oidc"], dict):
                existing["oidc"]["client_secret"] = secret
            write_sidecar(sibling, existing)


def write_stalwart_identity_secrets(secrets: dict) -> None:
    sibling = (
        PROJECT_ROOT.parent
        / "stalwart-easy-deploy"
        / ".stalwart-easy-deploy"
        / "integration"
        / "identity-provider.yaml"
    )
    if not sibling.is_file():
        return
    existing = load_yaml(sibling)
    token = str(secrets.get("LDAP_TOKEN") or "").strip()
    if token:
        existing.setdefault("ldap", {})
        if isinstance(existing["ldap"], dict):
            existing["ldap"]["bind_secret"] = token
    oidc_secret = str(secrets.get("OIDC_SECRET_STALWART") or "").strip()
    if oidc_secret:
        existing.setdefault("oidc", {})
        if isinstance(existing["oidc"], dict):
            existing["oidc"]["client_secret"] = oidc_secret
    write_sidecar(sibling, existing)


def reconcile_runtime(skip_pull: bool = False) -> None:
    config = load_config()
    secrets = load_yaml(SECRETS_PATH)
    mode = proxy_mode(config)
    ensure_docker_network("kanidm-net")
    if mode == "integrate":
        net = integrate_network_name(config)
        if net != DEFAULT_INTEGRATE_NETWORK:
            print(
                f"Warning: custom integrate network {net!r} is not yet supported in compose/integrate.yml; "
                f"using {DEFAULT_INTEGRATE_NETWORK}",
                file=sys.stderr,
            )
        ensure_docker_network(DEFAULT_INTEGRATE_NETWORK)
        stop_standalone_caddy()
    if not skip_pull:
        print("Pulling Kanidm stack images…")
        run_compose("pull")
    print("Starting Kanidm stack…")
    run_compose("up", "-d", "--wait", "--remove-orphans")
    bootstrap_identity(config, secrets)


def print_summary(config: dict, secrets: dict) -> None:
    kanidm = config["kanidm"]
    domain = kanidm["domain"]
    print()
    print("=== Deployment summary ===")
    print(f"Kanidm portal:   https://{domain}")
    print(f"OIDC issuer:     {kanidm_origin(domain)}/oauth2/openid/<client_id>")
    print(f"LDAP:            ldaps://kanidm:3636  base {ldap_base_dn(domain)}")
    print(f"Data directory:  {kanidm['data_dir']}")
    print(f"Secrets file:    {SECRETS_PATH}")
    print(f"idm_admin pass:  {secrets.get('IDM_ADMIN_PASSWORD')}  (web UI / CLI admin)")
    admin = (config.get("users") or [{}])[0]
    username = admin.get("username", "admin")
    if not str(admin.get("password") or "").strip():
        print(
            f"Person account:  {username} / {secrets.get('ADMIN_PASSWORD')} "
            f"(for OpenCloud/Matrix/Stalwart; created on bootstrap)"
        )
    print("  Sign in at the portal with idm_admin, not the person username, until bootstrap completes.")
    clients = oidc_clients(config)
    if clients:
        print(f"OIDC clients:    {', '.join(str(item.get('client_id')) for item in clients if isinstance(item, dict))}")
    if proxy_mode(config) == "integrate":
        print(f"Proxy mode:      integrate (Caddy fragment: {INTEGRATION_CADDY_FRAGMENT})")
        print("                 Run easydeploy-engine apply.sh to refresh the shared Caddy.")
    else:
        print("Proxy mode:      standalone (local kanidm_caddy on :443)")
    print()
    print("Kanidm is the source of truth for users, groups, and authentication.")
    print("Create further people with: docker compose --profile cli run --rm kanidm-cli person create …")
    print()


def apply_configuration(*, skip_runtime: bool = False, skip_pull: bool = False) -> None:
    config = load_config()
    validate_config(config)
    secrets = load_or_create_secrets()
    render_runtime_artifacts(config, secrets)
    if not skip_runtime:
        reconcile_runtime(skip_pull=skip_pull)
    print_summary(config, secrets)


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply kanidm-easy-deploy configuration")
    parser.add_argument("--skip-runtime", action="store_true")
    parser.add_argument("--skip-pull", action="store_true")
    args = parser.parse_args()
    try:
        apply_configuration(skip_runtime=args.skip_runtime, skip_pull=args.skip_pull)
    except (FileNotFoundError, ValueError, RuntimeError, PermissionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
