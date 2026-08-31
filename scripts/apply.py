#!/usr/bin/env python3
"""kanidm-easy-deploy configuration engine."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
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
DEFAULT_KANIDM_TAG = "1.11.1"
DEFAULT_LOGO_PATH = PROJECT_ROOT / "assets" / "branding" / "default-logo.svg"
BRANDING_DIR = STATE_DIR / "branding"
MAX_BRANDING_IMAGE_BYTES = 256 * 1024
SUPPORTED_BRANDING_IMAGE_TYPES = frozenset({"png", "jpg", "jpeg", "gif", "svg", "webp"})
KANIDM_CLIENT_CONFIG_BASENAME = "kanidm-client-config"
KANIDM_TOKENS_BASENAME = "kanidm_tokens"

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
DEFAULT_OPENCLOUD_CLAIM_MAPS = [
    {
        "claim": "opencloudRoles",
        "join": "array",
        "mappings": [
            {"group": "opencloud-admin", "values": ["admin"]},
            {"group": "opencloud-user", "values": ["user"]},
            {"group": "opencloud-guest", "values": ["guest"]},
        ],
    }
]


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
    for user in users:
        username = str((user or {}).get("username") or "").strip().lower()
        if username in {"admin", "idm_admin"}:
            raise ValueError(
                f"users username {username!r} is reserved by Kanidm; "
                "choose a person username such as 'operator'"
            )

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


IDENTITY_HOST_LABELS = frozenset({"auth", "idm", "sso", "kanidm", "login", "accounts"})


def org_mail_domain(kanidm_domain: str) -> str:
    """Organisation mail domain: auth.opencomp.eu → opencomp.eu."""
    labels = [part for part in str(kanidm_domain).strip().lower().split(".") if part]
    if len(labels) >= 3 and labels[0] in IDENTITY_HOST_LABELS:
        return ".".join(labels[1:])
    sibling = PROJECT_ROOT.parent / "stalwart-easy-deploy" / "deploy.yaml"
    if sibling.is_file():
        try:
            domain = str((load_yaml(sibling).get("stalwart") or {}).get("domain") or "").strip()
        except ValueError:
            domain = ""
        if domain and domain not in {"example.com"}:
            return domain.lower()
    return str(kanidm_domain).strip().lower()


def person_mail_address(username: str, email: str, kanidm_domain: str) -> str:
    if email.strip():
        return email.strip()
    return f"{username}@{org_mail_domain(kanidm_domain)}"


def default_display_name(domain: str) -> str:
    labels = [part for part in str(domain).strip().lower().split(".") if part]
    if len(labels) >= 2 and labels[0] in IDENTITY_HOST_LABELS:
        label = labels[1]
    elif labels:
        label = labels[0]
    else:
        label = str(domain).strip()
    return label.replace("-", " ").title()


def branding_disabled(value: Any) -> bool:
    if value is False:
        return True
    return str(value or "").strip().lower() in {"false", "no", "off", "0"}


def oauth2_icons_enabled(config: dict) -> bool:
    branding = config.get("branding") or {}
    if not isinstance(branding, dict):
        return True
    return not branding_disabled(branding.get("oauth2_icons", True))


def image_type_for_path(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix == "jpeg":
        return "jpg"
    if suffix not in SUPPORTED_BRANDING_IMAGE_TYPES:
        raise ValueError(
            f"Unsupported branding image type {suffix!r}; "
            f"expected one of {', '.join(sorted(SUPPORTED_BRANDING_IMAGE_TYPES))}"
        )
    return suffix


def validate_image_file(path: Path) -> None:
    size = path.stat().st_size
    if size <= 0:
        raise ValueError(f"branding image is empty: {path}")
    if size > MAX_BRANDING_IMAGE_BYTES:
        raise ValueError(
            f"branding image exceeds Kanidm's 256 KB limit ({size} bytes): {path}"
        )


def _download_url(url: str, dest: Path, *, max_bytes: int = MAX_BRANDING_IMAGE_BYTES) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "kanidm-easy-deploy/1.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        content_type = str(response.headers.get("Content-Type") or "").lower()
        if content_type and not content_type.startswith(("image/", "application/octet-stream")):
            raise ValueError(f"URL did not return an image ({content_type}): {url}")
        data = bytearray()
        while True:
            chunk = response.read(8192)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_bytes:
                raise ValueError(f"downloaded image exceeds 256 KB limit: {url}")
    if not data:
        raise ValueError(f"downloaded image is empty: {url}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)


def _guess_extension(url: str, content: bytes) -> str:
    path = urllib.parse.urlparse(url).path.lower()
    for ext in ("svg", "png", "jpg", "jpeg", "gif", "webp", "ico"):
        if path.endswith(f".{ext}"):
            return "jpg" if ext == "jpeg" else ext
    if content.startswith(b"<svg") or b"<svg" in content[:256]:
        return "svg"
    if content.startswith(b"\x89PNG"):
        return "png"
    if content.startswith((b"\xff\xd8\xff", b"GIF87a", b"GIF89a")):
        return "jpg" if content.startswith(b"\xff\xd8\xff") else "gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "webp"
    return "png"


def resolve_image_source(source: str, *, cache_name: str) -> Path:
    value = str(source or "").strip()
    if not value:
        raise ValueError("branding image source must not be empty")
    if value.startswith(("http://", "https://")):
        BRANDING_DIR.mkdir(parents=True, exist_ok=True)
        cache_base = BRANDING_DIR / cache_name
        temp = cache_base.with_suffix(".download")
        _download_url(value, temp)
        content = temp.read_bytes()
        ext = _guess_extension(value, content)
        # Kanidm does not accept ICO; skip and let the caller try the next URL.
        if ext == "ico":
            raise ValueError(f"ICO favicons are not supported by Kanidm: {value}")
        if ext not in SUPPORTED_BRANDING_IMAGE_TYPES:
            raise ValueError(f"Unsupported downloaded image type from {value!r}")
        dest = cache_base.with_suffix(f".{ext}")
        temp.replace(dest)
        validate_image_file(dest)
        return dest
    path = Path(value)
    if not path.is_absolute():
        path = (PROJECT_ROOT / path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"branding image not found: {source}")
    validate_image_file(path)
    return path


class FaviconLinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.icons: list[tuple[int, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "link":
            return
        attr_map = {key.lower(): (value or "") for key, value in attrs}
        rel = str(attr_map.get("rel") or "").lower()
        if "icon" not in rel.split():
            return
        href = str(attr_map.get("href") or "").strip()
        if not href:
            return
        url = urllib.parse.urljoin(self.base_url, href)
        priority = 0 if url.lower().endswith(".svg") else 1
        if "apple-touch-icon" in rel:
            priority = 2
        self.icons.append((priority, url))


def landing_origin(landing_url: str) -> str:
    parsed = urllib.parse.urlparse(str(landing_url or "").strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def discover_favicon_urls(landing_url: str) -> list[str]:
    origin = landing_origin(landing_url)
    if not origin:
        return []
    urls: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        normalized = url.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            urls.append(normalized)

    try:
        request = urllib.request.Request(
            landing_url,
            headers={"User-Agent": "kanidm-easy-deploy/1.0"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if "html" in content_type:
                html = response.read(256_000).decode("utf-8", errors="replace")
                parser = FaviconLinkParser(landing_url)
                parser.feed(html)
                for _, icon_url in sorted(parser.icons):
                    add(icon_url)
    except (urllib.error.URLError, TimeoutError, ValueError):
        pass

    for candidate in (
        f"{origin}/favicon.svg",
        f"{origin}/favicon.png",
        f"{origin}/favicon.ico",
        f"{origin}/apple-touch-icon.png",
        f"{origin}/apple-touch-icon-precomposed.png",
    ):
        add(candidate)
    return urls


def fetch_landing_favicon(landing_url: str, *, cache_name: str) -> Path | None:
    for index, url in enumerate(discover_favicon_urls(landing_url)):
        try:
            return resolve_image_source(url, cache_name=f"{cache_name}-{index}")
        except (OSError, ValueError, urllib.error.URLError):
            continue
    return None


def resolve_oauth2_client_image(config: dict, client: dict) -> Path | None:
    image_setting = client.get("image")
    client_id = str(client.get("client_id") or "client").strip() or "client"
    if branding_disabled(image_setting):
        return None
    if image_setting:
        return resolve_image_source(str(image_setting), cache_name=f"oauth2-{client_id}")
    if not oauth2_icons_enabled(config):
        return None
    landing = str(client.get("landing_url") or "").strip()
    if not landing:
        redirects = client.get("redirect_uris") or []
        if redirects:
            landing = str(redirects[0]).strip()
    if not landing:
        return None
    return fetch_landing_favicon(landing, cache_name=f"oauth2-{client_id}")


def set_kanidm_domain_image(image_path: Path) -> None:
    image_type = image_type_for_path(image_path)
    container_path = f"/tmp/kanidm-branding{image_path.suffix.lower() or '.img'}"
    result = kanidm_cli(
        "system",
        "domain",
        "set-image",
        container_path,
        image_type,
        "--name",
        "admin",
        mounts=[(str(image_path.resolve()), f"{container_path}:ro")],
    )
    if not cli_ok(result):
        raise RuntimeError(
            "Could not set Kanidm portal image: "
            f"{cli_output(result).strip()[:500]}"
        )


def set_kanidm_oauth2_image(client_id: str, image_path: Path) -> None:
    image_type = image_type_for_path(image_path)
    container_path = f"/tmp/kanidm-branding-{client_id}{image_path.suffix.lower() or '.img'}"
    result = kanidm_cli(
        "system",
        "oauth2",
        "set-image",
        client_id,
        container_path,
        image_type,
        "--name",
        "idm_admin",
        mounts=[(str(image_path.resolve()), f"{container_path}:ro")],
    )
    if not cli_ok(result):
        raise RuntimeError(
            f"Could not set OAuth2 image for {client_id!r}: "
            f"{cli_output(result).strip()[:500]}"
        )


def apply_portal_branding(config: dict) -> None:
    branding = config.get("branding") or {}
    if branding is False:
        return
    if not isinstance(branding, dict):
        raise ValueError("branding must be a mapping when set")

    display_name = str(branding.get("display_name") or "").strip()
    if not display_name:
        display_name = default_display_name(str(config["kanidm"]["domain"]))
    named = kanidm_cli(
        "system",
        "domain",
        "set-displayname",
        display_name,
        "--name",
        "admin",
    )
    if not cli_ok(named):
        raise RuntimeError(
            "Could not set Kanidm portal display name: "
            f"{cli_output(named).strip()[:500]}"
        )

    if branding_disabled(branding.get("logo", None)) and "logo" in branding:
        return

    logo_source = branding.get("logo")
    if logo_source:
        logo_path = resolve_image_source(str(logo_source), cache_name="portal-logo")
    else:
        logo_path = DEFAULT_LOGO_PATH
        validate_image_file(logo_path)
    set_kanidm_domain_image(logo_path)
    print(f"  Portal branding: {display_name}")


def apply_oauth2_client_image(config: dict, client: dict) -> None:
    client_id = str(client.get("client_id") or "").strip()
    if not client_id:
        return
    try:
        image_path = resolve_oauth2_client_image(config, client)
    except (OSError, ValueError) as exc:
        print(
            f"Warning: could not resolve OAuth2 image for {client_id!r}: {exc}",
            file=sys.stderr,
        )
        return
    if not image_path:
        return
    set_kanidm_oauth2_image(client_id, image_path)
    print(f"  OAuth2 icon set: {client_id}")


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
        f"verify_ca = false\n"
        f"# Public origin is {kanidm_origin(domain)}\n"
    )


def kanidm_client_config_path(data_dir: Path) -> Path:
    return data_dir / KANIDM_CLIENT_CONFIG_BASENAME


def kanidm_tokens_path(data_dir: Path) -> Path:
    return data_dir / KANIDM_TOKENS_BASENAME


def ensure_kanidm_cli_state(data_dir: Path) -> None:
    tokens = kanidm_tokens_path(data_dir)
    reset_tokens = not tokens.is_file()
    if tokens.is_file():
        try:
            token_data = json.loads(tokens.read_text() or "{}")
            reset_tokens = not isinstance(token_data.get("instances"), dict)
        except (json.JSONDecodeError, AttributeError):
            reset_tokens = True
    if reset_tokens:
        tokens.write_text('{"instances": {}}\n')
    tokens.chmod(0o600)


def kanidm_cli_volume_mounts(data_dir: str | Path) -> list[str]:
    root = Path(data_dir)
    ensure_kanidm_cli_state(root)
    config = kanidm_client_config_path(root)
    tokens = kanidm_tokens_path(root)
    if not config.is_file():
        raise FileNotFoundError(f"Missing Kanidm client config: {config}")
    return [
        "-v",
        f"{config}:/root/.config/kanidm:ro",
        "-v",
        f"{tokens}:/root/.cache/kanidm_tokens",
    ]


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
    image = f"{kanidm.get('image', 'docker.io/kanidm/server')}:{kanidm.get('tag', DEFAULT_KANIDM_TAG)}"
    tools = f"{kanidm.get('tools_image', 'docker.io/kanidm/tools')}:{kanidm.get('tools_tag', kanidm.get('tag', DEFAULT_KANIDM_TAG))}"
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
    client_config = build_client_toml(str(kanidm["domain"]))
    kanidm_client_config_path(data_dir).write_text(client_config)
    # Legacy alias; the kanidm CLI reads ~/.config/kanidm, not client.toml.
    (data_dir / "client.toml").write_text(client_config)
    ensure_kanidm_cli_state(data_dir)
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


def kanidm_cli(
    *args: str,
    password: str | None = None,
    input_text: str | None = None,
    mounts: list[tuple[str, str]] | None = None,
) -> subprocess.CompletedProcess[str]:
    config = load_config()
    kanidm = config["kanidm"]
    tools = f"{kanidm.get('tools_image', 'docker.io/kanidm/tools')}:{kanidm.get('tools_tag', kanidm.get('tag', DEFAULT_KANIDM_TAG))}"
    cmd = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--network",
        "kanidm-net",
        *kanidm_cli_volume_mounts(kanidm["data_dir"]),
    ]
    for host_path, container_path in mounts or []:
        cmd.extend(["-v", f"{host_path}:{container_path}"])
    if password:
        cmd.extend(["-e", f"KANIDM_PASSWORD={password}"])
    cmd.extend([tools, "kanidm", *args])
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
    scripting: bool = False,
    use_exec: bool = True,
) -> subprocess.CompletedProcess[str]:
    config = load_config()
    image = f"{config['kanidm'].get('image', 'docker.io/kanidm/server')}:{config['kanidm'].get('tag', DEFAULT_KANIDM_TAG)}"
    base_cmd = ["kanidmd"]
    if scripting:
        base_cmd.append("scripting")
    base_cmd.extend([subcommand, name])
    if not scripting:
        base_cmd.extend(["-c", "/data/server.toml"])
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
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )


def recovered_password(result: subprocess.CompletedProcess[str]) -> str:
    output = f"{result.stdout}\n{result.stderr}"
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and str(value.get("output") or "").strip():
            return str(value["output"]).strip()
    match = re.search(r'new_password:\s*"([^"]+)"', output)
    return match.group(1) if match else ""


def recover_account(name: str) -> str:
    """Recover a break-glass account and return Kanidm's generated password."""
    # Kanidm 1.11+ uses the machine-readable scripting interface.
    result = _kanidmd_admin(name, "recover-account", scripting=True, use_exec=True)
    if result.returncode == 0:
        generated = recovered_password(result)
        if generated:
            return generated
        raise RuntimeError(
            f"Kanidm recovered {name!r} but its generated password could not be parsed:\n"
            f"{result.stdout}\n{result.stderr}"
        )

    detail = (result.stderr or result.stdout or "").strip()
    # Kanidm <= 1.8 uses the legacy command.
    result = _kanidmd_admin(name, "recover-account", use_exec=True)
    if result.returncode == 0:
        generated = recovered_password(result)
        if generated:
            return generated
        raise RuntimeError(
            f"Kanidm recovered {name!r} but its generated password could not be parsed:\n"
            f"{result.stdout}\n{result.stderr}"
        )
    detail = (result.stderr or result.stdout or detail).strip()

    # Kanidm 1.7.3: repeat recover-account can duplicate account_valid_from; the
    # release notes say to disable then recover when recover fails.
    disable = _kanidmd_admin(name, "disable-account", use_exec=True)
    if disable.returncode == 0:
        retry = _kanidmd_admin(name, "recover-account", use_exec=True)
        if retry.returncode == 0:
            generated = recovered_password(retry)
            if generated:
                return generated
        detail = (retry.stderr or retry.stdout or detail).strip()

    fallback = _kanidmd_admin(name, "recover-account", use_exec=False)
    if fallback.returncode != 0:
        if _kanidmd_admin(name, "disable-account", use_exec=False).returncode == 0:
            fallback = _kanidmd_admin(name, "recover-account", use_exec=False)
    if fallback.returncode == 0:
        generated = recovered_password(fallback)
        if generated:
            return generated

    raise RuntimeError(
        f"Failed to recover Kanidm account {name!r}:\n{detail}\n{fallback.stderr or fallback.stdout}"
    )


