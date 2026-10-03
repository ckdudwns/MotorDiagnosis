#!/usr/bin/env bash
# Run after fetching origin/main, passing the reviewed, immutable deployment SHA.
set -Eeuo pipefail
umask 077

repo=/home/ubuntu/MotorDiagnosis
baseline=68ee56498f8c2fe6d4b26d79b99d27020ddfa0f4
target=${1:-}
service=motordiagnosis
files=(motor_diagnosis/periodic_snapshots.py motor_diagnosis/server.py motor_diagnosis/web.py)
die() { printf 'Deployment stopped: %s\n' "$*" >&2; exit 1; }

[[ $target =~ ^[0-9a-f]{40}$ ]] || die 'Pass the exact 40-character reviewed commit SHA.'
[[ $(pwd -P) == "$repo" ]] || die "Run from $repo."
[[ $(git rev-parse --show-toplevel) == "$repo" ]] || die 'Unexpected Git repository.'
[[ $(git branch --show-current) == main ]] || die 'The production checkout must be on main.'
git diff --quiet || die 'Tracked working files have local changes; preserve and inspect them first.'
git diff --cached --quiet || die 'The index has local changes; preserve and inspect them first.'
git cat-file -e "$target^{commit}" || die 'Target commit is missing; fetch origin/main first.'
git merge-base --is-ancestor "$target" refs/remotes/origin/main || die 'Target is not in fetched origin/main.'
current=$(git rev-parse HEAD)
[[ $current == "$baseline" || $current == "$target" ]] || die "Unexpected current commit $current; inspect before deploying."
git merge-base --is-ancestor "$baseline" "$target" || die 'Target does not descend from the expected production baseline.'

# This release may change only the reviewed runtime files and their regression tests.
while IFS= read -r path; do
  case "$path" in
    motor_diagnosis/periodic_snapshots.py|motor_diagnosis/server.py|motor_diagnosis/web.py|\
    tests/test_ai2_dashboard.mjs|tests/test_pump_live_dashboard.mjs|tests/test_rf66_dashboard.mjs|\
    tests/test_snapshot_dashboard.mjs|tests/operations_ui.test.mjs|\
    tests/test_event_status_filter.py|tests/test_snapshot_measurement_export.py|\
    scripts/deploy_operations_ui.sh) ;;
    *) die "Unreviewed path in target: $path" ;;
  esac
done < <(git diff --name-only "$baseline" "$target")
for path in "${files[@]}"; do
  [[ -f $path && ! -L $path ]] || die "Expected a regular source file: $path"
  [[ $(git ls-tree "$target" -- "$path") == 100644\ blob\ * ]] || die "Unexpected target file type: $path"
done

# Untracked production files are allowed. Git's fast-forward refuses to overwrite them.
sudo -v
state=$(systemctl is-active "$service" 2>/dev/null || true)
case "$state" in active|inactive|failed) ;; *) die "Service is transitioning or unknown: $state" ;; esac
printf 'Current commit: %s\nTarget commit: %s\nPrevious service state: %s\n' "$current" "$target" "$state"
if [[ $current == "$target" ]]; then
  printf 'The requested code is already installed. Service state was not changed.\n'
  exit 0
fi

backup_root=/home/ubuntu/motordiagnosis-code-backups
[[ ! -L $backup_root ]] || die 'Backup root must not be a symbolic link.'
mkdir -p -- "$backup_root"
chmod 700 -- "$backup_root"
exec 9>"$backup_root/operations-ui.lock"
flock -n 9 || die 'Another operations UI deployment is running.'
backup=$(mktemp -d "$backup_root/operations-ui-20261003-XXXXXX")
mkdir -- "$backup/motor_diagnosis"
for path in "${files[@]}"; do cp -p -- "$path" "$backup/$path"; done
printf '%s\n' "$current" > "$backup/previous-commit.txt"
printf '%s\n' "$target" > "$backup/target-commit.txt"
printf '%s\n' "$state" > "$backup/previous-service-state.txt"
(cd "$backup" && sha256sum "${files[@]}" > SHA256SUMS)
printf 'Original code backup: %s\n' "$backup"

updated=0
rollback() {
  local result=$?
  trap - ERR
  if (( updated )); then
    printf 'Deployment check failed. Restoring only the three original runtime files.\n' >&2
    if (cd "$backup" && sha256sum --check --status SHA256SUMS); then
      for path in "${files[@]}"; do cp -p -- "$backup/$path" "$repo/$path" || exit "$result"; done
      if [[ $state == active ]]; then
        sudo systemctl restart "$service" || printf 'Service restart failed; inspect it manually.\n' >&2
      fi
      printf 'Runtime files restored from %s. Git HEAD remains %s; the three restored files are now local changes.\n' "$backup" "$target" >&2
      printf 'Do not reset or clean this checkout. Review the failure before retrying.\n' >&2
    else
      printf 'Backup integrity check failed; no restore attempted. Inspect %s manually.\n' "$backup" >&2
    fi
  fi
  exit "$result"
}
trap rollback ERR

git merge --ff-only "$target"
updated=1
.venv/bin/python -m py_compile "${files[@]}"
if [[ $state == active ]]; then
  sudo systemctl restart "$service"
  sudo systemctl is-active --quiet "$service"
  curl --fail --silent --show-error --retry 5 --retry-connrefused --retry-delay 1 --max-time 10 \
    http://127.0.0.1:8787/api/health > "$backup/health-local.json"
  curl --fail --silent --show-error --retry 5 --retry-connrefused --retry-delay 1 --max-time 10 \
    https://motordiagnosis-api.duckdns.org/api/health > "$backup/health-public.json"
  .venv/bin/python - "$backup" <<'PY'
import json
from pathlib import Path
import sys
for name in ('health-local.json', 'health-public.json'):
    with (Path(sys.argv[1]) / name).open() as stream:
        assert json.load(stream).get('ok') is True, name
PY
  printf 'Code deployed and health checks passed. Verify CSV download after logging in.\n'
else
  printf 'Code updated. The service was stopped and remains stopped; health was not checked.\n'
  printf 'When ready: sudo systemctl start motordiagnosis\n'
  printf 'Then: curl --fail https://motordiagnosis-api.duckdns.org/api/health\n'
fi
trap - ERR
printf 'Installed commit: %s\nCode backup: %s\n' "$(git rev-parse HEAD)" "$backup"
