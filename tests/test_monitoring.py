import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import test_bot
from bot import database, discord_bot as bot
from bot.monitoring import HealthMonitor
from bot.status import format_status


class MonitoringTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_bot.CollectionTests.setUp
    message = test_bot.CollectionTests.message

    def monitor(self, channel_id="99"):
        client = MagicMock()
        channel = SimpleNamespace(id=99, send=AsyncMock())
        client.get_channel.return_value = channel
        return HealthMonitor(client, {"2": "common"}, channel_id), channel

    async def fail_upload(self, attempts=3, error="TimeoutError"):
        await bot.process_message(self.message(), upload=False)
        for _ in range(attempts):
            database.mark_upload_attempt(1)
        database.mark_upload_failed(1, error)

    async def test_disabled_monitor_never_fetches_or_sends(self):
        await self.fail_upload()
        monitor, channel = self.monitor(None)
        await monitor.check_once()
        monitor.client.get_channel.assert_not_called()
        channel.send.assert_not_awaited()

    async def test_transient_failure_does_not_alert(self):
        await self.fail_upload(attempts=1)
        monitor, channel = self.monitor()
        await monitor.check_once()
        channel.send.assert_not_awaited()

    async def test_repeated_failure_and_recovery_sent_once_across_restart(self):
        await self.fail_upload()
        monitor, channel = self.monitor()
        await monitor.check_once()
        await monitor.check_once()
        restarted = HealthMonitor(monitor.client, {"2": "common"}, "99")
        await restarted.check_once()
        self.assertEqual(channel.send.await_count, 1)
        self.assertEqual(database.active_alerts("99"), {"uploads"})
        database.update_drive_info(1, "remote", None)
        await restarted.check_once()
        await restarted.check_once()
        self.assertEqual(channel.send.await_count, 2)
        self.assertIn("복구", channel.send.call_args.args[0])
        self.assertEqual(database.active_alerts("99"), set())
        self.assertEqual(channel.send.call_args.kwargs["allowed_mentions"].to_dict()["parse"], [])

    async def test_auth_failure_alerts_without_waiting_three_attempts(self):
        await self.fail_upload(attempts=1, error="AuthorizationRequired")
        monitor, channel = self.monitor()
        await monitor.check_once()
        self.assertEqual(channel.send.await_count, 1)

    async def test_failed_send_is_not_marked_delivered(self):
        await self.fail_upload()
        monitor, channel = self.monitor()
        channel.send.side_effect = OSError("offline")
        with self.assertRaises(OSError):
            await monitor.check_once()
        self.assertEqual(database.active_alerts("99"), set())
        channel.send.side_effect = None
        await monitor.check_once()
        self.assertEqual(database.active_alerts("99"), {"uploads"})

    async def test_history_failure_counter_and_recovery(self):
        monitor, channel = self.monitor()
        for _ in range(2):
            database.record_health("history:2", "Forbidden")
            await monitor.check_once()
        channel.send.assert_not_awaited()
        database.record_health("history:2", "Forbidden")
        await monitor.check_once()
        self.assertEqual(channel.send.await_count, 1)
        database.record_health("history:2")
        await monitor.check_once()
        self.assertEqual(channel.send.await_count, 2)
        snapshot = database.health_snapshot({"2": "common"})
        self.assertEqual(snapshot["checks"]["history:2"]["failures"], 0)
        self.assertIsNotNone(snapshot["checks"]["history:2"]["last_success_at"])

    async def test_status_is_aggregate_only_and_covers_empty_channel(self):
        await self.fail_upload()
        snapshot = database.health_snapshot({"2": "common", "8": "plc"})
        self.assertEqual(snapshot["totals"]["failed"], 1)
        self.assertEqual(snapshot["channels"][0]["collected"], 1)
        self.assertEqual(snapshot["channels"][1]["collected"], 0)
        self.assertNotIn("sample.txt", json.dumps(snapshot))
        self.assertNotIn("sha256", json.dumps(snapshot))
        text = format_status(snapshot)
        self.assertIn("실패·재시도 1건", text)
        self.assertIn("채널 8", text)

    async def test_status_command_only_in_configured_channel_and_throttled(self):
        monitor, channel = self.monitor()
        message = SimpleNamespace(author=SimpleNamespace(bot=False), channel=channel, content="!hub 상태")
        with patch("bot.monitoring.time.monotonic", return_value=100):
            self.assertTrue(await monitor.handle_status(message))
            self.assertTrue(await monitor.handle_status(message))
        self.assertEqual(channel.send.await_count, 1)
        message.channel = SimpleNamespace(id=1, send=AsyncMock())
        self.assertFalse(await monitor.handle_status(message))
        message.channel.send.assert_not_awaited()

    async def test_disabled_status_command_does_not_respond(self):
        monitor, channel = self.monitor(None)
        message = SimpleNamespace(author=SimpleNamespace(bot=False), channel=channel, content="!hub status")
        self.assertFalse(await monitor.handle_status(message))
        channel.send.assert_not_awaited()

    async def test_migrations_preserve_alert_state(self):
        database.record_health("history:2", "Forbidden")
        database.record_alert_delivery("99", "history:2", True)
        database.init_db()
        self.assertEqual(database.active_alerts("99"), {"history:2"})
        self.assertEqual(database.health_snapshot({})["checks"]["history:2"]["failures"], 1)
