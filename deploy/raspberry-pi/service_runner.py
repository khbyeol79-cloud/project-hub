"""Offline readiness check and systemd entry point; never prints credentials."""

import argparse
import ast
import importlib
import json
import os
from pathlib import Path
import runpy
import sys

CONFIG_ERROR = 78
ROOT = Path(__file__).resolve().parents[2]
CATEGORIES = {"common", "plc", "robot", "vision", "3d_model", "arduino", "meeting", "final"}
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"


def load_object(path, errors):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        errors.append(f"{path.name}: missing, unreadable, or invalid JSON object.")
        return {}


def configuration_errors(root):
    """Check local inputs without authenticating, refreshing tokens, or opening a DB."""
    from dotenv import dotenv_values

    errors = []
    env_path = root / ".env"
    if not env_path.is_file():
        errors.append(".env is missing. Copy the local configuration to this project root.")
    else:
        try:
            values = dotenv_values(env_path, encoding="utf-8", interpolate=False)
            value = values.get("DISCORD_BOT_TOKEN")
            if not isinstance(value, str) or not value.strip() or value.strip() in {
                "YOUR_BOT_TOKEN", "YOUR_DISCORD_BOT_TOKEN", "replace_me"
            }:
                errors.append(".env: DISCORD_BOT_TOKEN is empty or a placeholder.")
        except (OSError, UnicodeError):
            errors.append(".env cannot be read as UTF-8.")

    channels = load_object(root / "config" / "channels.json", errors).get("channels")
    if not isinstance(channels, dict) or not channels:
        errors.append("channels.json: configure at least one collection channel.")
    elif any(not key.isascii() or not key.isdecimal() or int(key) <= 0
             or not isinstance(category, str) or category not in CATEGORIES
             for key, category in channels.items()):
        errors.append("channels.json: invalid channel ID or unsupported category.")

    credentials = load_object(root / "credentials.json", errors).get("installed", {})
    if not isinstance(credentials, dict) or not all(
        isinstance(credentials.get(key), str) and credentials[key]
        for key in ("client_id", "client_secret")
    ):
        errors.append("credentials.json: use the Google OAuth Desktop app credentials.")
        credentials = {}

    token = load_object(root / "token.json", errors)
    for key in ("refresh_token", "client_id", "client_secret"):
        if not isinstance(token.get(key), str) or not token[key]:
            errors.append(f"token.json: {key} is missing; authorize on the PC first.")
    if credentials and token.get("client_id") != credentials.get("client_id"):
        errors.append("token.json and credentials.json belong to different OAuth clients.")
    scopes = token.get("scopes", [])
    if isinstance(scopes, str):
        scopes = scopes.split()
    if not isinstance(scopes, list) or DRIVE_SCOPE not in scopes:
        errors.append("token.json: Drive file permission is missing; reauthorize on the PC.")
    if token:
        try:
            from google.oauth2.credentials import Credentials
            Credentials.from_authorized_user_info(token, [DRIVE_SCOPE])
        except (ValueError, TypeError, KeyError):
            errors.append("token.json: invalid Google credential format or expiry.")

    # Older revisions started the bot during import; require the new main entry point.
    try:
        tree = ast.parse((root / "bot" / "discord_bot.py").read_text(encoding="utf-8-sig"))
        if not any(isinstance(node, ast.FunctionDef) and node.name == "main" for node in tree.body):
            errors.append("Update bot/discord_bot.py from the PC: main() entry point is required.")
    except (OSError, SyntaxError, UnicodeError):
        errors.append("bot/discord_bot.py is missing or cannot be parsed.")
    for name in ("database.py", "drive.py"):
        if not (root / "bot" / name).is_file():
            errors.append(f"bot/{name} is missing.")

    for path in (root, root / "token.json", root / "project_hub.db", root / "storage", root / "logs"):
        if path.exists() and not os.access(path, os.W_OK):
            errors.append(f"Write permission is required for {path.name}.")
    if os.name == "posix":
        for name in (".env", "credentials.json", "token.json"):
            path = root / name
            if path.is_symlink():
                errors.append(f"{name}: use a regular file in the project root.")
            elif path.exists() and path.stat().st_mode & 0o077:
                errors.append(f"{name}: run chmod 600 on this file.")
    return errors


def check(root):
    errors = []
    if sys.version_info[:2] != (3, 11):
        errors.append("Python 3.11 is required for this deployment.")
    for module in ("discord", "dotenv", "google.auth", "google_auth_oauthlib.flow", "googleapiclient.discovery"):
        try:
            importlib.import_module(module)
        except ImportError:
            errors.append(f"Missing dependency: {module}. Run setup.sh.")
    if errors:
        return errors
    return configuration_errors(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="check local files only; no network calls")
    args = parser.parse_args()
    errors = check(ROOT)
    if errors:
        for message in errors:
            print(f"CONFIG: {message}", file=sys.stderr)
        return CONFIG_ERROR
    if args.check:
        print("Local checks passed. Discord login and Drive access still need a live test.")
        return 0
    if sys.platform != "linux":
        print("This service launcher is for Linux. Use --check for an offline check.", file=sys.stderr)
        return CONFIG_ERROR

    import fcntl
    import discord
    from google.auth.exceptions import RefreshError

    os.umask(0o077)
    os.chdir(ROOT)
    (ROOT / "logs").mkdir(exist_ok=True)
    # Held for the full process lifetime; prevents two service launchers on this copy.
    with (ROOT / "logs" / "service.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("CONFIG: Another service launcher is already running for this project.", file=sys.stderr)
            return CONFIG_ERROR
        sys.path.insert(0, str(ROOT))
        try:
            runpy.run_module("bot.discord_bot", run_name="__main__")
        except (discord.LoginFailure, discord.PrivilegedIntentsRequired):
            print("CONFIG: Check the Discord token and Message Content Intent, then restart the service.", file=sys.stderr)
            return CONFIG_ERROR
        except RefreshError as error:
            if getattr(error, "retryable", False):
                print("Temporary Google authorization error; systemd will retry.", file=sys.stderr)
                return 1
            print("CONFIG: Google authorization needs renewal on the PC. Replace token.json, then restart.", file=sys.stderr)
            return CONFIG_ERROR
    return 0


if __name__ == "__main__":
    sys.exit(main())
