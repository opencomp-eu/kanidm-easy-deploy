#!/usr/bin/env bash
# Run kanidm CLI against this kit's server (no manual compose flags needed).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib.sh
source "${SCRIPT_DIR}/scripts/lib.sh"

DEPLOY="${SCRIPT_DIR}/deploy.yaml"
COMPOSE_ENV="${SCRIPT_DIR}/.kanidm-easy-deploy/compose.env"

if [[ ! -f "${DEPLOY}" ]]; then
	die "Missing deploy.yaml — run wizard.sh or copy deploy.yaml.example first."
fi

if [[ ! -f "${COMPOSE_ENV}" ]]; then
	die "Missing ${COMPOSE_ENV} — run bash apply.sh once first."
fi

# shellcheck disable=SC1090
source "${COMPOSE_ENV}"

DATA_DIR="${KANIDM_DATA_DIR:?KANIDM_DATA_DIR not set in compose.env}"
TOOLS="${KANIDM_TOOLS_IMAGE:?KANIDM_TOOLS_IMAGE not set in compose.env}"
CONFIG="${DATA_DIR}/kanidm-client-config"
TOKENS="${DATA_DIR}/kanidm_tokens"

if [[ ! -f "${CONFIG}" ]]; then
	die "Missing ${CONFIG} — run bash apply.sh first."
fi

if [[ ! -f "${TOKENS}" ]]; then
	printf '{}\n' >"${TOKENS}"
	chmod 600 "${TOKENS}"
fi

interactive=0
if [[ $# -eq 0 ]]; then
	set -- --help
fi
case "${1:-}" in
	login | person | group | service-account | system | self)
		interactive=1
		;;
esac

docker_args=(--rm)
if [[ "${interactive}" -eq 1 ]]; then
	docker_args+=(-i)
	if [[ -t 0 ]]; then
		docker_args+=(-t)
	fi
fi

exec docker run "${docker_args[@]}" \
	--network kanidm-net \
	-v "${CONFIG}:/root/.config/kanidm:ro" \
	-v "${TOKENS}:/root/.cache/kanidm_tokens" \
	"${TOOLS}" \
	kanidm "$@"
