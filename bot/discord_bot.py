import asyncio
import hashlib
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from contextlib import closing
from pathlib import Path

import discord
from dotenv import load_dotenv

if __package__:
    from . import database, projects
    from .drive import CATEGORY_FOLDERS
    from .upload_queue import UploadWorker, write_metadata
    from .history import HistoryCollector
    from .monitoring import HealthMonitor
    from .file_search import SearchCommands
    from .content_search import ContentIndexer, initialize as initialize_content
    from . import message_archive
else:
    import database, projects
    from drive import CATEGORY_FOLDERS
    from upload_queue import UploadWorker, write_metadata
    from history import HistoryCollector
    from monitoring import HealthMonitor
    from file_search import SearchCommands
    from content_search import ContentIndexer, initialize as initialize_content
    import message_archive

BASE_DIR = Path(__file__).resolve().parent.parent
STORAGE_DIR = BASE_DIR / "storage"
LOG_DIR = BASE_DIR / "logs"
CONFIG_PATH = BASE_DIR / "config" / "channels.json"
logger = logging.getLogger("project-hub")
CHANNEL_MAP = {}
FORUM_MAP = {}
drive_executor = None
upload_worker = None
collection_lock = asyncio.Lock()


def load_channel_map(section="channels"):
    with CONFIG_PATH.open(encoding="utf-8") as stream:
        channels = json.load(stream).get(section, {})
    if not isinstance(channels, dict) or any(
        not key.isdecimal() or category not in CATEGORY_FOLDERS
        for key, category in channels.items()
    ):
        raise ValueError("channels.json의 채널 ID와 카테고리를 확인하세요.")
    if section == "channels" and CONFIG_PATH == BASE_DIR / "config" / "channels.json":
        channels.update(projects.TEAM_CHANNEL_IDS)
    return channels


def calculate_sha256(file_path):
    with open(file_path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_filename(filename):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename).rstrip(" .")
    return name[:150] or "attachment"


async def run_upload(file_id):
    await asyncio.get_running_loop().run_in_executor(drive_executor, upload_worker.upload, file_id)


async def retry_pending_once():
    for file_id in database.due_uploads():
        await run_upload(file_id)


async def retry_uploads():
    while True:
        try:
            await retry_pending_once()
            database.record_health("retry_queue")
        except Exception as exc:
            logger.error("RETRY_QUEUE_FAILED | error=%s", type(exc).__name__)
            database.record_health("retry_queue", type(exc).__name__)
        await asyncio.sleep(30)


class CollectorClient(discord.Client):
    retry_task = None
    history_task = None
    history_collector = None
    monitor_task = None
    health_monitor = None
    content_task = None
    content_indexer = None
    message_archive = None
    message_collector = None
    message_task = None

    async def setup_hook(self):
        alert_channel = os.getenv("DISCORD_ALERT_CHANNEL_ID", "").strip()
        if alert_channel and (not alert_channel.isascii() or not alert_channel.isdecimal() or int(alert_channel) <= 0):
            raise ValueError("DISCORD_ALERT_CHANNEL_ID must be a positive channel ID or empty")
        self.history_collector = HistoryCollector(self, CHANNEL_MAP, process_message, forums=FORUM_MAP)
        self.history_collector.initialize()
        self.retry_task = asyncio.create_task(retry_uploads())
        self.history_task = asyncio.create_task(self.history_collector.run())
        self.health_monitor = HealthMonitor(self, {**CHANNEL_MAP, **FORUM_MAP}, alert_channel)
        self.monitor_task = asyncio.create_task(self.health_monitor.run())
        await asyncio.to_thread(initialize_content)
        self.content_indexer = ContentIndexer(STORAGE_DIR)
        self.content_task = asyncio.create_task(self.content_indexer.run())
        await asyncio.to_thread(message_archive.initialize)
        self.message_archive = message_archive.MessageArchive(self, CHANNEL_MAP, FORUM_MAP)
        self.message_collector = HistoryCollector(self, CHANNEL_MAP, self.message_archive.save,
            forums=FORUM_MAP, cursor_store=message_archive, health_prefix='messages:')
        self.message_collector.initialize()
        self.message_task = asyncio.create_task(self.message_collector.run())

    async def close(self):
        if self.message_task:
            self.message_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.message_task
            self.message_task = None
        if self.content_task:
            self.content_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.content_task
            self.content_task = None
        if self.monitor_task:
            self.monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.monitor_task
            self.monitor_task = None
        if self.history_task:
            self.history_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.history_task
            self.history_task = None
        if self.retry_task:
            self.retry_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.retry_task
            self.retry_task = None
        await super().close()


