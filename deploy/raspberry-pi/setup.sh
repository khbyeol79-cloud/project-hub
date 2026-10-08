#!/usr/bin/env bash
# Run as the Raspberry Pi login user, not with sudo. Registration does not start the bot.
set -euo pipefail
umask 077

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ "$(uname -s)" == Linux ]] || fail 'Run this script on the Raspberry Pi (Linux).'
[[ "$EUID" -ne 0 ]] || fail 'Run as your normal login user: bash deploy/raspberry-pi/setup.sh'
[[ -d /run/systemd/system ]] || fail 'A running systemd system is required.'

DEPLOY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_DIR="$(cd -- "$DEPLOY_DIR/../.." && pwd -P)"
SERVICE_USER="$(id -un)"
SERVICE_GROUP="$(id -gn)"
[[ "$PROJECT_DIR" =~ ^/[A-Za-z0-9_./-]+$ ]] || fail 'Use a project path without spaces or special characters.'
[[ "$SERVICE_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || fail 'Unsupported user name.'
[[ "$SERVICE_GROUP" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || fail 'Unsupported group name.'
[[ -O "$PROJECT_DIR" && -w "$PROJECT_DIR" ]] || fail 'The project must be owned and writable by your login user.'
[[ -f "$PROJECT_DIR/requirements.txt" && -f "$PROJECT_DIR/bot/discord_bot.py" ]] || fail 'Place this folder at project-hub/deploy/raspberry-pi.'
for cmd in sudo systemctl systemd-analyze; do
    command -v "$cmd" >/dev/null || fail "Missing command: $cmd."
done
LOGROTATE_BIN="$(command -v logrotate || true)"
if [[ -z "$LOGROTATE_BIN" && -x /usr/sbin/logrotate ]]; then
    LOGROTATE_BIN=/usr/sbin/logrotate
fi
[[ -n "$LOGROTATE_BIN" ]] || fail 'Install logrotate first: sudo apt install logrotate'
UV_BIN="$(command -v uv || true)"
if [[ -z "$UV_BIN" && -x "$HOME/.local/bin/uv" ]]; then
    UV_BIN="$HOME/.local/bin/uv"
fi
STATE="$(systemctl show project-hub.service --property=ActiveState --value 2>/dev/null || true)"
case "$STATE" in
    active|activating|deactivating|reloading) fail 'Stop project-hub.service before updating its environment.' ;;
esac

VENV="$DEPLOY_DIR/.venv"
if [[ -e "$VENV" ]]; then
    [[ -x "$VENV/bin/python" ]] || fail 'Existing deployment .venv is not a Linux environment. Move it aside, then retry.'
else
    if command -v python3.11 >/dev/null; then
        python3.11 -m venv "$VENV"
    elif [[ -n "$UV_BIN" ]]; then
        "$UV_BIN" venv --seed --python 3.11 "$VENV"
    else
        fail 'Python 3.11 is required. Install Python 3.11 with venv support, or install uv (see README).'
    fi
fi
"$VENV/bin/python" -c 'import sys; assert sys.version_info[:2] == (3, 11), "Python 3.11 required"'
"$VENV/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"

# Never display or copy secret contents. Restrict existing local files to this user.
for name in .env credentials.json token.json; do
    file="$PROJECT_DIR/$name"
    if [[ -e "$file" || -L "$file" ]]; then
        [[ -f "$file" && ! -L "$file" && -O "$file" ]] || fail "$name must be a regular file owned by $SERVICE_USER."
        chmod 600 -- "$file"
    fi
done
"$VENV/bin/python" "$DEPLOY_DIR/service_runner.py" --check
mkdir -p -- "$PROJECT_DIR/logs" "$PROJECT_DIR/storage"

TEMP_DIR="$(mktemp -d)"
cleanup() {
    rm -f -- "$TEMP_DIR/project-hub.service" "$TEMP_DIR/project-hub.logrotate"
    rmdir -- "$TEMP_DIR"
}
trap cleanup EXIT
sed -e "s|@ROOT@|$PROJECT_DIR|g" -e "s|@DEPLOY@|$DEPLOY_DIR|g" \
    -e "s|@USER@|$SERVICE_USER|g" -e "s|@GROUP@|$SERVICE_GROUP|g" \
    "$DEPLOY_DIR/project-hub.service.in" > "$TEMP_DIR/project-hub.service"
sed -e "s|@ROOT@|$PROJECT_DIR|g" -e "s|@USER@|$SERVICE_USER|g" \
    -e "s|@GROUP@|$SERVICE_GROUP|g" \
    "$DEPLOY_DIR/project-hub.logrotate.in" > "$TEMP_DIR/project-hub.logrotate"
systemd-analyze verify "$TEMP_DIR/project-hub.service"
"$LOGROTATE_BIN" --debug --state /dev/null "$TEMP_DIR/project-hub.logrotate"

BACKUP_DIR="/var/backups/project-hub/$(date +%Y%m%d-%H%M%S)-$$"
for target in /etc/systemd/system/project-hub.service /etc/logrotate.d/project-hub; do
    if sudo test -e "$target"; then
        sudo install -d -m 0700 -- "$BACKUP_DIR"
        sudo cp -p -- "$target" "$BACKUP_DIR/$(basename -- "$target")"
    fi
done
sudo install -m 0644 -- "$TEMP_DIR/project-hub.service" /etc/systemd/system/project-hub.service
sudo install -m 0644 -- "$TEMP_DIR/project-hub.logrotate" /etc/logrotate.d/project-hub
sudo systemctl daemon-reload
printf '\nRegistered for user %s at %s. The bot was NOT started.\n' "$SERVICE_USER" "$PROJECT_DIR"
printf 'Check that logrotate.timer or cron runs logrotate daily.\n'
printf 'After stopping the same bot on the PC, enable and start it:\n'
printf '  sudo systemctl enable --now project-hub.service\n'
printf 'Then check Discord login and a real attachment upload (active alone is insufficient):\n'
printf '  sudo journalctl -u project-hub.service -n 60 --no-pager\n'