def cli_output(result: subprocess.CompletedProcess[str]) -> str:
    return f"{result.stdout or ''}\n{result.stderr or ''}"


def cli_ok(result: subprocess.CompletedProcess[str], *ok_fragments: str) -> bool:
    combined = cli_output(result).lower()
    if any(fragment in combined for fragment in ok_fragments):
        return True
    # Some Kanidm CLI commands historically return exit code 0 even when the
    # operation failed. Treat logged errors as failures too.
    has_logged_error = bool(re.search(r"(?m)\bERROR\b", cli_output(result)))
    return result.returncode == 0 and not has_logged_error


def cli_exists(result: subprocess.CompletedProcess[str]) -> bool:
    """True when a get/list command found an existing Kanidm entry."""
    combined = cli_output(result).lower()
    if any(
        fragment in combined
        for fragment in (
            "nomatchingentries",
            "item not found",
            "no matching entries",
        )
    ):
        return False
    return cli_ok(result)


def cli_json_field(result: subprocess.CompletedProcess[str], *fields: str) -> str:
    """Extract a string field from Kanidm JSON output."""
    output = result.stdout or ""

    def find_field(value: Any) -> str:
        if isinstance(value, dict):
            for field in fields:
                field_value = value.get(field)
                if isinstance(field_value, str) and field_value.strip():
                    return field_value.strip()
                if field_value is not None:
                    nested = find_field(field_value)
                    if nested:
                        return nested
            for nested_value in value.values():
                nested = find_field(nested_value)
                if nested:
                    return nested
        elif isinstance(value, list):
            for item in value:
                nested = find_field(item)
                if nested:
                    return nested
        return ""

    # Kanidm 1.11 emits pretty-printed, multi-line JSON.
    try:
        parsed = json.loads(output.strip())
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        value = find_field(parsed)
        if value:
            return value

    # Retain support for one JSON object per output line.
    for line in output.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        found = find_field(value)
        if found:
            return found

    # Last resort for JSON mixed with non-JSON diagnostic lines.
    for field in fields:
        match = re.search(
            rf'"{re.escape(field)}"\s*:\s*"((?:\\.|[^"\\])*)"',
            output,
        )
        if match:
            try:
                return json.loads(f'"{match.group(1)}"')
            except json.JSONDecodeError:
                return match.group(1)
    return ""


