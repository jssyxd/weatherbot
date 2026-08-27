from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import metar_observer as observer


class ExecutionBoundaryTests(unittest.TestCase):
    def test_audit_token_uses_yes_token_only(self) -> None:
        signal = {
            "bucket": {"bucket_id": "bucket-31", "yes_token_id": "yes-token", "no_token_id": "no-token"},
            "execution": {"side": "BUY_YES", "token_id": "yes-token"},
        }
        self.assertEqual(observer.signal_token_id(signal), "yes-token")

    def test_audit_token_never_falls_back_to_no_token(self) -> None:
        signal = {"bucket": {"bucket_id": "bucket-31", "no_token_id": "no-token"}}
        self.assertIsNone(observer.signal_token_id(signal))

    def test_tree6yes_rejects_legacy_execution_engines(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"scan_interval_seconds": 900, "execution_engine": "tree3", "stations": ["ZSPD"]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "已移除所有 NO 侧执行路径"):
                observer.load_config(path)

    def test_tree6yes_live_mode_has_no_executor_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"scan_interval_seconds": 900, "execution_engine": "tree6yes", "mode": "live", "stations": ["ZSPD"]}), encoding="utf-8")
            config = observer.load_config(path)
        self.assertEqual(config["execution_engine"], "tree6yes")
        self.assertEqual(config["mode"], "live")
        self.assertFalse(hasattr(observer, "enrich_execution"))


if __name__ == "__main__":
    unittest.main()
