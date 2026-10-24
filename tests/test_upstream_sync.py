"""Offline regression. Run with Python 3.12 and AstrBot 4.28.2 dependencies.

Set ASTRBOT_SOURCE to an AstrBot checkout if AstrBot is not installed as a package.
All state is created in a temporary directory; no live account is used.
"""
import asyncio
import importlib
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[1]
TEMP = tempfile.TemporaryDirectory(prefix="priestess-test-")
os.environ["ASTRBOT_ROOT"] = TEMP.name
if os.environ.get("ASTRBOT_SOURCE"):
    sys.path.insert(0, os.environ["ASTRBOT_SOURCE"])
package = types.ModuleType("priestess_under_test")
package.__path__ = [str(REPO)]
sys.modules[package.__name__] = package
main = importlib.import_module(package.__name__ + ".main")
echoes = importlib.import_module(package.__name__ + ".priestess_features.endfield_guide.echoes")
builds = importlib.import_module(package.__name__ + ".priestess_features.endfield_guide.builds")
renderer = importlib.import_module(package.__name__ + ".render_adapter")
db = importlib.import_module(package.__name__ + ".db")


class UpstreamSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = Path(TEMP.name) / self._testMethodName
        self.root.mkdir()
        await db.init_db(str(self.root / "fixture.db"), force=True)
        self.store = echoes.EchoStore(self.root / "echoes.json", self.root / "optout.json")
        self.builds = builds.BuildStore(self.root / "builds.json")

    async def asyncTearDown(self):
        await db._engine.dispose()

    async def bind(self, count):
        async with await db.get_session() as session:
            for i in range(count):
                user = db.SkUser(platform_user_id=str(i), cred="fixture", cred_token="fixture")
                session.add(user)
                await session.flush()
                session.add(db.Character(id=user.id, uid=str(i), role_id=str(i), nickname="fixture", app_code="endfield", channel_master_id="1", isdefault=True))
            await session.commit()

    async def test_five_workers_and_partial_save(self):
        await self.bind(56)
        active = peak = completed = 0
        sessions = set()
        snapshots = []
        original_save = self.store._save

        async def member(session, user_id, qq, data, stats, optout, builds, build_members, **kwargs):
            nonlocal active, peak, completed
            sessions.add(id(session))
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.002)
            data["members"][qq] = {"records": {}}
            stats["ok"] += 1
            active -= 1
            completed += 1

        def save(data):
            snapshots.append(completed)
            original_save(data)

        with patch.object(self.store, "_member", side_effect=member), patch.object(self.store, "_save", side_effect=save):
            stats = await self.store.collect()
        self.assertEqual(peak, 5)
        self.assertEqual(len(sessions), 5)
        self.assertIn(50, snapshots)
        self.assertEqual(snapshots[-1], 56)
        self.assertEqual(stats["ok"], 56)
        self.assertEqual(len(self.store.load()["members"]), 56)

    async def test_endpoint_failure_keeps_old_records_and_still_reads_builds(self):
        await self.bind(2)
        old = {"ts": 1, "records": {"old": {"time": 12}}}
        self.store._save({"members": {"0": old}, "targets": {}})
        requested = []

        async def fetch(url, user, char):
            requested.append((url, char.uid))
            if url == builds.CARD_URL:
                return {"detail": {"chars": []}}
            raise echoes.RecordError("fixture")

        with patch.object(echoes, "_fetch", side_effect=fetch), patch.object(echoes, "REQUEST_GAP", 0):
            stats = await self.store.collect(self.builds)
        self.assertEqual(self.store.load()["members"]["0"], old)
        self.assertEqual(stats["retained"], 1)
        self.assertEqual(stats["builds"], 2)
        self.assertEqual(stats["echoes_failed"], 2)
        self.assertEqual(stats["monument_failed"], 2)
        requested.clear()
        with patch.object(echoes, "_fetch", side_effect=fetch), patch.object(echoes, "REQUEST_GAP", 0):
            await self.store.collect(self.builds, builds_only=True)
        self.assertEqual({url for url, _ in requested}, {builds.CARD_URL})

    async def test_worker_failure_waits_for_cancellation_before_final_save(self):
        await self.bind(7)
        stopped = asyncio.Event()

        async def member(session, user_id, qq, data, *args, **kwargs):
            if qq == "0":
                await asyncio.sleep(0.01)
                raise RuntimeError("fixture worker stopped")
            try:
                data["members"][qq] = {"records": {}}
                await asyncio.sleep(100)
            finally:
                stopped.set()

        with patch.object(self.store, "_member", side_effect=member):
            with self.assertRaisesRegex(RuntimeError, "fixture worker stopped"):
                await self.store.collect()
        self.assertTrue(stopped.is_set())
        self.assertTrue(self.store.load()["members"])
        self.assertFalse(self.store.lock.locked())

    async def test_schedule_and_builds_are_independent(self):
        now = datetime(2026, 10, 25, 2, 59, tzinfo=main.CN)
        run, kind = main.SklandPlugin._next_guide_collection(now)
        self.assertEqual((run.hour, run.minute, kind), (3, 0, "builds"))
        run, kind = main.SklandPlugin._next_guide_collection(run)
        self.assertEqual((run.hour, run.minute, kind), (5, 30, "clears"))
        run, kind = main.SklandPlugin._next_guide_collection(run)
        self.assertEqual((run.hour, run.minute, kind), (17, 30, "clears"))
        run, kind = main.SklandPlugin._next_guide_collection(run)
        self.assertEqual((run.day, run.hour, kind), (26, 3, "builds"))
        plugin = main.SklandPlugin(types.SimpleNamespace())
        with patch.object(plugin.guide, "collect", AsyncMock()) as collect:
            await plugin._run_guide_collection("builds")
            collect.assert_awaited_once_with(builds_only=True)
            collect.reset_mock()
            with patch.object(plugin.guide.build_store, "current", return_value=True):
                await plugin._run_guide_collection("clears")
            collect.assert_awaited_once_with(teams_only=True)
            collect.reset_mock()
            with patch.object(plugin.guide.build_store, "current", return_value=False):
                await plugin._run_guide_collection("clears")
            collect.assert_awaited_once_with(teams_only=False)

    async def test_render_timeout_retry_and_clear_error(self):
        from playwright.async_api import TimeoutError as PageTimeout
        with patch.object(renderer, "_html_to_pic_once", AsyncMock(side_effect=[PageTimeout("fixture"), "ok.png"])) as render:
            self.assertEqual(await renderer._html_to_pic("html"), "ok.png")
            self.assertEqual(render.await_count, 2)
        with patch.object(renderer, "_html_to_pic_once", AsyncMock(side_effect=PageTimeout("fixture"))) as render:
            with self.assertRaisesRegex(RuntimeError, "生成图片超时"):
                await renderer._html_to_pic("html")
            self.assertEqual(render.await_count, 2)
        with patch.object(renderer, "_html_to_pic_once", AsyncMock(side_effect=ValueError("invalid HTML"))) as render:
            with self.assertRaises(ValueError):
                await renderer._html_to_pic("html")
            self.assertEqual(render.await_count, 1)

    async def test_reload_starts_existing_jobs_without_external_messages(self):
        from contextlib import ExitStack
        plugin = main.SklandPlugin(types.SimpleNamespace())
        names = ("auto_sign", "wiki_sync", "calendar", "guide_collection", "stamina_alert", "sanity_reminder", "weekly_report", "health")
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "init_db", AsyncMock()))
            starters = [stack.enter_context(patch.object(plugin, f"_start_{name}_loop")) for name in names]
            await plugin.initialize()
            for starter in starters:
                starter.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