def login_idm_admin(password: str) -> subprocess.CompletedProcess[str]:
    return kanidm_cli("login", "--name", "idm_admin", password=password)


def login_admin(password: str) -> subprocess.CompletedProcess[str]:
    return kanidm_cli("login", "--name", "admin", password=password)


def ensure_idm_admin_login(password: str) -> str:
    """Login with the configured secret, recovering only on a fresh/stale account."""
    login = login_idm_admin(password)
    if cli_ok(login):
        return password
    try:
        generated_password = recover_account("idm_admin")
    except RuntimeError as exc:
        print(f"Warning: could not recover idm_admin: {exc}", file=sys.stderr)
        return ""

    secrets_data = load_yaml(SECRETS_PATH)
    secrets_data["IDM_ADMIN_PASSWORD"] = generated_password
    save_yaml(SECRETS_PATH, secrets_data)
    SECRETS_PATH.chmod(0o600)

    retry = login_idm_admin(generated_password)
    if cli_ok(retry):
        print("Updated IDM_ADMIN_PASSWORD in secrets.yaml after Kanidm recovery.")
        return generated_password
    print(
        "Warning: kanidm CLI login as idm_admin failed. "
        f"{(retry.stderr or retry.stdout or '').strip()[:400]}",
        file=sys.stderr,
    )
    return ""


