from __future__ import annotations

import yaml

from scripts import config_edit


def _run_wizard(path):
    config_edit.update_from_wizard(
        domain="idm.acme.test",
        data_dir="/var/lib/kanidm",
        admin_username="olu",
        admin_display_name="Olu",
        admin_email="olu@acme.test",
        admin_password=None,
        proxy_mode="integrate",
        path=path,
    )
    return yaml.safe_load(path.read_text())


def test_wizard_drops_example_organisation_name(tmp_path):
    data = _run_wizard(tmp_path / "deploy.yaml")
    assert "display_name" not in (data.get("branding") or {})


def test_wizard_keeps_operator_chosen_name(tmp_path):
    path = tmp_path / "deploy.yaml"
    path.write_text(yaml.safe_dump({"branding": {"display_name": "Acme Corp"}}))
    assert _run_wizard(path)["branding"]["display_name"] == "Acme Corp"
