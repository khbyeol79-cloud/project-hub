#!/usr/bin/env bash
set -euo pipefail
umask 077
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ "$(uname -s)" == Linux && "$EUID" -ne 0 ]] || fail 'Run on the Pi as the normal login user.'
DEPLOY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd -- "$DEPLOY_DIR/../.." && pwd -P)"
SERVICE_USER="$(id -un)"
SERVICE_GROUP="$(id -gn)"
[[ "$PROJECT_DIR" =~ ^/[A-Za-z0-9_./-]+$ ]] || fail 'Unsupported project path.'
[[ "$SERVICE_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ && "$SERVICE_GROUP" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || fail 'Unsupported account name.'
[[ -x "$DEPLOY_DIR/.venv/bin/python" ]] || fail 'Install the collector environment first using setup.sh.'
[[ -f "$PROJECT_DIR/project_hub.db" ]] || fail 'Collector database is missing.'
"$DEPLOY_DIR/.venv/bin/python" "$DEPLOY_DIR/service_runner.py" --check
TEMP_DIR="$(mktemp -d)"
cleanup() { rm -f -- "$TEMP_DIR/project-hub-backup.service" "$TEMP_DIR/project-hub-backup.timer"; rmdir -- "$TEMP_DIR"; }
trap cleanup EXIT
for name in project-hub-backup.service project-hub-backup.timer; do
    sed -e "s|@ROOT@|$PROJECT_DIR|g" -e "s|@DEPLOY@|$DEPLOY_DIR|g" \
        -e "s|@USER@|$SERVICE_USER|g" -e "s|@GROUP@|$SERVICE_GROUP|g" \
        "$DEPLOY_DIR/$name.in" > "$TEMP_DIR/$name"
done
systemd-analyze verify "$TEMP_DIR/project-hub-backup.service" "$TEMP_DIR/project-hub-backup.timer"
BACKUP_DIR="/var/backups/project-hub/backup-units-$(date +%Y%m%d-%H%M%S)-$$"
for name in project-hub-backup.service project-hub-backup.timer; do
    if sudo test -e "/etc/systemd/system/$name"; then
        sudo install -d -m 0700 -- "$BACKUP_DIR"
        sudo cp -p -- "/etc/systemd/system/$name" "$BACKUP_DIR/$name"
    fi
    sudo install -m 0644 -- "$TEMP_DIR/$name" "/etc/systemd/system/$name"
done
sudo systemctl daemon-reload
printf 'Backup units registered. Collector keeps running.\n'
printf 'First backup: sudo systemctl start project-hub-backup.service\n'
printf 'Daily schedule: sudo systemctl enable --now project-hub-backup.timer\n'
