#!/usr/bin/env bash
# FishCloud production deployment: source-only sync, backup, health check, rollback.
set -Eeuo pipefail
export LC_ALL=C

ROOT="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${DEPLOY_HOST:-43.142.234.226}"
USER_NAME="${DEPLOY_USER:-ubuntu}"
PORT="${DEPLOY_PORT:-22}"
KEY="${SSH_KEY:-$HOME/Downloads/codex.pem}"
REMOTE_DIR="${REMOTE_DIR:-/opt/xianyu-auto-reply-fix}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose-cn.yml}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8090/health}"
KEEP_BACKUPS="${KEEP_BACKUPS:-8}"
ACTION="${1:-deploy}"
DRY_RUN=0
RELEASE="latest"
shift || true

usage() {
  cat <<'EOF'
Usage: scripts/deploy-production.sh [check|static|deploy|rollback] [--dry-run] [--release ID]

Environment: DEPLOY_HOST, DEPLOY_USER, DEPLOY_PORT, SSH_KEY, REMOTE_DIR,
COMPOSE_FILE, HEALTH_URL, KEEP_BACKUPS.

Use "static" for CSS/HTML/JS-only releases. It publishes without rebuilding
the container, creates a server-side static snapshot, and verifies health.

Safety: .env, data, logs, backups, static/uploads, and realtime logs are never
uploaded or deleted. Every release has a server-side snapshot.
EOF
}

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --release) RELEASE="${2:?--release needs an ID}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
case "$ACTION" in check|static|deploy|rollback) ;; *) usage >&2; exit 2;; esac

die() { echo "Deployment failed: $*" >&2; exit 1; }
info() { echo "==> $*"; }
[[ -f "$KEY" ]] || die "SSH key not found: $KEY"
[[ -f "$ROOT/$COMPOSE_FILE" ]] || die "Compose file not found: $ROOT/$COMPOSE_FILE"
command -v ssh >/dev/null || die "ssh is required"
command -v scp >/dev/null || die "scp is required"
command -v tar >/dev/null || die "tar is required"
key_mode="$(stat -f '%Lp' "$KEY" 2>/dev/null || stat -c '%a' "$KEY")"
[[ "$key_mode" == "600" ]] || die "SSH key must have mode 0600: chmod 600 '$KEY'"

