"""Transition-only notifications; no messages are sent unless a channel is configured."""
import asyncio
import logging
import time

import discord

if __package__:
    from . import database
    from .status import format_status
else:
    import database
    from status import format_status

logger = logging.getLogger("project-hub")


def current_issues(snapshot, previous):
    issues = {}
    if snapshot["upload_alarm_count"] or ("uploads" in previous and snapshot["totals"].get("failed", 0)):
        issues["uploads"] = "Drive 업로드가 반복 실패하거나 인증 갱신이 필요합니다. 로컬 파일은 보존되어 있습니다."
    for key, check in snapshot["checks"].items():
        if check["failures"] >= 3 or (key in previous and check["failures"] > 0):
            issues[key] = "수집 이력 확인 또는 재시도 처리가 연속 실패했습니다. 채널 권한·저장 공간·서비스 로그를 확인하세요."
    return issues


class HealthMonitor:
    def __init__(self, client, channel_map, alert_channel_id=None):
        self.client = client
        self.channel_map = dict(channel_map)
        self.alert_channel_id = str(alert_channel_id) if alert_channel_id else None
        self.last_status_sent = None

    async def destination(self):
        channel = self.client.get_channel(int(self.alert_channel_id))
        return channel if channel is not None else await self.client.fetch_channel(int(self.alert_channel_id))

    async def check_once(self):
        if not self.alert_channel_id:
            return
        snapshot = database.health_snapshot(self.channel_map)
        previous = database.active_alerts(self.alert_channel_id)
        issues = current_issues(snapshot, previous)
        for key in sorted(set(issues) | previous):
            active = key in issues
            if active == (key in previous):
                continue
            text = (f"[Project Hub 장애] {key}\n{issues[key]}" if active else
                    f"[Project Hub 복구] {key}\n해당 오류가 해소되었습니다.")
            channel = await self.destination()
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
            database.record_alert_delivery(self.alert_channel_id, key, active)

    async def run(self):
        while True:
            await self.client.wait_until_ready()
            try:
                await self.check_once()
            except Exception as exc:
                logger.error("HEALTH_ALERT_FAILED | error=%s", type(exc).__name__)
            await asyncio.sleep(60)

    async def handle_status(self, message):
        if (message.author.bot or not self.alert_channel_id
                or str(message.channel.id) != self.alert_channel_id
                or getattr(message, "content", "").strip() not in ("!hub 상태", "!hub status")):
            return False
        now = time.monotonic()
        if self.last_status_sent is not None and now - self.last_status_sent < 30:
            return True
        text = format_status(database.health_snapshot(self.channel_map))
        # Bound the response to Discord's message limit without leaking any file contents.
        await message.channel.send(text[:1950], allowed_mentions=discord.AllowedMentions.none())
        self.last_status_sent = now
        return True
