#!/usr/bin/env bash
# wizard.sh — interactive setup for kanidm-easy-deploy
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"

EASYDEPLOY_INVOKE_ARGS=("$@")
clear_parent_python_env

DEPLOY_YAML="${SCRIPT_DIR}/deploy.yaml"
NO_APPLY=0
PROXY_MODE=""

usage() {
	echo "Usage: bash wizard.sh [--from-engine] [--no-apply] [--proxy-mode standalone|integrate]"
}

while [[ $# -gt 0 ]]; do
	case "$1" in
		--help|-h)
			usage
			exit 0
			;;
		--from-engine)
			NO_APPLY=1
			PROXY_MODE="integrate"
			shift
			;;
		--no-apply)
			NO_APPLY=1
			shift
			;;
		--proxy-mode)
			PROXY_MODE="${2:-}"
			shift 2
			;;
		--proxy-mode=*)
			PROXY_MODE="${1#*=}"
			shift
			;;
		*)
			die "Unknown option: $1"
			;;
	esac
done

print_banner() {
	echo
	echo -e "${BOLD}  Kanidm Easy Deploy — Setup Wizard${RESET}"
	echo -e "  ─────────────────────────────────────────────────────"
	echo
}

gather_config() {
	local domain data_dir
	local admin_username admin_display_name admin_email admin_password
	local proceed proxy_mode
	local base_domain

	print_banner
	echo -e "  Press Enter to accept a ${CYAN}[default]${RESET}.\n"
	print_data_dir_hint

	ask domain "Kanidm identity domain (e.g. idm.example.com)" "idm.example.com"
	base_domain="$(base_domain_from_host "$domain")"

	ask data_dir "Data directory" "$(default_data_dir kanidm)"

	echo
	echo -e "${BOLD}  Initial admin person${RESET}"
	echo "  Kanidm is the source of truth for users and groups."
	ask admin_username "Username" "admin"
	ask admin_display_name "Display name" "Admin"
	ask admin_email "Email" "admin@${base_domain}"
	ask_secret admin_password "Password (leave empty to auto-generate on apply)"

	echo
	echo -e "${BOLD}  Reverse proxy${RESET}"
	if [[ -n "${PROXY_MODE}" ]]; then
		proxy_mode="${PROXY_MODE,,}"
		info "Proxy mode: ${proxy_mode} (set by easydeploy-engine)"
	else
		echo "  standalone — this kit runs Caddy on :443 (single-service VPS)"
		echo "  integrate  — shared Caddy via easydeploy-engine (multi-service VPS)"
		ask proxy_mode "Proxy mode: standalone or integrate" "standalone"
		proxy_mode="${proxy_mode,,}"
	fi
	if [[ "$proxy_mode" != "standalone" && "$proxy_mode" != "integrate" ]]; then
		die "proxy mode must be 'standalone' or 'integrate'"
	fi

	echo
	echo -e "${BOLD}  Summary${RESET}"
	echo "  Portal:        https://${domain}"
	echo "  Data dir:      ${data_dir}"
	echo "  Admin person:  ${admin_username} <${admin_email}>"
	echo "  LDAP:          enabled (ldaps://kanidm:3636)"
	echo "  Proxy mode:    ${proxy_mode}"
	echo
	echo "  Ensure DNS A/AAAA for ${domain} points to this server before continuing."
	echo

	if [[ "${NO_APPLY}" == "1" ]]; then
		ask_yn proceed "Write deploy.yaml?" "y"
	else
		ask_yn proceed "Write deploy.yaml and deploy now?" "y"
	fi
	[[ "$proceed" == "y" ]] || {
		info "Cancelled."
		exit 0
	}

	cd "${SCRIPT_DIR}"
	uv run python - <<PY
from scripts.config_edit import update_from_wizard
from pathlib import Path

update_from_wizard(
    domain=${domain@Q},
    data_dir=${data_dir@Q},
    admin_username=${admin_username@Q},
    admin_display_name=${admin_display_name@Q},
    admin_email=${admin_email@Q},
    admin_password=${admin_password@Q} or None,
    proxy_mode=${proxy_mode@Q},
    path=Path(${DEPLOY_YAML@Q}),
)
PY

	success "Wrote ${DEPLOY_YAML}"
}

main() {
	bash "${SCRIPT_DIR}/ensure-dependencies.sh"
	ensure_docker_group_session "${EASYDEPLOY_INVOKE_ARGS[@]}"
	cd "${SCRIPT_DIR}"
	gather_config
	if [[ "${NO_APPLY}" == "1" ]]; then
		info "Skipping apply (--no-apply / --from-engine). easydeploy-engine will apply."
		return 0
	fi
	bash "${SCRIPT_DIR}/apply.sh"
}

main "$@"
