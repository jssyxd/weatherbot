"""修复1补全: 实时极值滚动更新 单测。"""
import sys
import unittest

sys.path.insert(0, "/tmp/wb-src-allno")
from metar_observer import update_daily_extrema_from_observation

CITY = {"city_id": "testcity", "icao": "TEST", "timezone": "Asia/Shanghai", "market_unit": "C"}

class ExtremeRollingUpdateTests(unittest.TestCase):
    def test_first_observation_sets_both(self):
        state = {}
        update_daily_extrema_from_observation(state, CITY, "2026-09-02", 27.5)
        d = state["daily_extrema"]["testcity|2026-09-02"]
        self.assertEqual(d["high"], 27.5)
        self.assertEqual(d["low"], 27.5)

    def test_new_high_updates_max(self):
        state = {}
        update_daily_extrema_from_observation(state, CITY, "2026-09-02", 27.5)
        update_daily_extrema_from_observation(state, CITY, "2026-09-02", 32.0)  # 中午创新高
        d = state["daily_extrema"]["testcity|2026-09-02"]
        self.assertEqual(d["high"], 32.0)
        self.assertEqual(d["low"], 27.5)

    def test_lower_temp_does_not_drop_high(self):
        state = {}
        update_daily_extrema_from_observation(state, CITY, "2026-09-02", 30.0)
        update_daily_extrema_from_observation(state, CITY, "2026-09-02", 24.0)  # 回落
        d = state["daily_extrema"]["testcity|2026-09-02"]
        self.assertEqual(d["high"], 30.0)
        self.assertEqual(d["low"], 24.0)

    def test_preserves_existing_warmup_extrema(self):
        state = {"daily_extrema": {"testcity|2026-09-02": {"high": 31.0, "low": 19.0, "market_local_date": "2026-09-02"}}}
        update_daily_extrema_from_observation(state, CITY, "2026-09-02", 33.0)  # 超过 warmup high
        d = state["daily_extrema"]["testcity|2026-09-02"]
        self.assertEqual(d["high"], 33.0)
        self.assertEqual(d["low"], 19.0)

if __name__ == "__main__":
    unittest.main()
