from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from data_recorder import AppendOnlyRecorder, RecorderError, payload_sha256


class DataRecorderTests(unittest.TestCase):
    def test_append_only_envelope_has_hash_chain_and_dual_clocks(self) -> None:
        wall = iter(["2026-08-27T00:00:00+00:00", "2026-08-27T00:00:01+00:00", "2026-08-27T00:00:02+00:00"])
        monotonic = iter([101, 202])
        with tempfile.TemporaryDirectory() as directory:
            with AppendOnlyRecorder(
                Path(directory), date_utc="2026-08-27", stream="market_ws", source="polymarket",
                session_id="session-1", wall_clock=lambda: next(wall), monotonic_ns=lambda: next(monotonic),
            ) as recorder:
                first = recorder.append({"event_type": "book", "asset_id": "token-1"}, event_type="book")
                second = recorder.append({"event_type": "price_change", "asset_id": "token-1"}, event_type="price_change")
                health_path = recorder.write_health({"book_ready": True})

            self.assertEqual(first["sequence"], 1)
            self.assertEqual(second["sequence"], 2)
            self.assertIsNone(first["previous_payload_sha256"])
            self.assertEqual(second["previous_payload_sha256"], first["payload_sha256"])
            self.assertEqual(second["received_monotonic_ns"], 202)
            lines = recorder.part_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            persisted = [json.loads(line) for line in lines]
            self.assertEqual(persisted[0], first)
            self.assertEqual(persisted[1], second)
            health = json.loads(health_path.read_text(encoding="utf-8"))
            self.assertEqual(health["records_written"], 2)
            self.assertEqual(health["extra"]["book_ready"], True)
            self.assertTrue(health["safety"]["paper_only"])
            self.assertEqual(health["safety"]["orders_submitted"], 0)

    def test_same_session_cannot_overwrite_raw_part(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = AppendOnlyRecorder(Path(directory), date_utc="2026-08-27", stream="weather", source="checkwx", session_id="s1")
            first.close()
            with self.assertRaisesRegex(RecorderError, "session_part_already_exists"):
                AppendOnlyRecorder(Path(directory), date_utc="2026-08-27", stream="weather", source="checkwx", session_id="s1")

    def test_payload_hash_is_stable_across_object_key_order(self) -> None:
        self.assertEqual(payload_sha256({"b": 2, "a": 1}), payload_sha256({"a": 1, "b": 2}))

    def test_unsafe_path_components_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RecorderError, "invalid_stream"):
                AppendOnlyRecorder(Path(directory), date_utc="2026-08-27", stream="../unsafe", source="checkwx", session_id="s1")


if __name__ == "__main__":
    unittest.main()
