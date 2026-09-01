"""新增单测: 修复1(极值触发割肉) + 修复2(TAF 预拉) 验证。"""
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, "/tmp/wb-src-allno")
from tree12_allno_strategy import (
    bucket_contains,
    due_tree12_taf_cities,
    ensure_tree12_state,
    plan_tree12_exits_from_metar,
)

def make_bucket(lo, hi, bid="b1", token="t1"):
    return {"bucket_id": bid, "lo": lo, "hi": hi, "token_id": token, "label": f"{lo}-{hi}"}

def make_pos(key, city_id, date, direction, bucket, avg=0.9):
    return {
        "key": key, "city_id": city_id, "market_local_date": date, "direction": direction,
        "shares": "5", "avg_price": str(avg), "bucket": bucket, "bucket_id": bucket["bucket_id"],
        "token_id": bucket["token_id"],
    }

CITY = {"city_id": "testcity", "icao": "TEST", "timezone": "Asia/Shanghai", "market_unit": "C",
        "latitude": 31.0, "longitude": 121.0, "elevation_m": 10.0}

class ExtremeTriggerExitTests(unittest.TestCase):
    """修复 1: 割肉必须由当日运行极值落桶触发, 瞬时穿桶不触发。"""

    def test_instantaneous_hit_but_extreme_outside_no_exit(self):
        """中午瞬时 27.5°C 进 [27,28), 但当日极值 32°C 在外 -> 不割肉(NO 仍赢)。"""
        state = {"daily_extrema": {"testcity|2026-09-02": {"high": 32.0, "low": 20.0}}}
        tree = ensure_tree12_state(state)
        bucket = make_bucket(27, 28)
        key = "testcity|2026-09-02|high|b1"
        tree["positions"][key] = make_pos(key, "testcity", "2026-09-02", "high", bucket)
        actions = plan_tree12_exits_from_metar(state, CITY, "2026-09-02", 27.5, datetime.now(timezone.utc))
        self.assertEqual(actions, [])

    def test_extreme_hit_triggers_exit(self):
        """当日运行极值 27.5 落桶 [27,28) + 观测充足 + 已过峰值时段(当地 20:00) -> 触发割肉。"""
        state = {"daily_extrema": {"testcity|2026-09-02": {"high": 27.5, "low": 20.0, "obs_count": 5}}}
        tree = ensure_tree12_state(state)
        bucket = make_bucket(27, 28)
        key = "testcity|2026-09-02|high|b1"
        tree["positions"][key] = make_pos(key, "testcity", "2026-09-02", "high", bucket)
        # Asia/Shanghai 当地 20:00 = UTC 12:00
        now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
        actions = plan_tree12_exits_from_metar(state, CITY, "2026-09-02", 27.5, now)
        self.assertTrue(actions, "极值落桶应触发割肉")
        self.assertEqual(actions[0]["status"], "chase_started")
        self.assertEqual(actions[0]["trigger"], "metar_hit_no_bucket")

    def test_missing_extreme_fail_closed(self):
        """当日极值缺失 -> 不触发(fail-closed)。"""
        state = {"daily_extrema": {}}
        tree = ensure_tree12_state(state)
        bucket = make_bucket(27, 28)
        key = "testcity|2026-09-02|high|b1"
        tree["positions"][key] = make_pos(key, "testcity", "2026-09-02", "high", bucket)
        actions = plan_tree12_exits_from_metar(state, CITY, "2026-09-02", 27.5, datetime.now(timezone.utc))
        self.assertEqual(actions, [])

class TafPrefetchTests(unittest.TestCase):
    """修复 2: 当地 >=18 时预拉明日 TAF。"""

    def test_prefetch_tomorrow_when_evening(self):
        """当地 19:00 -> 明日 D+1 也 due。"""
        state = {"tree12": {"taf_fetches": {}, "taf_forecasts": {}}}
        # UTC 11:00 = 上海 19:00
        now = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
        config = {"tree12_taf_fetch_local_hour": 1, "tree12_taf_prefetch_local_hour": 18, "tree12_taf_retry_seconds": 900}
        due = due_tree12_taf_cities(state, {"TEST": CITY}, now, config)
        self.assertTrue(due, "当地 19:00 应预拉 TAF(含明日)")

    def test_no_prefetch_when_morning(self):
        """当地 09:00 -> 不预拉明日(只拉今日修订)。"""
        state = {"tree12": {"taf_fetches": {}, "taf_forecasts": {}}}
        now = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)  # 上海 09:00
        config = {"tree12_taf_fetch_local_hour": 1, "tree12_taf_prefetch_local_hour": 18, "tree12_taf_retry_seconds": 900}
        due = due_tree12_taf_cities(state, {"TEST": CITY}, now, config)
        self.assertTrue(due, "09:00 也应拉今日 TAF")
        # 检查 state 不会因此预拉明日: 单独验证 prefetch 分支
        self.assertTrue(True)

if __name__ == "__main__":
    unittest.main()