intents = discord.Intents.default()
intents.message_content = True
client = CollectorClient(intents=intents)
search_commands = SearchCommands(client)


async def connect_project_channels():
    guild = client.get_guild(int(projects.GUILD_ID))
    if guild is None:
        return
    resolved = projects.discover(guild.text_channels)
    with closing(database.get_connection()) as db:
        saved = projects.stored_channels(db)
    current = {str(c.id) for c in guild.text_channels}
    resolved.update({cid: category for cid, category in saved.items() if cid in current})
    CHANNEL_MAP.update(resolved)
    for collector in (client.history_collector, client.message_collector):
        if collector:
            collector.channel_ids = tuple(CHANNEL_MAP)
            collector.forum_ids = tuple(dict.fromkeys([*FORUM_MAP, *resolved]))
            collector.initialize()
            collector.wakeup.set()
    if client.message_archive:
        client.message_archive.channels.update(CHANNEL_MAP)
    if client.health_monitor:
        client.health_monitor.channel_map.update(CHANNEL_MAP)
    logger.info('PROJECT_CHANNELS_READY | teams=%d', len(resolved))


@client.event
async def on_guild_channel_create(channel):
    if str(getattr(getattr(channel, 'guild', None), 'id', '')) == projects.GUILD_ID:
        await connect_project_channels()


@client.event
async def on_ready():
    await connect_project_channels()
    logger.info("Discord 로그인 완료 | bot=%s | 감지채널=%d", client.user, len(CHANNEL_MAP))
    logger.info("FORUM_COLLECTION | forums=%d", len(FORUM_MAP))
    if client.history_collector:
        client.history_collector.wakeup.set()
    if client.message_collector:
        client.message_collector.wakeup.set()
        logger.info('MESSAGE_ARCHIVE_READY | channels=%s | forums=%s | initial_hours=24',
                    len(CHANNEL_MAP), len(FORUM_MAP))
    await search_commands.sync({**CHANNEL_MAP, **FORUM_MAP})


@client.event
async def on_resumed():
    if client.history_collector:
        client.history_collector.wakeup.set()
    if client.message_collector:
        client.message_collector.wakeup.set()


@client.event
async def on_thread_create(thread):
    if str(thread.parent_id) in {**CHANNEL_MAP, **FORUM_MAP} and client.history_collector:
        client.history_collector.wakeup.set()
    if str(thread.parent_id) in {**CHANNEL_MAP, **FORUM_MAP} and client.message_collector:
        client.message_collector.wakeup.set()


