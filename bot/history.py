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
    def __init__(self, client, channel_ids, process_message, *, batch_size=100, interval=60):
        self.client = client
        self.channel_ids = tuple(str(value) for value in channel_ids)
        self.process_message = process_message
        self.batch_size = batch_size
        self.interval = interval
        self.wakeup = asyncio.Event()

    def initialize(self, now=None):
        now = now or datetime.now(timezone.utc)
        fallback = discord.utils.time_snowflake(now - timedelta(hours=24))
        for channel_id in self.channel_ids:
            database.initialize_channel_cursor(channel_id, fallback)

    async def scan_channel(self, channel_id):
        cursor = database.get_channel_cursor(channel_id)
        if cursor is None:
            raise RuntimeError("History cursor must be initialized before live events")
        channel = self.client.get_channel(int(channel_id))
        if channel is None:
            channel = await self.client.fetch_channel(int(channel_id))
        processed = 0
        async for message in channel.history(
            limit=self.batch_size, after=discord.Object(id=cursor), oldest_first=True
        ):
            # Local file + SQLite persistence is enough. Drive has its own durable queue.
            if not await self.process_message(message, upload=False):
                logger.error("HISTORY_MESSAGE_BLOCKED | channel=%s | message_id=%s", channel_id, message.id)
                database.record_health("history:" + str(channel_id), "AttachmentSaveFailed")
                return False
            database.advance_channel_cursor(channel_id, message.id)
            processed += 1
        if processed:
            logger.info("HISTORY_SCANNED | channel=%s | messages=%s", channel_id, processed)
        database.record_health("history:" + str(channel_id))
        return processed == self.batch_size

    async def scan_once(self):
        more = False
        for channel_id in self.channel_ids:
            try:
                more = await self.scan_channel(channel_id) or more
            except Exception as exc:
                # Permission and connection failures on one channel must not block others.
                logger.error("HISTORY_SCAN_FAILED | channel=%s | error=%s", channel_id, type(exc).__name__)
                database.record_health("history:" + str(channel_id), type(exc).__name__)
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