SSH=(ssh -i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -p "$PORT")
SCP=(scp -i "$KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -P "$PORT")
TARGET="$USER_NAME@$HOST"
quote_args() { local out= arg; for arg; do printf -v out '%s%q ' "$out" "$arg"; done; printf '%s' "$out"; }
remote() { local args; args="$(quote_args "$@")"; "${SSH[@]}" "$TARGET" "bash -s -- $args"; }

ensure_source_published() {
  git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "Deploy from the Git working tree so source stays synchronized"
  local branch local_head remote_head
  branch="$(git -C "$ROOT" branch --show-current)"
  [[ -n "$branch" ]] || die "Deploy from a named Git branch"
  git -C "$ROOT" fetch origin "$branch" >/dev/null || die "Unable to verify the GitHub branch before deployment"
  [[ -z "$(git -C "$ROOT" status --porcelain)" ]] || die "Commit and push local changes before deployment"
  local_head="$(git -C "$ROOT" rev-parse HEAD)"
  remote_head="$(git -C "$ROOT" rev-parse "origin/$branch")"
  [[ "$local_head" == "$remote_head" ]] || die "Push $branch to GitHub before deployment"
}

check() {
  info "Checking SSH, Compose, sudo, and service health"
  remote "$REMOTE_DIR" "$COMPOSE_FILE" "$HEALTH_URL" <<'REMOTE'
set -Eeuo pipefail
test -d "$1"
test -f "$1/$2"
sudo -n true
docker compose version >/dev/null
curl -fsS "$3" >/dev/null
echo "Remote preflight passed"
REMOTE
}

rollback() {
  info "Rolling back source snapshot: $RELEASE"
  remote "$REMOTE_DIR" "$COMPOSE_FILE" "$HEALTH_URL" "$RELEASE" <<'REMOTE'
set -Eeuo pipefail
root="$1"; compose="$2"; health="$3"; requested="$4"; backups="$root/backups/deploy-source"
if [[ "$requested" == latest ]]; then
  snapshot="$(find "$backups" -mindepth 2 -maxdepth 2 -name source.tar.gz -printf '%T@ %p
' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2-)"
else
  snapshot="$backups/$requested/source.tar.gz"
fi
[[ -n "${snapshot:-}" && -f "$snapshot" ]] || { echo "No matching source snapshot" >&2; exit 1; }
sudo tar -xzf "$snapshot" --no-same-owner --no-same-permissions -C "$root"
cd "$root"
sudo docker compose -f "$compose" up -d --build --remove-orphans
for _ in $(seq 1 30); do curl -fsS "$health" >/dev/null && { echo "Rollback complete"; exit 0; }; sleep 2; done
exit 1
REMOTE
}

[[ "$ACTION" == check ]] && { check; exit 0; }
[[ "$ACTION" == rollback ]] && { [[ "$DRY_RUN" == 1 ]] && { info "Would roll back $RELEASE"; exit 0; }; rollback; exit 0; }

check
ensure_source_published
release_id="$(date -u +%Y%m%dT%H%M%SZ)"
if git -C "$ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then release_id+="-$(git -C "$ROOT" rev-parse --short HEAD)"; fi
archive="$(mktemp "${TMPDIR:-/tmp}/fishcloud-source.${release_id}.XXXXXX")"
archive+=".tar.gz"
trap 'rm -f "$archive"' EXIT

if [[ "$ACTION" == static ]]; then
  info "Packing static frontend only"
  tar -C "$ROOT" --exclude=static/uploads --exclude='*.pyc' --exclude='__pycache__' -czf "$archive" static
  if [[ "$DRY_RUN" == 1 ]]; then info "Would publish static release $release_id"; exit 0; fi
  remote_archive="/tmp/fishcloud-static-$release_id.tar.gz"
  "${SCP[@]}" "$archive" "$TARGET:$remote_archive"
  info "Publishing frontend without container rebuild"
  remote "$REMOTE_DIR" "$HEALTH_URL" "$remote_archive" "$release_id" "$KEEP_BACKUPS" <<'REMOTE'
set -Eeuo pipefail
root="$1"; health="$2"; archive="$3"; release="$4"; keep="$5"
backup="$root/backups/deploy-static/$release"
trap 'sudo rm -f "$archive"' EXIT
test -f "$archive"
sudo mkdir -p "$backup"
sudo tar -C "$root" --exclude=static/uploads -czf "$backup/static.tar.gz" static
sudo tar -xzf "$archive" --no-same-owner --no-same-permissions -C "$root"
curl -fsS "$health" >/dev/null
page_html="$(curl -fsS http://127.0.0.1:8090/static/index.html)"
grep -q 'static/css/app.css' <<<"$page_html"
find "$root/backups/deploy-static" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -nr | tail -n +$((keep + 1)) | cut -d' ' -f2- | xargs -r sudo rm -rf
echo "Static deployment complete: $release"
REMOTE
  info "Static deployment complete: $release_id"
  exit 0
fi

info "Packing source only"
tar -C "$ROOT" --exclude=.git --exclude=.env --exclude=data --exclude=logs --exclude=backups --exclude=trajectory_history --exclude=update_backup --exclude='*.log' --exclude=static/uploads --exclude='*.pyc' --exclude='__pycache__' -czf "$archive" .
if [[ "$DRY_RUN" == 1 ]]; then info "Would publish $archive as $release_id"; exit 0; fi

remote_archive="/tmp/fishcloud-source-$release_id.tar.gz"
info "Uploading source archive"
"${SCP[@]}" "$archive" "$TARGET:$remote_archive"
info "Deploying, rebuilding, and verifying health"
if ! remote "$REMOTE_DIR" "$COMPOSE_FILE" "$HEALTH_URL" "$remote_archive" "$release_id" "$KEEP_BACKUPS" <<'REMOTE'
set -Eeuo pipefail
root="$1"; compose="$2"; health="$3"; archive="$4"; release="$5"; keep="$6"
backup="$root/backups/deploy-source/$release"
cleanup() { sudo rm -f "$archive"; }
trap cleanup EXIT
[[ -f "$archive" ]] || { echo "Source archive missing" >&2; exit 1; }
sudo mkdir -p "$backup"
sudo tar -C "$root" --exclude=.env --exclude=data --exclude=logs --exclude=backups --exclude=trajectory_history --exclude=update_backup --exclude='*.log' --exclude=static/uploads -czf "$backup/source.tar.gz" .
sudo tar -xzf "$archive" --no-same-owner --no-same-permissions -C "$root"
cd "$root"
sudo docker compose -f "$compose" up -d --build --remove-orphans
for _ in $(seq 1 30); do
  if curl -fsS "$health" >/dev/null; then
    find "$root/backups/deploy-source" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p
' | sort -nr | tail -n +$((keep + 1)) | cut -d' ' -f2- | xargs -r sudo rm -rf
    echo "Deployment complete: $release"
    exit 0
  fi
  sleep 2
done
echo "Health check failed; restoring previous source" >&2
sudo tar -xzf "$backup/source.tar.gz" --no-same-owner --no-same-permissions -C "$root"
sudo docker compose -f "$compose" up -d --build --remove-orphans
exit 1
REMOTE
then
  die "Health check failed; previous source was restored"
fi
info "Deployment complete: $release_id"
