import os
import json
from pathlib import Path

import discord
from dotenv import load_dotenv


load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage"
CONFIG_PATH = BASE_DIR / "config" / "channels.json"


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
    print(f"로그인 완료: {client.user}")
    print(f"감지 채널 수: {len(CHANNEL_MAP)}")


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

    for attachment in message.attachments:
        save_path = save_dir / attachment.filename

        await attachment.save(save_path)

        print("----- 파일 저장 완료 -----")
        print(f"카테고리: {category}")
        print(f"채널: {message.channel.name}")
        print(f"채널 ID: {message.channel.id}")
        print(f"작성자: {message.author}")
        print(f"작성자 ID: {message.author.id}")
        print(f"파일명: {attachment.filename}")
        print(f"저장경로: {save_path}")
        print(f"메시지 ID: {message.id}")
        print(f"업로드 시간: {message.created_at}")
        print("--------------------------")


if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN이 .env 파일에 없습니다.")

client.run(TOKEN)