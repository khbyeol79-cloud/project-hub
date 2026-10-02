"""Ordered, resumable catch-up of attachments missed while offline."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord

if __package__:
    from . import database
else:
    import database

logger = logging.getLogger("project-hub")


class HistoryCollector:
    def __init__(self, client, channel_ids, process_message, *, forums=None, batch_size=100, interval=60):
        self.client = client
        self.channel_ids = tuple(str(value) for value in channel_ids)
        self.forum_ids = tuple(str(value) for value in (forums or {}))
        self.process_message = process_message
        self.batch_size = batch_size
        self.interval = interval
        self.wakeup = asyncio.Event()

    def initialize(self, now=None):
        now = now or datetime.now(timezone.utc)
        fallback = discord.utils.time_snowflake(now - timedelta(hours=24))
        for channel_id in self.channel_ids:
            database.initialize_channel_cursor(channel_id, fallback)

    async def scan_channel(self, channel_id, *, channel=None, report_health=True):
        cursor = database.get_channel_cursor(channel_id)
        if cursor is None:
            raise RuntimeError("History cursor must be initialized before live events")
        channel = channel or self.client.get_channel(int(channel_id))
        if channel is None:
            channel = await self.client.fetch_channel(int(channel_id))
        processed = 0
        async for message in channel.history(
            limit=self.batch_size, after=discord.Object(id=cursor), oldest_first=True
        ):
            # Local file + SQLite persistence is enough. Drive has its own durable queue.
            if not await self.process_message(message, upload=False):
                logger.error("HISTORY_MESSAGE_BLOCKED | channel=%s | message_id=%s", channel_id, message.id)
                raise AttachmentSaveFailed()
            database.advance_channel_cursor(channel_id, message.id)
            processed += 1
        if processed:
            logger.info("HISTORY_SCANNED | channel=%s | messages=%s", channel_id, processed)
        if report_health:
            database.record_health("history:" + str(channel_id))
        return processed == self.batch_size

    async def scan_forum(self, forum_id):
        forum = self.client.get_channel(int(forum_id))
        if forum is None:
            forum = await self.client.fetch_channel(int(forum_id))
        seen = set()
        more, failed = False, False

        async def scan_thread(thread):
            nonlocal more, failed
            if str(thread.parent_id) != forum_id or thread.id in seen:
                return
            seen.add(thread.id)
            try:
                database.initialize_channel_cursor(str(thread.id), 0)
                more = await self.scan_channel(str(thread.id), channel=thread, report_health=False) or more
            except Exception as exc:
                failed = True
                logger.error("FORUM_THREAD_FAILED | forum=%s | thread=%s | error=%s",
                             forum_id, thread.id, type(exc).__name__)

        for thread in await forum.guild.active_threads():
            await scan_thread(thread)
        async for thread in forum.archived_threads(limit=None):
            await scan_thread(thread)
        database.record_health("history:" + forum_id, "ThreadScanFailed" if failed else None)
        return more

    async def scan_once(self):
        more = False
        for channel_id in self.channel_ids:
            try:
                more = await self.scan_channel(channel_id) or more
            except Exception as exc:
                # Permission and connection failures on one channel must not block others.
                logger.error("HISTORY_SCAN_FAILED | channel=%s | error=%s", channel_id, type(exc).__name__)
                database.record_health("history:" + str(channel_id), type(exc).__name__)
        for forum_id in self.forum_ids:
            try:
                more = await self.scan_forum(forum_id) or more
            except Exception as exc:
                logger.error("FORUM_SCAN_FAILED | forum=%s | error=%s", forum_id, type(exc).__name__)
                database.record_health("history:" + forum_id, type(exc).__name__)
        return more

    async def run(self):
        while True:
            await self.client.wait_until_ready()
            self.wakeup.clear()
            more = await self.scan_once()
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=1 if more else self.interval)
            except asyncio.TimeoutError:
                pass


class AttachmentSaveFailed(RuntimeError):
    """Keep the history cursor before a partially collected message."""
