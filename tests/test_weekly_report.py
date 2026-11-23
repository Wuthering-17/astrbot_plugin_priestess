"""Offline weekly image reminders: only fixture accounts and fake QQ clients."""
import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta

from test_upstream_sync import main, db, TEMP, package

weekly = importlib.import_module(package.__name__ + ".priestess_features.weekly_report")
calendar = importlib.import_module(package.__name__ + ".priestess_features.game_calendar.calendar")


class WeeklyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(TEMP.name) / self._testMethodName
        self.root.mkdir()
        await db.init_db(str(self.root / "fixture.db"), force=True)
        self.config = {"weekly_report_arknights_groups": "10001", "weekly_report_endfield_groups": "20002"}
        self.plugin = NS(plugin_config=self.config)
        self.service = weekly.WeeklyReport(self.plugin, self.root)
        self.plugin.weekly_report = self.service
        self.when = datetime(2026, 11, 1, 19, 0, tzinfo=main.CN)
        self.service.now = lambda: self.when
        self.bot = NS(self_id="99999", get_group_member_list=AsyncMock(return_value=[{"user_id": n, "nickname": f"群友{n}"} for n in ("11111", "22222", "33333", "44444", "55555")]), send_weekly_image=AsyncMock())
        self.stack = []
        for target, name, value in ((weekly, "REQUEST_GAP", 0), (weekly, "SEND_GAP", 0), (weekly, "build_report", AsyncMock(return_value=b"report")), (weekly, "build_pending_image", AsyncMock(return_value=b"pending"))):
            p = patch.object(target, name, value)
            self.stack.append(p)
            p.start()

    async def asyncTearDown(self):
        for p in reversed(self.stack): p.stop()
        await db._engine.dispose()

    async def bind(self, qqs=("11111", "22222", "33333", "44444")):
        async with await db.get_session() as session:
            for qq in qqs:
                u = db.SkUser(platform_user_id=qq, user_id=qq, cred="fixture", cred_token="fixture")
                session.add(u)
                await session.flush()
                for game in ("arknights", "endfield"):
                    session.add(db.Character(id=u.id, uid=f"{game}-{qq}", role_id=qq, nickname=qq, app_code=game, channel_master_id="1", isdefault=True))
            await session.commit()

    async def test_image_messages_only_and_rejected_send_is_failure(self):
        action = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"message_id": 1}})
        client = weekly._OneBotClient(NS(call_action=action), "qq")
        await client.send_weekly_image(10001, b"png", ["11111"])
        message = action.await_args.kwargs["message"]
        self.assertEqual([s["type"] for s in message], ["at", "image"])
        self.assertEqual(message[0]["data"]["qq"], "11111")
        action.return_value = {"status": "failed", "retcode": 100}
        with self.assertRaises(RuntimeError): await client.send_weekly_image(10001, b"png", [])

    async def test_progress_api_and_game_groups_with_personal_optout(self):
        await self.bind()
        await self.service.preferences.configure("33333", "粥 关")
        calls = []
        async def api(user, func, cred, *args):
            calls.append((user.platform_user_id, func))
            if user.platform_user_id == "44444": raise RuntimeError("fixture failed request")
            done = user.platform_user_id == "22222"
            return NS(campaign=NS(reward=NS(current=1800 if done else 600, total=1800)), weeklyMission=NS(score=100 if done else 30, total=100))
        with patch.object(weekly, "call_api_with_refresh", side_effect=api):
            await self.service.send_for_bot(self.bot)
        sends = self.bot.send_weekly_image.await_args_list
        self.assertEqual([(c.args[0], c.args[2]) for c in sends], [(10001, ["11111"]), (20002, ["11111", "33333"])])
        self.assertNotIn(("33333", weekly.SklandAPI.ark_card), calls)
        self.assertNotIn("55555", {qq for qq, _ in calls})

    async def test_preferences_survive_reload_and_do_not_affect_other_members(self):
        result = await self.service.preferences.configure("11111", "粥 关")
        self.assertIn("所有群", result)
        loaded = weekly.WeeklyReport(self.plugin, self.root).preferences
        self.assertFalse(loaded.enabled("11111", "arknights"))
        self.assertTrue(loaded.enabled("11111", "endfield"))
        self.assertTrue(loaded.enabled("22222", "arknights"))
        await loaded.configure("11111", "开")
        self.assertTrue(loaded.enabled("11111", "arknights"))
        await loaded.configure("11111", "关")
        self.assertFalse(loaded.enabled("11111", "endfield"))
        loaded.path.write_text("broken", "utf-8")
        self.assertFalse(loaded.enabled("22222", "arknights"))
        await loaded.configure("11111", "开")
        self.assertEqual(loaded.path.read_text("utf-8"), "broken")

    async def test_unknown_completed_or_unbound_does_not_mention(self):
        await self.bind()
        self.config["weekly_report_endfield_groups"] = ""
        progress = {"11111": None, "22222": (1800, 1800), "33333": (2000, 1800), "44444": None}
        async def fetch(qq, game):
            return progress[qq]
        with patch.object(self.service, "_fetch_progress", side_effect=fetch):
            await self.service.send_for_bot(self.bot)
        self.assertEqual(self.bot.send_weekly_image.await_args.args[2], [])
        with patch.object(weekly, "call_api_with_refresh", AsyncMock(return_value=NS(weeklyMission=NS(score=0, total=0)))):
            self.assertIsNone(await self.service._fetch_progress("11111", "endfield"))

    async def test_only_configured_groups_and_disabled_switches_skip_queries(self):
        self.config.update(weekly_report_arknights_groups="", weekly_report_endfield_groups="")
        await self.service.send_for_bot(self.bot)
        self.bot.get_group_member_list.assert_not_awaited()
        self.bot.send_weekly_image.assert_not_awaited()
        self.config.update(weekly_report_arknights_groups="10001", notify_weekly_mentions_arknights=False)
        await self.service.send_for_bot(self.bot)
        self.bot.get_group_member_list.assert_not_awaited()  # rendering is mocked
        self.assertEqual(self.bot.send_weekly_image.await_args.args[2], [])
        self.config.update(weekly_report_endfield_groups="20002", notify_all=False)
        self.bot.send_weekly_image.reset_mock()
        await self.service.send_for_bot(self.bot)
        self.bot.send_weekly_image.assert_not_awaited()

    async def test_success_is_persistent_partial_failure_retries_only_failed_group(self):
        self.config["weekly_report_arknights_groups"] = "10001,30003"
        self.config["weekly_report_endfield_groups"] = ""
        fail = {30003}
        async def send(gid, *args):
            if gid in fail: raise RuntimeError("fixture send failed")
        self.bot.send_weekly_image.side_effect = send
        await self.service.send_for_bot(self.bot)
        fail.clear()
        self.bot.send_weekly_image.reset_mock()
        reloaded = weekly.WeeklyReport(self.plugin, self.root)
        reloaded.now = self.service.now
        await reloaded.send_for_bot(self.bot)
        self.assertEqual([c.args[0] for c in self.bot.send_weekly_image.await_args_list], [30003])
        self.bot.send_weekly_image.reset_mock()
        await reloaded.send_for_bot(self.bot)
        self.bot.send_weekly_image.assert_not_awaited()

    async def test_each_game_is_separate_and_instruction_is_inside_image(self):
        self.stack[2].stop()
        self.stack[3].stop()
        self.plugin.wiki = NS(resolver=lambda: ({"calendar": {}}, None), data_dir=self.root)
        self.plugin.guide = NS(echo_store=NS(optout=lambda: set()))
        p = [{"qq": "11111", "name": "Alice <test>", "current": 600, "total": 1800}]
        with patch.object(weekly, "_essence_section", AsyncMock(return_value="")), patch.object(calendar, "ARKNIGHTS", NS(load=lambda: {"activities": [], "pools": []})), patch.object(weekly.ef_theme, "render_page", AsyncMock(return_value=b"fixture")) as render:
            await weekly.build_report(self.plugin, self.bot, 10001, game_key="arknights", pending=p)
            html = render.await_args.args[0]
            self.assertIn("明日方舟 <em>周报</em>", html)
            self.assertIn("/周常提醒 粥 关", html)
            self.assertIn("Alice &lt;test&gt;", html)
            self.assertNotIn("战争回响", html)
            await weekly.build_report(self.plugin, self.bot, 20002, game_key="endfield", pending=p)
            html = render.await_args.args[0]
            self.assertIn("终末地 <em>周报</em>", html)
            self.assertNotIn("剿灭作战", html)
            self.assertIn("/周常提醒 终 关", html)

    async def test_progress_failure_retries_without_repeating_previous_mentions(self):
        await self.bind(("11111", "22222"))
        self.config["weekly_report_endfield_groups"] = ""
        fail = {"22222"}
        async def fetch(qq, game):
            if qq in fail: raise RuntimeError("fixture unavailable")
            return 600, 1800
        with patch.object(self.service, "_fetch_progress", side_effect=fetch):
            await self.service.send_for_bot(self.bot)
            self.assertEqual(self.bot.send_weekly_image.await_args.args[2], ["11111"])
            self.bot.send_weekly_image.reset_mock()
            fail.clear()
            await self.service.send_for_bot(self.bot)
            self.assertEqual(self.bot.send_weekly_image.await_args.args[1:], (b"pending", ["22222"]))

    async def test_many_members_are_image_batches_and_cancel_during_query_is_respected(self):
        qqs = tuple(str(11111 + n) for n in range(25))
        await self.bind(qqs)
        self.config["weekly_report_endfield_groups"] = ""
        self.bot.get_group_member_list.return_value = [{"user_id": qq, "nickname": qq} for qq in qqs]
        async def fetch(qq, game):
            if qq == qqs[-1]: await self.service.preferences.configure(qqs[0], "粥 关")
            return 600, 1800
        with patch.object(self.service, "_fetch_progress", side_effect=fetch):
            await self.service.send_for_bot(self.bot)
        calls = self.bot.send_weekly_image.await_args_list
        self.assertEqual([len(c.args[2]) for c in calls], [20, 4])
        self.assertEqual([c.args[1] for c in calls], [b"report", b"pending"])
        self.assertNotIn(qqs[0], [qq for c in calls for qq in c.args[2]])

    async def test_preview_does_not_check_progress_or_emit_mentions(self):
        event = NS(get_platform_id=lambda: "qq", get_group_id=lambda: "10001", get_sender_id=lambda: "11111")
        with patch.object(weekly, "_current_bot", return_value=self.bot), patch.object(self.service, "_fetch_progress", AsyncMock()) as fetch:
            path = await self.service.preview(event, "粥")
            self.assertEqual(Path(path).read_bytes(), b"report")
            weekly.build_report.assert_awaited_once_with(self.plugin, self.bot, 10001, "11111", game_key="arknights")
            fetch.assert_not_awaited()
            self.bot.send_weekly_image.assert_not_awaited()

    async def test_sunday_window_and_next_week_delivery_reset(self):
        async def finish_wait(awaitable, **kwargs):
            awaitable.close()
            stop.set()
        for day, hour, expected in ((6, 18, 0), (0, 19, 0), (6, 19, 1)):
            self.when = datetime(2026, 10, 26, hour, tzinfo=main.CN) + timedelta(days=day)
            stop = asyncio.Event()
            with patch.object(self.service, "_send_all", AsyncMock()) as send, patch.object(weekly.asyncio, "wait_for", side_effect=finish_wait):
                await self.service.run(stop)
                self.assertEqual(send.await_count, expected)
        await self.service.send_for_bot(self.bot)
        self.bot.send_weekly_image.reset_mock()
        self.when += timedelta(days=7)
        await self.service.send_for_bot(self.bot)
        self.assertEqual(self.bot.send_weekly_image.await_count, 2)

    async def test_command_can_only_change_own_qq_preferences(self):
        stop = lambda: None
        event = NS(get_platform_name=lambda: "aiocqhttp", get_sender_id=lambda: "11111", get_message_str=lambda: "/周常提醒 粥 关", plain_result=lambda x: x, stop_event=stop)
        plugin = NS(weekly_report=self.service, _command_argument=main.SklandPlugin._command_argument)
        awaitables = main.SklandPlugin.cmd_weekly_mentions(plugin, event)
        replies = [r async for r in awaitables]
        self.assertIn("已关闭", replies[0])
        self.assertFalse(self.service.preferences.enabled("11111", "arknights"))
        self.assertTrue(self.service.preferences.enabled("22222", "arknights"))
        event.get_platform_name = lambda: "telegram"
        event.get_message_str = lambda: "/周常提醒 粥 开"
        replies = [r async for r in main.SklandPlugin.cmd_weekly_mentions(plugin, event)]
        self.assertIn("仅支持 QQ", replies[0])
        self.assertFalse(self.service.preferences.enabled("11111", "arknights"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
