#!/usr/bin/env bash
set -euo pipefail

if [[ $# != 1 ]]; then
  echo "usage: $0 BACKUP_DIRECTORY" >&2
  exit 2
fi
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
destination="$1"
[[ ! -e "$destination" ]] || { echo "backup destination already exists" >&2; exit 2; }
mkdir -m 700 -p "$destination"
destination="$(cd "$destination" && pwd)"
cd "$root"
compose=(podman compose --project-name "${HARNESSA_COMPOSE_PROJECT:-harnessa}" -f infra/compose.yaml)
api_container="$("${compose[@]}" ps -q api)"
[[ -n "$api_container" ]] || { echo "API container must exist before backup" >&2; exit 1; }
restart_services() { "${compose[@]}" start api worker >/dev/null 2>&1 || true; }
trap restart_services EXIT

"${compose[@]}" stop api worker
"${compose[@]}" exec -T db pg_dump -U "${POSTGRES_USER:-harnessa}" -d "${POSTGRES_DB:-harnessa}" -Fc >"$destination/database.dump"
mkdir -m 700 "$destination/artifacts"
podman cp "$api_container:/var/lib/harnessa/artifacts/." "$destination/artifacts"
tar --create --gzip --file "$destination/artifacts.tar.gz" --directory "$destination/artifacts" .
rm -rf "$destination/artifacts"
mkdir -m 700 "$destination/release-config"
cp infra/compose.yaml infra/litellm/config.yaml "$destination/release-config/"
git rev-parse HEAD >"$destination/source-revision.txt"

if [[ -n "${HARNESSA_GIT_DATA_ROOTS:-}" ]]; then
  IFS=: read -r -a git_roots <<<"$HARNESSA_GIT_DATA_ROOTS"
  relative_roots=()
  for source in "${git_roots[@]}"; do
    [[ "$source" == /* ]] || { echo "configured Git data roots must be absolute paths" >&2; exit 1; }
    [[ -d "$source/.git" || -f "$source/.git" ]] || { echo "configured Git data root is not a repository: $source" >&2; exit 1; }
    relative_roots+=("${source#/}")
  done
  tar --create --file "$destination/repository-data.tar" --directory / -- "${relative_roots[@]}"
fi

(cd "$destination" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum >SHA256SUMS)
printf 'Backup created at %s\n' "$destination"
printf 'Includes database, CAS artifacts, deployment config references, and optional configured Git roots. Secret values are excluded.\n'