def ensure_admin_login(password: str) -> str:
    """Login with the system admin account (required for domain branding)."""
    login = login_admin(password)
    if cli_ok(login):
        return password
    try:
        generated_password = recover_account("admin")
    except RuntimeError as exc:
        print(f"Warning: could not recover admin: {exc}", file=sys.stderr)
        return ""

    secrets_data = load_yaml(SECRETS_PATH)
    secrets_data["ADMIN_PASSWORD"] = generated_password
    save_yaml(SECRETS_PATH, secrets_data)
    SECRETS_PATH.chmod(0o600)

    retry = login_admin(generated_password)
    if cli_ok(retry):
        print("Updated ADMIN_PASSWORD in secrets.yaml after Kanidm recovery.")
        return generated_password
    print(
        "Warning: kanidm CLI login as admin failed. "
        f"{(retry.stderr or retry.stdout or '').strip()[:400]}",
        file=sys.stderr,
    )
    return ""


def credential_status(username: str) -> subprocess.CompletedProcess[str]:
    return kanidm_cli(
        "person",
        "credential",
        "status",
        username,
        "--name",
        "idm_admin",
    )


def create_enrollment_link(username: str, domain: str) -> str:
    """Create a one-day web enrollment link for a person without credentials."""
    result = kanidm_cli(
        "person",
        "credential",
        "create-reset-token",
        username,
        "--ttl",
        "86400",
        "--name",
        "idm_admin",
    )
    if not cli_ok(result):
        # Kanidm <= 1.8 accepted TTL as a positional argument.
        result = kanidm_cli(
            "person",
            "credential",
            "create-reset-token",
            username,
            "86400",
            "--name",
            "idm_admin",
        )
    if not cli_ok(result):
        print(
            f"Warning: could not create enrollment link for {username!r}: "
            f"{(result.stderr or result.stdout or '').strip()[:300]}",
            file=sys.stderr,
        )
        return ""
    output = f"{result.stdout}\n{result.stderr}"
    link = next(
        (
            line.split("This link:", 1)[1].strip()
            for line in output.splitlines()
            if "This link:" in line
        ),
        "",
    )
    if link:
        # The CLI connects on the internal Docker hostname, but users need the
        # public origin in the enrollment URL.
        link = link.replace("https://kanidm:8443", kanidm_origin(domain))
    return link


