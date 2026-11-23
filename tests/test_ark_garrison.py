"""Offline fixtures for Stronghold Protocol history and its season-scoped leaderboard."""

import importlib
import unittest
from unittest.mock import AsyncMock, patch

from test_upstream_sync import package

garrison = importlib.import_module(package.__name__ + ".priestess_features.ark_garrison")
models = importlib.import_module(package.__name__ + ".schemas.arknights.garrison")


def fixture_resources(activity="activity_a", season="测试赛季"):
    return models.GarrisonResources.model_validate({"resources": [{
        "versionInfo": {"activityId": activity, "versionId": "v1", "seasonName": season},
        "bandInfo": {"band": {"name": "测试策略"}},
    }]})


def fixture_history():
    record = {"id": "record_1", "activityId": "activity_a", "versionId": "v1", "completeState": 1,
              "trophyBefore": 24, "trophyAfter": 30, "bandId": "band", "gameEndTs": 1795320000}
    return models.GarrisonData.model_validate({
        "history": {"records": [record, {**record, "id": "record_2"}], "favourRecords": [record]},
        "career": {"totalTrophyCnt": 30, "totalSingleBattleCnt": 3, "totalSingleBattleWinCnt": 2,
                   "totalMultiBattleCnt": 0, "totalMultiBattleWinCnt": 0, "top1BandUseId": "band"},
        "gameUserInfo": {"name": "博士 <A>"},
    })


class GarrisonTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_handles_null_history_and_favorite_indices(self):
        resources = fixture_resources()
        empty = models.GarrisonData.model_validate({"history": None})
        self.assertEqual(models.GarrisonView.from_data(empty, resources, uid="1001").records, [])
        with self.assertRaisesRegex(ValueError, "共 1 条"):
            models.GarrisonView.from_data(fixture_history(), resources, uid="1001", favored=True, record_index=2)

    async def test_detail_and_pagination_use_real_renderer_inputs(self):
        service = garrison.ArkGarrison(garrison.CACHE_DIR)
        resources = fixture_resources()
        view = models.GarrisonView.from_data(fixture_history(), resources, uid="1001")
        self.assertEqual(garrison.parse_choice("收藏 详情 1 -r 2"), (True, False, False, 1, None))
        self.assertEqual(garrison.parse_choice("第2页"), (False, False, False, None, 2))
        self.assertIn("超出范围", (await service.history(view, False, False, 2))[1])
        with patch.object(garrison, "_picture", AsyncMock(return_value="fixture.webp")) as picture:
            self.assertEqual(await service.history(view, False, False, None), (["fixture.webp"], ""))
        html = picture.await_args.args[0]
        self.assertIn("<h1>博士</h1>", html)
        self.assertIn("测试策略", html)
        self.assertIn("30", html)

    async def test_season_switch_and_optout_do_not_republish_previous_rows(self):
        path = garrison.CACHE_DIR / "garrison-season-fixture"
        path.mkdir(exist_ok=True)
        service = garrison.ArkGarrison(path)
        first = fixture_resources()
        next_season = fixture_resources("activity_b", "下个赛季")
        player = garrison.Standing("1", "10", "博士", 30, 3, 2, "测试策略")
        bindings = {"1": {"10"}, "2": {"20"}}
        self.assertEqual(service._merge({"1": (first, player, True)}, bindings), 1)
        self.assertEqual(service._merge({"1": (first, None, False)}, bindings), 1)
        self.assertEqual(service._merge({"2": (next_season, None, True)}, bindings), 0)
        service.optout("1", True)
        self.assertEqual(service._merge({"1": (next_season, player, True)}, bindings), 0)
        self.assertEqual(service._load()["season"], "下个赛季")


if __name__ == "__main__":
    unittest.main(defaultTest="GarrisonTests")
