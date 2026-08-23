"""The rotation gate for LOCAL routes is a MEASUREMENT, not a name list.

local-bench.json (written by glimmer\\tools\\bench_local_models.py) records the
real generation rate of every local model on this machine. A local route is
held out of rotation when it measured below the file's floor or never produced
an answer; a route that measured fast is admitted even if its name is on the
hand-written fallback list. Without the file, the name list still applies.

Runs offline. No Ollama, no network.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

sys.argv = sys.argv[:1]
import flexfactor as F  # noqa: E402


def _write_bench(dirpath: str, models: list, floor: float = 5.0) -> None:
    with open(os.path.join(dirpath, "local-bench.json"), "w", encoding="utf-8") as fh:
        json.dump({"schema": 1, "slow_tok_per_s": floor, "models": models}, fh)


class MeasuredLocalGate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ff-bench-")
        os.environ["AITIME_STATE_DIR"] = self.dir
        os.environ.pop("FLEXFACTOR_ROTATION_EXCLUDE", None)
        F._LOCAL_BENCH_CACHE = None

    def tearDown(self):
        os.environ.pop("AITIME_STATE_DIR", None)
        os.environ.pop("FLEXFACTOR_ROTATION_EXCLUDE", None)
        F._LOCAL_BENCH_CACHE = None

    def test_slow_measurement_excludes(self):
        _write_bench(self.dir, [{"tag": "big-dense:30b", "ok": True,
                                 "gen_tok_per_s": 1.6, "answered": True}])
        why = F._rotation_excluded_reason("ollama/big-dense:30b")
        self.assertIn("1.6 tok/s", why)

    def test_fast_measurement_admits(self):
        _write_bench(self.dir, [{"tag": "moe:20b", "ok": True,
                                 "gen_tok_per_s": 18.4, "answered": True}])
        self.assertEqual(F._rotation_excluded_reason("ollama/moe:20b"), "")

    def test_measurement_outranks_the_name_list(self):
        # Glimmer is on the fallback name list; a FAST measurement wins.
        _write_bench(self.dir, [{"tag": "muse-glimmer:30b", "ok": True,
                                 "gen_tok_per_s": 40.0, "answered": True}])
        self.assertEqual(F._rotation_excluded_reason("ollama/muse-glimmer:30b"), "")

    def test_no_answer_excludes_even_if_fast(self):
        _write_bench(self.dir, [{"tag": "thinker:8b", "ok": True, "gen_tok_per_s": 30.0,
                                 "answered": False, "reasoning_only": True}])
        why = F._rotation_excluded_reason("ollama/thinker:8b")
        self.assertIn("no answer", why)
        self.assertIn("reasoning-only", why)

    def test_unmeasured_local_route_falls_back_to_name_list(self):
        _write_bench(self.dir, [{"tag": "something-else:1b", "ok": True,
                                 "gen_tok_per_s": 50.0, "answered": True}])
        self.assertTrue(F._rotation_excluded_reason("ollama/muse-glimmer:30b"))
        self.assertEqual(F._rotation_excluded_reason("ollama/qwen3-coder:30b"), "")

    def test_failed_measurement_falls_back_to_name_list(self):
        _write_bench(self.dir, [{"tag": "muse-glimmer:30b", "ok": False, "error": "x"}])
        self.assertTrue(F._rotation_excluded_reason("ollama/muse-glimmer:30b"))

    def test_cloud_routes_ignore_the_bench_file(self):
        _write_bench(self.dir, [{"tag": "meta/muse-glimmer-30b", "ok": True,
                                 "gen_tok_per_s": 0.1, "answered": True}])
        self.assertEqual(F._rotation_excluded_reason("nvidia_nim/meta/muse-glimmer-30b"), "")

    def test_missing_file_means_name_list_only(self):
        self.assertTrue(F._rotation_excluded_reason("ollama/muse-glimmer:30b"))
        self.assertEqual(F._rotation_excluded_reason("ollama/gpt-oss:20b"), "")

    def test_corrupt_file_is_not_an_error(self):
        with open(os.path.join(self.dir, "local-bench.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(F._rotation_excluded_reason("ollama/gpt-oss:20b"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