def save_enrollment_links(links: dict[str, str], credentialed: set[str]) -> None:
    path = STATE_DIR / "enrollment-links.yaml"
    existing = load_yaml(path) if path.is_file() else {}
    for username in credentialed:
        existing.pop(username, None)
    existing.update(links)
    if not existing:
        if path.is_file():
            path.unlink()
        return
    save_yaml(path, existing)
    path.chmod(0o600)


def bootstrap_identity(config: dict, secrets: dict) -> None:
    """Create the first person, groups, OIDC clients, and LDAP token."""
    print("Bootstrapping Kanidm identity (idm_admin, groups, OIDC, LDAP)…")
    idm_admin_password = ensure_idm_admin_login(secrets["IDM_ADMIN_PASSWORD"])
    if not idm_admin_password:
        raise RuntimeError(
            "Kanidm bootstrap cannot authenticate as idm_admin; refusing to "
            "report a partially configured deployment.\n"
            "For Kanidm 1.11+, recover manually with:\n"
            "  docker exec kanidm kanidmd scripting recover-account idm_admin\n"
            "Then copy the generated password into IDM_ADMIN_PASSWORD in "
            ".kanidm-easy-deploy/secrets.yaml."
        )
    secrets["IDM_ADMIN_PASSWORD"] = idm_admin_password
    admin_password = ensure_admin_login(secrets["ADMIN_PASSWORD"])
    if not admin_password:
        raise RuntimeError(
            "Kanidm bootstrap cannot authenticate as admin; portal branding "
            "requires the system admin account.\n"
            "For Kanidm 1.11+, recover manually with:\n"
            "  docker exec kanidm kanidmd scripting recover-account admin\n"
            "Then copy the generated password into ADMIN_PASSWORD in "
            ".kanidm-easy-deploy/secrets.yaml."
        )
    secrets["ADMIN_PASSWORD"] = admin_password
    apply_portal_branding(config)

    for group in configured_groups(config):
        existing_group = kanidm_cli("group", "get", group, "--name", "idm_admin")
        if not cli_exists(existing_group):
            created = kanidm_cli("group", "create", group, "--name", "idm_admin")
            if not cli_ok(created, "already exists", "duplicate", "attributeuniqueness"):
                raise RuntimeError(
                    f"Could not create group {group!r}: "
                    f"{cli_output(created).strip()[:500]}"
                )

    enrollment_links: dict[str, str] = {}
    credentialed: set[str] = set()
    for user in config.get("users") or []:
        if not isinstance(user, dict):
            continue
        username = str(user.get("username") or "").strip()
        if not username:
            continue
        display = str(user.get("display_name") or username)
        email = person_mail_address(
            username,
            str(user.get("email") or ""),
            str(config["kanidm"]["domain"]),
        )
        if str(user.get("password") or "").strip():
            password_note = (
                f"Note: users[{username!r}].password is not applied to web login. "
                "Kanidm uses a one-time enrollment link to set portal/OIDC credentials."
            )
        else:
            password_note = ""
        existing_person = kanidm_cli("person", "get", username, "--name", "idm_admin")
        if not cli_exists(existing_person):
            created = kanidm_cli(
                "person", "create", username, display, "--name", "idm_admin"
            )
            if not cli_ok(created, "already exists", "duplicate", "attributeuniqueness"):
                raise RuntimeError(
                    f"Could not create person {username!r}: "
                    f"{cli_output(created).strip()[:500]}"
                )
        if email:
            kanidm_cli(
                "person", "update", username, "--mail", email,
                "--name", "idm_admin",
            )
        kanidm_cli("person", "posix", "set", username, "--name", "idm_admin")
        for group in user.get("groups") or []:
            name = str(group or "").strip()
            if not name:
                continue
            added = kanidm_cli(
                "group", "add-members", name, username,
                "--name", "idm_admin",
            )
            if not cli_ok(added, "already", "duplicate", "members already"):
                raise RuntimeError(
                    f"Could not add {username!r} to group {name!r}: "
                    f"{cli_output(added).strip()[:500]}"
                )
        status = credential_status(username)
        if not cli_ok(status) or "no credentials" in f"{status.stdout}\n{status.stderr}".lower():
            if password_note:
                print(password_note, file=sys.stderr)
            link = create_enrollment_link(username, str(config["kanidm"]["domain"]))
            if link:
                enrollment_links[username] = link
        else:
            credentialed.add(username)

    # Let LDAP/Unix authentication use the person's primary password when no
    # separate POSIX password exists.
    kanidm_cli(
        "group",
        "account-policy",
        "allow-primary-cred-fallback",
        "mail-users",
        "true",
    )
    save_enrollment_links(enrollment_links, credentialed)

    kanidm_cli("system", "domain", "set-ldap-allow-unix-password-bind", "true")
    ensure_ldap_token(secrets)
    ensure_ldap_mail_read()
    apply_oauth2_clients(config, secrets)
    write_stalwart_identity_secrets(secrets)


