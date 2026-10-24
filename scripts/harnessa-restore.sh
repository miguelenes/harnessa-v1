#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 BACKUP_DIRECTORY [EMPTY_GIT_RESTORE_DIRECTORY] (target Compose database and artifacts volume must be empty)" >&2
  exit 2
fi
backup="$(cd "$1" && pwd)"
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
compose=(podman compose --project-name "${HARNESSA_COMPOSE_PROJECT:-harnessa}" -f infra/compose.yaml)
[[ -f "$backup/database.dump" && -f "$backup/artifacts.tar.gz" && -f "$backup/SHA256SUMS" ]] || { echo "backup is incomplete" >&2; exit 1; }
(cd "$backup" && sha256sum --check SHA256SUMS)
db_user="${POSTGRES_USER:-harnessa}"
db_name="${POSTGRES_DB:-harnessa}"
"${compose[@]}" up -d db redis >/dev/null
services_ready=0
for _ in {1..60}; do
  if "${compose[@]}" exec -T db pg_isready -U "$db_user" -d "$db_name" >/dev/null 2>&1 \
    && "${compose[@]}" exec -T redis redis-cli ping 2>/dev/null | grep -qx PONG; then
    services_ready=1
    break
  fi
  sleep 1
done
[[ "$services_ready" == 1 ]] || { echo "refusing restore: PostgreSQL and Redis did not become ready" >&2; exit 1; }
if [[ -f "$backup/repository-data.tar" && $# != 2 ]]; then
  echo "backup includes Git roots; provide a fresh staging directory as the second argument" >&2
  exit 2
fi
table_count="$("${compose[@]}" exec -T db psql -U "$db_user" -d "$db_name" -Atqc "SELECT count(*) FROM pg_tables WHERE schemaname='public'")"
[[ "$table_count" == 0 ]] || { echo "refusing restore: target database is not empty" >&2; exit 1; }
# Check the target artifact volume before changing the database. `run` creates
# the named volume if needed but does not start the API or run migrations.
"${compose[@]}" run --rm --no-deps api python -c 'from pathlib import Path; p=Path("/var/lib/harnessa/artifacts"); p.mkdir(parents=True, exist_ok=True); assert not any(p.iterdir()), "refusing restore: artifact volume is not empty"'
if [[ $# == 2 ]]; then
  [[ -f "$backup/repository-data.tar" ]] || { echo "backup contains no Git roots to restore" >&2; exit 2; }
  [[ ! -e "$2" ]] || [[ -d "$2" && -z "$(find "$2" -mindepth 1 -print -quit)" ]] || { echo "Git restore destination must be absent or empty" >&2; exit 1; }
  mkdir -m 700 -p "$2"
  python - "$backup/repository-data.tar" "$2" <<'PY'
import sys
import tarfile
from pathlib import Path, PurePosixPath

archive_path, destination = sys.argv[1:]
root = Path(destination).resolve()
with tarfile.open(archive_path, "r:") as archive:
    members = archive.getmembers()
    for member in members:
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts:
            raise SystemExit("Git archive contains an unsafe path")
    archive.extractall(root, members=members, filter="data")
PY
fi
"${compose[@]}" exec -T db pg_restore --no-owner --exit-on-error -U "$db_user" -d "$db_name" <"$backup/database.dump"

# Start the API to mount its named artifact volume and apply compatible migrations.
"${compose[@]}" up -d --build api
"${compose[@]}" exec -T api python -c 'import sys,tarfile; from pathlib import Path; root=Path("/var/lib/harnessa/artifacts").resolve(); archive=tarfile.open(fileobj=sys.stdin.buffer, mode="r|gz");
for member in archive:
 path=(root/member.name).resolve();
 assert path == root or root in path.parents, "archive path escapes artifact root";
 assert not (member.issym() or member.islnk() or member.isdev()), "archive contains a link or device";
 archive.extract(member, root, filter="data")' <"$backup/artifacts.tar.gz"
"${compose[@]}" up -d --build worker
printf 'Restore completed into the previously empty Compose database and artifact volume.\n'
if [[ $# == 2 ]]; then
  printf 'Git repositories restored to the isolated staging directory %s; review and map each repository before enabling adapters.\n' "$2"
fi
printf 'Restore secret values separately from the deployment secret manager, then run scripts/harnessa-doctor.sh.\n'