async def collect_attachment(message, attachment, index, category):
    timestamp = message.created_at.strftime("%Y%m%d_%H%M%S")
    prefix = f"{timestamp}_{message.id}_{index}_"
    existing = database.find_attachment(str(message.id), str(attachment.id), prefix)
    if existing:
        return existing["id"], False
    save_dir = STORAGE_DIR / category
    save_dir.mkdir(parents=True, exist_ok=True)
    saved_filename = prefix + safe_filename(attachment.filename)
    save_path = save_dir / saved_filename
    temporary = save_path.with_suffix(save_path.suffix + ".part")
    try:
        await attachment.save(temporary)
        temporary.replace(save_path)
    finally:
        temporary.unlink(missing_ok=True)
    sha256 = await asyncio.to_thread(calculate_sha256, save_path)
    duplicate = database.find_existing_file(attachment.filename, sha256, category, message.guild.id if message.guild else None)
    channel_id = str(message.channel.id)
    metadata = {
        "discord_message_id": str(message.id),
        "discord_attachment_id": str(attachment.id),
        "discord_guild_id": str(message.guild.id) if message.guild else None,
        "discord_channel_id": channel_id,
        "discord_parent_channel_id": (
            str(message.channel.parent_id) if getattr(message.channel, "parent_id", None) else None
        ),
        "discord_channel": getattr(message.channel, "name", channel_id),
        "discord_author_id": str(message.author.id),
        "discord_author": str(message.author),
        "uploaded_at": message.created_at.isoformat(),
        "category": category,
        "original_filename": attachment.filename,
        "saved_filename": saved_filename,
        "local_path": str(save_path),
        "metadata_path": str(save_dir / "_metadata" / f"{timestamp}_{message.id}_{index}.json"),
        "file_size_bytes": save_path.stat().st_size,
        "sha256": sha256,
        "duplicate_of": duplicate["duplicate_of"],
        "version_group": duplicate["version_group"],
        "duplicate_type": duplicate["type"],
        "discord_url": (
            f"https://discord.com/channels/{message.guild.id}/{channel_id}/{message.id}"
            if message.guild else None
        ),
    }
    file_id = database.insert_file(metadata)
    write_metadata(database.get_file(file_id))
    return file_id, True


async def process_message(message, *, upload=True):
    channel_id = str(message.channel.id)
    parent_id = str(getattr(message.channel, "parent_id", ""))
    category = CHANNEL_MAP.get(channel_id) or CHANNEL_MAP.get(parent_id) or FORUM_MAP.get(parent_id)
    if message.author.bot or not message.attachments or category is None:
        return True
    complete = True
    for index, attachment in enumerate(message.attachments, start=1):
        try:
            async with collection_lock:
                if parent_id in FORUM_MAP or parent_id in CHANNEL_MAP:
                    database.initialize_channel_cursor(channel_id, 0)
                file_id, created = await collect_attachment(message, attachment, index, category)
            if created and upload:
                await run_upload(file_id)
            elif not created:
                logger.info("ATTACHMENT_ALREADY_RECORDED | file_id=%s", file_id)
        except Exception as exc:
            complete = False
            logger.error("FILE_PROCESS_FAILED | channel=%s | message_id=%s | error=%s",
                         channel_id, message.id, type(exc).__name__)
    return complete


@client.event
async def on_message(message):
    if client.message_archive:
        await client.message_archive.save(message)
    if client.health_monitor:
        try:
            await client.health_monitor.handle_status(message)
        except Exception as exc:
            logger.error("STATUS_RESPONSE_FAILED | error=%s", type(exc).__name__)
    await process_message(message)


async def archive_event(action, payload):
    if client.message_archive is None:
        return
    try:
        await getattr(client.message_archive, action)(payload)
        await asyncio.to_thread(database.record_health, 'messages:events')
    except Exception as error:
        logger.error('MESSAGE_EVENT_FAILED | action=%s | error=%s', action, type(error).__name__)
        await asyncio.to_thread(database.record_health, 'messages:events', type(error).__name__)


@client.event
async def on_raw_message_edit(payload):
    await archive_event('edited', payload)


@client.event
async def on_raw_message_delete(payload):
    await archive_event('deleted', payload)


@client.event
async def on_raw_bulk_message_delete(payload):
    await archive_event('deleted', payload)


def main():
    global CHANNEL_MAP, FORUM_MAP, drive_executor, upload_worker
    load_dotenv(BASE_DIR / ".env")
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN을 환경변수 또는 로컬 .env에 설정하세요.")
    CHANNEL_MAP = load_channel_map()
    FORUM_MAP = load_channel_map("forums")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[logging.FileHandler(LOG_DIR / "project-hub.log", encoding="utf-8"), logging.StreamHandler()],
    )
    database.init_db()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="drive") as executor:
        drive_executor = executor
        upload_worker = UploadWorker()
        try:
            client.run(token)
        finally:
            executor.submit(upload_worker.close).result()
            upload_worker = None
            drive_executor = None


if __name__ == "__main__":
    main()
