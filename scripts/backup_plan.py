#!/usr/bin/env python3
"""Dynamic backup plan for kanidm-easy-deploy.

The Kanidm data directory is operator configuration (`kanidm.data_dir`), so the
plan cannot be a static backup-plan.yaml — it resolves the data dir from
deploy.yaml on every load. See easydeploy-lib/python/backup_plan.py.
"""

from __future__ import annotations

from pathlib import Path

import yaml

STATE_DIR = ".kanidm-easy-deploy"
DEFAULT_DATA_DIR = "/var/lib/kanidm"


def resolve_plan(project_root: Path) -> dict:
    data_dir = DEFAULT_DATA_DIR
    deploy_path = Path(project_root) / "deploy.yaml"
    if deploy_path.exists():
        config = yaml.safe_load(deploy_path.read_text()) or {}
        kanidm = config.get("kanidm") if isinstance(config, dict) else None
        configured = str((kanidm or {}).get("data_dir") or "").strip()
        if configured:
            data_dir = configured

    return {
        "service": "kanidm",
        "archive_prefix": "kanidm",
        "timer_name": "kanidm-easy-deploy-backup",
        "state_dir": STATE_DIR,
        "secrets_file": f"{STATE_DIR}/secrets.yaml",
        "hooks": {
            "apply": "apply.sh",
            "stop": "stop.sh",
            "start": "start.sh",
        },
        "persistent_paths": [
            "deploy.yaml",
            STATE_DIR,
            {"path": data_dir, "as": "data/kanidm"},
        ],
    }
