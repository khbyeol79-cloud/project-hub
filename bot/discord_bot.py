import os
import json
import hashlib
import logging
from pathlib import Path

import discord
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage"
LOG_DIR = BASE_DIR / "logs"
CONFIG_PATH = BASE_DIR / "config" / "channels.json"

LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(
            LOG_DIR / "project-hub.log",
            encoding="utf-8"
        ),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger("project-hub")


def load_channel_map():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)

    return config["channels"]


def calculate_sha256(file_path):
    sha256 = hashlib.sha256()

    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha256.update(chunk)

    return sha256.hexdigest()


CHANNEL_MAP = load_channel_map()

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)


@client.event
async def on_ready():
    logger.info(
        "Discord 로그인 완료 | bot=%s | 감지채널=%d",
        client.user,
        len(CHANNEL_MAP)
    )


@client.event
async def on_message(message):
    if message.author.bot:
        return

    if not message.attachments:
        return

    channel_id = str(message.channel.id)

    if channel_id not in CHANNEL_MAP:
        return

    category = CHANNEL_MAP[channel_id]

    save_dir = STORAGE_DIR / category
    metadata_dir = save_dir / "_metadata"

    save_dir.mkdir(parents=True, exist_ok=True)
    metadata_dir.mkdir(parents=True, exist_ok=True)

    for index, attachment in enumerate(message.attachments, start=1):

        try:
            timestamp = message.created_at.strftime("%Y%m%d_%H%M%S")

            original_filename = attachment.filename

            saved_filename = (
                f"{timestamp}_"
                f"{message.id}_"
                f"{index}_"
                f"{original_filename}"
            )

            save_path = save_dir / saved_filename

            await attachment.save(save_path)

            file_size = save_path.stat().st_size
            sha256 = calculate_sha256(save_path)

            if message.guild:
                discord_url = (
                    f"https://discord.com/channels/"
                    f"{message.guild.id}/"
                    f"{message.channel.id}/"
                    f"{message.id}"
                )
            else:
                discord_url = None

            metadata = {
                "discord_message_id": str(message.id),
                "discord_guild_id": (
                    str(message.guild.id)
                    if message.guild
                    else None
                ),
                "discord_channel_id": str(message.channel.id),
                "discord_channel": message.channel.name,

                "discord_author_id": str(message.author.id),
                "discord_author": str(message.author),

                "uploaded_at": message.created_at.isoformat(),

                "category": category,

                "original_filename": original_filename,
                "saved_filename": saved_filename,
                "local_path": str(save_path),

                "file_size_bytes": file_size,
                "sha256": sha256,

                "discord_url": discord_url
            }

            metadata_filename = (
                f"{timestamp}_{message.id}_{index}.json"
            )

            metadata_path = metadata_dir / metadata_filename

            with open(
                metadata_path,
                "w",
                encoding="utf-8"
            ) as f:
                json.dump(
                    metadata,
                    f,
                    ensure_ascii=False,
                    indent=2
                )

            logger.info(
                "FILE_SAVED | category=%s | channel=%s | "
                "author=%s | file=%s | size=%d | sha256=%s | "
                "message_id=%s",
                category,
                message.channel.name,
                message.author,
                original_filename,
                file_size,
                sha256,
                message.id
            )

        except Exception:
            logger.exception(
                "FILE_SAVE_FAILED | channel=%s | "
                "message_id=%s | file=%s",
                message.channel.name,
                message.id,
                attachment.filename
            )


if not TOKEN:
    raise RuntimeError(
        "DISCORD_BOT_TOKEN이 .env 파일에 없습니다."
    )

client.run(TOKEN)