#!/usr/bin/env python3
"""Read and write deploy.yaml for the Kanidm wizard."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEPLOY_PATH = PROJECT_ROOT / "deploy.yaml"

DEFAULT_GROUPS = [
    "opencloud-admin",
    "opencloud-user",
    "opencloud-guest",
    "matrix-admins",
    "mail-users",
]


def load_or_init(path: Path = DEFAULT_DEPLOY_PATH) -> dict:
    if not path.exists():
        example = PROJECT_ROOT / "deploy.yaml.example"
        if example.is_file():
            with example.open() as handle:
                return yaml.safe_load(handle) or {}
        return {}

    with path.open() as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("deploy.yaml root must be a mapping")
    return data


def save(path: Path, data: dict) -> None:
    with path.open("w") as handle:
        yaml.safe_dump(data, handle, default_flow_style=False, sort_keys=False)


def update_from_wizard(
    *,
    domain: str,
    data_dir: str,
    admin_username: str,
    admin_display_name: str,
    admin_email: str,
    admin_password: str | None,
    proxy_mode: str,
    admin_ui_enabled: bool = True,
    admin_ui_domain: str | None = None,
    path: Path = DEFAULT_DEPLOY_PATH,
) -> None:
    config = load_or_init(path)

    kanidm = config.setdefault("kanidm", {})
    kanidm["domain"] = domain
    kanidm.setdefault("image", "docker.io/kanidm/server")
    kanidm.setdefault("tag", "1.11.1")
    kanidm.setdefault("tools_image", "docker.io/kanidm/tools")
    kanidm.setdefault("tools_tag", "1.11.1")
    kanidm["data_dir"] = data_dir.rstrip("/")
    kanidm["ldap"] = True

    config["proxy"] = {
        "type": "caddy",
        "mode": proxy_mode,
        "integrate": {"network": "easydeploy-net"},
    }

    user_entry: dict[str, Any] = {
        "username": admin_username,
        "display_name": admin_display_name,
        "email": admin_email,
        "groups": ["opencloud-admin", "opencloud-user", "matrix-admins", "mail-users"],
    }
    if admin_password:
        user_entry["password"] = admin_password
    config["users"] = [user_entry]
    admin_ui: dict[str, Any] = {"enabled": bool(admin_ui_enabled)}
    if admin_ui_enabled and admin_ui_domain:
        admin_ui["domain"] = str(admin_ui_domain).strip().rstrip("/")
    config["admin_ui"] = admin_ui
    config["groups"] = list(DEFAULT_GROUPS)
    config["oidc"] = {"enabled": True, "clients": list((config.get("oidc") or {}).get("clients") or [])}

    save(path, config)


def main() -> None:
    parser = argparse.ArgumentParser(description="Update deploy.yaml from wizard")
    parser.add_argument("--deploy-yaml", type=Path, default=DEFAULT_DEPLOY_PATH)
    args = parser.parse_args()
    if not args.deploy_yaml.exists():
        raise SystemExit(f"Missing {args.deploy_yaml}")


if __name__ == "__main__":
    main()
