#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
compose=(podman compose --project-name "${HARNESSA_COMPOSE_PROJECT:-harnessa}" -f infra/compose.yaml)
failed=0

check() {
  local name="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf 'PASS %s\n' "$name"
  else
    printf 'FAIL %s\n' "$name"
    failed=1
  fi
}

check "Compose configuration" "${compose[@]}" config --quiet
check "PostgreSQL service" "${compose[@]}" exec -T db pg_isready -U "${POSTGRES_USER:-harnessa}" -d "${POSTGRES_DB:-harnessa}"
check "Redis service" "${compose[@]}" exec -T redis redis-cli ping
check "API readiness" curl --fail --silent --show-error --max-time 3 "http://127.0.0.1:${API_PORT:-8010}/health/ready"
check "LiteLLM readiness" "${compose[@]}" exec -T litellm python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4000/health/readiness', timeout=2)"

if [[ -n "${BOOTSTRAP_TOKEN:-}" && -n "${SECRET_KEY:-}" ]]; then
  printf 'PASS bootstrap and signing secrets are present (values hidden)\n'
else
  printf 'WARN bootstrap or signing secret is unset; do not use this configuration for production\n'
fi
if [[ "${COOKIE_SECURE:-false}" == "true" ]]; then
  printf 'PASS secure session cookies enabled\n'
else
  printf 'WARN secure session cookies are disabled; configure TLS before production use\n'
fi

exit "$failed"