def parse_api_token(result: subprocess.CompletedProcess[str]) -> str:
    secret = cli_json_field(result, "result", "token", "secret")
    if secret:
        return secret
    # Compatibility with older Kanidm text output.
    value = (result.stdout or "").strip().splitlines()
    secret = next(
        (
            line.strip()
            for line in reversed(value)
            if line.strip()
            and " " not in line.strip()
            and not line.strip().startswith(("{", "}"))
            and "success" not in line.lower()
            and "displayed once" not in line.lower()
        ),
        "",
    )
    return secret


def ensure_ldap_token(secrets: dict) -> None:
    if str(secrets.get("LDAP_TOKEN") or "").strip() and str(secrets.get("LDAP_TOKEN_CREATED") or ""):
        return
    existing = kanidm_cli(
        "service-account", "get", "stalwart-ldap", "--name", "idm_admin"
    )
    if not cli_exists(existing):
        account = kanidm_cli(
            "service-account",
            "create",
            "stalwart-ldap",
            "Stalwart LDAP",
            "idm_admins",
            "--name",
            "idm_admin",
        )
        if not cli_ok(account, "already exists", "duplicate", "attributeuniqueness"):
            raise RuntimeError(
                "Could not create stalwart-ldap service account: "
                f"{cli_output(account).strip()[:800]}"
            )
        existing = kanidm_cli(
            "service-account", "get", "stalwart-ldap", "--name", "idm_admin"
        )
        if not cli_exists(existing):
            raise RuntimeError(
                "Created stalwart-ldap but it is still missing: "
                f"{cli_output(existing).strip()[:800]}"
            )
    token = kanidm_cli(
        "-o", "json",
        "service-account", "api-token", "generate",
        "stalwart-ldap", "stalwart", "--name", "idm_admin",
    )
    if not cli_ok(token):
        raise RuntimeError(
            "Could not generate stalwart-ldap API token: "
            f"{cli_output(token).strip()[:800]}"
        )
    secret = parse_api_token(token)
    if not secret:
        raise RuntimeError(
            "Kanidm generated an LDAP API token but its value could not be parsed:\n"
            f"{cli_output(token).strip()[:800]}"
        )
    secrets["LDAP_TOKEN"] = secret
    secrets["LDAP_TOKEN_CREATED"] = "1"
    save_yaml(SECRETS_PATH, secrets)


