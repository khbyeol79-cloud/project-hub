import os
from pathlib import Path

import discord
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage"

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"로그인 완료: {client.user}")


@client.event
async def on_message(message):
    if message.author.bot:
        return

    if not message.attachments:
        return

    save_dir = STORAGE_DIR / "test"
    save_dir.mkdir(parents=True, exist_ok=True)

    for attachment in message.attachments:
        save_path = save_dir / attachment.filename
        await attachment.save(save_path)

        print("----- 파일 저장 -----")
        print(f"채널: {message.channel}")
        print(f"작성자: {message.author}")
        print(f"파일명: {attachment.filename}")
        print(f"저장경로: {save_path}")
        print(f"메시지 ID: {message.id}")


if not TOKEN:
    raise RuntimeError("DISCORD_BOT_TOKEN이 .env 파일에 없습니다.")

client.run(TOKEN)