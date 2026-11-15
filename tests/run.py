#!/usr/bin/env python3
"""Behavioural tests for period-reconcile. Run: python3 tests/run.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "period_reconcile.py")
FIX = os.path.join(ROOT, "tests", "fixtures")


def run(*args, cwd=ROOT):
    return subprocess.run(
        [sys.executable, TOOL, *args], cwd=cwd, capture_output=True, text=True
    )


def report(*args):
    proc = run(*args, "--json")
    return proc, json.loads(proc.stdout)


class WindowChecks(unittest.TestCase):
    def test_clean_archive_passes(self):
        proc, rep = report(os.path.join(FIX, "clean"))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(rep["verdict"], "consistent")

    def test_tainted_daily_above_weekly_is_violation(self):
        proc, rep = report(os.path.join(FIX, "tainted"))
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(rep["windows"]["violations"], 1)
        v = rep["windows"]["worst"][0]
        self.assertEqual((v["repo"], v["check"], v["lower_period"], v["upper_period"]),
                         ("acme/beta", "daily > weekly", 500, 100))

    def test_window_pairs_counted(self):
        _, rep = report(os.path.join(FIX, "clean"))
        # 2 captures x (daily/weekly + weekly/monthly) = 4 pairs
        self.assertEqual(rep["windows"]["pairs"], 4)

    def test_violation_alone_fails_verdict(self):
        _, rep = report(os.path.join(FIX, "tainted"), "--max-outlier-rate", "1.0")
        self.assertEqual(rep["verdict"], "problems")
        self.assertIn("monotonicity", rep["problems"][0])


class RateChecks(unittest.TestCase):
    def test_gap_days_are_skipped_not_scored(self):
        _, rep = report(os.path.join(FIX, "gap"))
        self.assertEqual(rep["rates"]["pairs"], 0)
        self.assertEqual(rep["rates"]["skipped_non_consecutive_days"], 1)

    def test_elapsed_time_is_honoured(self):
        _, rep = report(os.path.join(FIX, "skew"))
        self.assertEqual(rep["rates"]["pairs"], 1)
        row = rep["rates"]["worst"][0]
        self.assertAlmostEqual(row["hours"], 25.0, places=1)
        self.assertAlmostEqual(row["norm_residual"], 0.0, places=2)
        self.assertLess(row["norm_ratio"], 0.01)

    def test_raw_and_normalized_differ(self):
        _, rep = report(os.path.join(FIX, "skew"))
        row = rep["rates"]["worst"][0]
        # 250 total growth, 240 reported for 25h -> raw drift is non-zero
        self.assertEqual(row["delta_total"], 250)
        self.assertEqual(row["raw_drift"], 10)
        # 10 stars of raw drift is inside 20% of 240, so it is not an outlier;
        # the point is that the row still carries the unnormalised number.
        self.assertEqual(rep["rates"]["raw_outliers"], 0)

    def test_tolerance_changes_outlier_count(self):
        _, rep = report(os.path.join(FIX, "skew"), "--tolerance", "0.5")
        self.assertEqual(rep["rates"]["norm_outliers"], 0)
        self.assertEqual(rep["rates"]["raw_outliers"], 0)


class Interface(unittest.TestCase):
    def test_json_is_parseable(self):
        proc = run(os.path.join(FIX, "clean"), "--json")
        json.loads(proc.stdout)
        self.assertEqual(proc.stderr, "")

    def test_exit_two_on_missing_path(self):
        proc = run("/nonexistent/archive")
        self.assertEqual(proc.returncode, 2)

    def test_exit_two_on_bad_tolerance(self):
        proc = run(os.path.join(FIX, "clean"), "--tolerance", "0")
        self.assertEqual(proc.returncode, 2)

    def test_exit_two_on_non_snapshot_json(self):
        tmp = tempfile.mkdtemp()
        try:
            with open(os.path.join(tmp, "junk.json"), "w") as fh:
                json.dump({"hello": 1}, fh)
            proc = run(os.path.join(tmp, "junk.json"))
            self.assertEqual(proc.returncode, 2)
        finally:
            shutil.rmtree(tmp)

    def test_duplicate_dates_rejected(self):
        tmp = tempfile.mkdtemp()
        src = os.path.join(FIX, "clean")
        try:
            for name in os.listdir(src):
                shutil.copy(os.path.join(src, name), tmp)
            shutil.copy(os.path.join(src, "2026-02-01.json"),
                        os.path.join(tmp, "2026-02-01-copy.json"))
            # date-named copies only; rename to a date-named duplicate
            os.rename(os.path.join(tmp, "2026-02-01-copy.json"),
                      os.path.join(tmp, "2026-02-03.json"))
            with open(os.path.join(tmp, "2026-02-03.json")) as fh:
                payload = json.load(fh)
            payload["date"] = "2026-02-01"
            with open(os.path.join(tmp, "2026-02-03.json"), "w") as fh:
                json.dump(payload, fh)
            proc = run(tmp)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("duplicate", proc.stderr)
        finally:
            shutil.rmtree(tmp)

    def test_top_limits_offenders(self):
        _, rep = report(os.path.join(FIX, "tainted"), "--top", "1")
        self.assertLessEqual(len(rep["rates"]["worst"]), 1)
        self.assertLessEqual(len(rep["windows"]["worst"]), 1)

    def test_replay_hash_is_stable(self):
        _, a = report(os.path.join(FIX, "clean"))
        _, b = report(os.path.join(FIX, "clean"))
        self.assertEqual(a["replay_sha256"], b["replay_sha256"])
        self.assertEqual(len(a["replay_sha256"]), 64)

    def test_plain_output_names_verdict(self):
        proc = run(os.path.join(FIX, "clean"))
        self.assertIn("Verdict", proc.stdout)
        self.assertIn("replay_sha256", proc.stdout)

    def test_clean_example_exits_zero(self):
        proc = run(os.path.join(ROOT, "examples", "in-clean"), "--json")
        self.assertEqual(proc.returncode, 0)
        committed = json.load(open(os.path.join(ROOT, "examples", "out", "clean-report.json")))
        self.assertEqual(json.loads(proc.stdout)["replay_sha256"], committed["replay_sha256"])

    def test_examples_are_reproducible(self):
        example = os.path.join(ROOT, "examples", "out", "report.json")
        proc = run(os.path.join(ROOT, "examples", "in"), "--json")
        committed = json.load(open(example))
        self.assertEqual(json.loads(proc.stdout)["replay_sha256"], committed["replay_sha256"])


if __name__ == "__main__":
    unittest.main(verbosity=2)