def ensure_ldap_mail_read() -> None:
    """Kanidm treats mail as PII; Stalwart's login filter needs to see it."""
    for group in ("idm_mail_servers", "idm_people_pii_read"):
        added = kanidm_cli(
            "group", "add-members", group, "stalwart-ldap", "--name", "idm_admin"
        )
        if not cli_ok(added, "already", "duplicate", "members already"):
            raise RuntimeError(
                f"Could not add stalwart-ldap to {group}: "
                f"{cli_output(added).strip()[:500]}"
            )


def apply_oauth2_clients(config: dict, secrets: dict) -> None:
    domain = str(config["kanidm"]["domain"])
    clients = oidc_clients(config)
    if not clients:
        print(
            "Warning: no OIDC client definitions found. In an engine deployment, "
            "check .kanidm-easy-deploy/integration/oidc-clients.d/ and ensure the "
            "application services are enabled in easydeploy-engine/engine.yaml.",
            file=sys.stderr,
        )
        return
    for client in clients:
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
        existing = kanidm_cli("system", "oauth2", "get", client_id, "--name", "idm_admin")
        if not cli_exists(existing):
            if public:
                created = kanidm_cli(
                    "system", "oauth2", "create-public", client_id, name, landing,
                    "--name", "idm_admin",
                )
            else:
                created = kanidm_cli(
                    "system", "oauth2", "create", client_id, name, landing,
                    "--name", "idm_admin",
                )
            if not cli_ok(created):
                raise RuntimeError(
                    f"Could not create OAuth2 client {client_id!r}: "
                    f"{(created.stderr or created.stdout or '').strip()[:500]}"
                )
        else:
            kanidm_cli(
                "system", "oauth2", "set-landing-url", client_id, landing,
                "--name", "idm_admin",
            )
            kanidm_cli(
                "system", "oauth2", "set-displayname", client_id, name,
                "--name", "idm_admin",
            )
        for url in redirects:
            result = kanidm_cli(
                "system", "oauth2", "add-redirect-url", client_id, url,
                "--name", "idm_admin",
            )
            if not cli_ok(result, "already", "duplicate"):
                raise RuntimeError(
                    f"Could not add redirect URL to OAuth2 client {client_id!r}: "
                    f"{(result.stderr or result.stdout or '').strip()[:500]}"
                )
        scopes = [str(item) for item in (client.get("scopes") or DEFAULT_OIDC_SCOPES) if str(item).strip()]
        if scopes:
            result = kanidm_cli(
                "system", "oauth2", "update-scope-map", client_id,
                "idm_all_persons", *scopes, "--name", "idm_admin",
            )
            if not cli_ok(result):
                raise RuntimeError(
                    f"Could not configure scopes for OAuth2 client {client_id!r}: "
                    f"{(result.stderr or result.stdout or '').strip()[:500]}"
                )
        if to_bool(client.get("prefer_short_username", True)):
            kanidm_cli(
                "system", "oauth2", "prefer-short-username", client_id,
                "--name", "idm_admin",
            )
        apply_oauth2_claim_maps(client_id, client)
        if to_bool(client.get("legacy_crypto")):
            kanidm_cli("system", "oauth2", "warning-enable-legacy-crypto", client_id)
        if to_bool(client.get("disable_pkce")):
            kanidm_cli("system", "oauth2", "warning-insecure-client-disable-pkce", client_id)
        if not public:
            shown = kanidm_cli(
                "-o", "json", "system", "oauth2", "show-basic-secret", client_id,
                "--name", "idm_admin",
            )
            value = cli_json_field(shown, "secret", "result")
            if not value:
                secret = (shown.stdout or "").strip().splitlines()
                value = next((line.strip() for line in reversed(secret) if line.strip() and not line.lower().startswith("name")), "")
            if value:
                secrets[f"OIDC_SECRET_{client_id.upper()}"] = value
                save_yaml(SECRETS_PATH, secrets)
                write_consumer_secret(client_id, value, domain)
        verified = kanidm_cli(
            "system", "oauth2", "get", client_id, "--name", "idm_admin"
        )
        if not cli_ok(verified):
            raise RuntimeError(
                f"OAuth2 client {client_id!r} could not be verified after apply: "
                f"{(verified.stderr or verified.stdout or '').strip()[:500]}"
            )
        apply_oauth2_client_image(config, client)
        print(f"  OIDC client ready: {client_id} → {landing}")
    remove_stale_oauth2_clients({str(item.get("client_id") or "").strip() for item in clients if isinstance(item, dict)})


STALE_OAUTH2_CLIENT_IDS = ("stalwart",)


