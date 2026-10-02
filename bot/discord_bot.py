import os
import json
import logging
from datetime import datetime
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
    save_dir.mkdir(parents=True, exist_ok=True)

    for index, attachment in enumerate(message.attachments, start=1):
        try:
            timestamp = message.created_at.strftime("%Y%m%d_%H%M%S")

            original_filename = attachment.filename

            safe_filename = (
                f"{timestamp}_"
                f"{message.id}_"
                f"{index}_"
                f"{original_filename}"
            )

            save_path = save_dir / safe_filename

            await attachment.save(save_path)

            logger.info(
                "FILE_SAVED | category=%s | channel=%s | channel_id=%s | "
                "author=%s | author_id=%s | original=%s | saved=%s | "
                "message_id=%s",
                category,
                message.channel.name,
                message.channel.id,
                message.author,
                message.author.id,
                original_filename,
                save_path,
                message.id
            )

        except Exception:
            logger.exception(
                "FILE_SAVE_FAILED | channel=%s | message_id=%s | file=%s",
                message.channel.name,
                message.id,
                attachment.filename
            )


if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN이 .env 파일에 없습니다.")

client.run(TOKEN)