def remove_stale_oauth2_clients(active_ids: set[str]) -> None:
    """Drop leftover clients from earlier wiring (confidential `stalwart` vs public `stalwart-webui`)."""
    clients_dir = INTEGRATION_DIR / "oidc-clients.d"
    for stale in STALE_OAUTH2_CLIENT_IDS:
        if stale in active_ids:
            continue
        sidecar = clients_dir / f"{stale}.yaml"
        if sidecar.is_file():
            sidecar.unlink()
            print(f"  Removed stale OIDC sidecar {sidecar.name}")
        existing = kanidm_cli("system", "oauth2", "get", stale, "--name", "idm_admin")
        if not cli_exists(existing):
            continue
        deleted = kanidm_cli("system", "oauth2", "delete", stale, "--name", "idm_admin")
        if not cli_ok(deleted, "nomatchingentries", "no matching", "item not found"):
            raise RuntimeError(
                f"Could not delete stale OAuth2 client {stale!r}: "
                f"{cli_output(deleted).strip()[:500]}"
            )
        print(f"  Removed stale OAuth2 client {stale}")


def oauth2_claim_maps_for(client_id: str, client: dict) -> list[dict]:
    raw = client.get("claim_maps")
    if isinstance(raw, list) and raw:
        return [item for item in raw if isinstance(item, dict)]
    if client_id == "opencloud" or client_id.startswith("opencloud-"):
        return DEFAULT_OPENCLOUD_CLAIM_MAPS
    return []


def apply_oauth2_claim_maps(client_id: str, client: dict) -> None:
    """Map Kanidm groups to application role strings (not UUID/SPN groups claims)."""
    for claim_map in oauth2_claim_maps_for(client_id, client):
        claim = str(claim_map.get("claim") or "").strip()
        if not claim:
            continue
        join = str(claim_map.get("join") or "array").strip() or "array"
        joined = kanidm_cli(
            "system", "oauth2", "update-claim-map-join", client_id, claim, join,
            "--name", "idm_admin",
        )
        if not cli_ok(joined, "already"):
            raise RuntimeError(
                f"Could not set claim-map join for {client_id!r} {claim!r}: "
                f"{cli_output(joined).strip()[:500]}"
            )
        for mapping in claim_map.get("mappings") or []:
            if not isinstance(mapping, dict):
                continue
            group = str(mapping.get("group") or "").strip()
            values = [
                str(item).strip()
                for item in (mapping.get("values") or [])
                if str(item).strip()
            ]
            if not group or not values:
                continue
            mapped = kanidm_cli(
                "system", "oauth2", "update-claim-map",
                client_id, claim, group, *values, "--name", "idm_admin",
            )
            if not cli_ok(mapped, "already"):
                raise RuntimeError(
                    f"Could not map {group!r} onto {client_id!r} claim {claim!r}: "
                    f"{cli_output(mapped).strip()[:500]}"
                )


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


def kanidm_server_image(config: dict) -> str:
    kanidm = config["kanidm"]
    return f"{kanidm.get('image', 'docker.io/kanidm/server')}:{kanidm.get('tag', DEFAULT_KANIDM_TAG)}"


def kanidm_container_health() -> str:
    result = subprocess.run(
        ["docker", "inspect", "kanidm", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{end}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return (result.stdout or "").strip()


def kanidm_healthcheck_ok() -> bool:
    result = subprocess.run(
        ["docker", "exec", "kanidm", "/sbin/kanidmd", "healthcheck"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def reindex_database_offline(config: dict) -> None:
    """Offline reindex — fixes some 1.11.x fresh-install index issues."""
    data_dir = str(config["kanidm"]["data_dir"])
    image = kanidm_server_image(config)
    print("Stopping Kanidm for offline database reindex…")
    subprocess.run(["docker", "stop", "kanidm"], check=False)
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{data_dir}:/data",
            image,
            "kanidmd",
            "database",
            "reindex",
            "-c",
            "/data/server.toml",
        ],
        check=True,
    )


def wait_for_kanidm_ready(timeout_sec: int = 300) -> None:
    """Wait until the server responds to health checks (Docker or kanidmd healthcheck)."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        health = kanidm_container_health()
        if health == "healthy" or kanidm_healthcheck_ok():
            return
        if health == "unhealthy":
            break
        time.sleep(5)
    raise RuntimeError(
        "Kanidm did not become healthy in time. "
        "Check: docker logs kanidm"
    )


def start_kanidm_stack() -> None:
    run_compose("up", "-d", "--remove-orphans")
    config = load_config()
    try:
        wait_for_kanidm_ready()
    except RuntimeError:
        print("Kanidm healthcheck failed; trying offline database reindex (common on 1.11.x fresh installs)…")
        reindex_database_offline(config)
        run_compose("up", "-d", "--remove-orphans")
        wait_for_kanidm_ready()


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
    start_kanidm_stack()
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
    print(f"idm_admin pass:  {secrets.get('IDM_ADMIN_PASSWORD')}  (people/groups/OIDC)")
    print(f"admin pass:      {secrets.get('ADMIN_PASSWORD')}  (domain branding)")
    admin = (config.get("users") or [{}])[0]
    username = admin.get("username", "operator")
    print(f"Person account:  {username} (portal + apps)")
    enrollment_path = STATE_DIR / "enrollment-links.yaml"
    if enrollment_path.is_file():
        links = load_yaml(enrollment_path)
        if links.get(username):
            print(f"Enrollment link: {links[username]}")
    print("  idm_admin is for break-glass admin only; use the person account above for daily login.")
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
    print("Create further people with: bash kanidm-cli.sh person create … --name idm_admin")
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
