"""Tests for the public refresh scheduling guard; no network or private state."""

from datetime import datetime
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


SPEC = spec_from_file_location("refresh_due", Path(__file__).with_name("refresh_due.py"))
GUARD = module_from_spec(SPEC)
sys.modules[SPEC.name] = GUARD
SPEC.loader.exec_module(GUARD)


def utc(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def batch(start="2026-11-23T06:30:00Z", end="2026-11-23T06:31:00Z"):
    return {"refresh_batch": {
        "version": 1, "id": start, "started_at": start, "completed_at": end,
        "status": "partial", "stock_counts": {"live": 52, "partial": 2},
        "providers": {
            "yahoo": {"status": "partial", "complete_stock_count": 52},
            "cnn": {"status": "live", "retrieved_at": end},
            "ecb": {"status": "live", "retrieved_at": end}
        }
    }}


class RefreshDueTests(unittest.TestCase):
    def decision(self, value, payload=None, **kwargs):
        return GUARD.decide_refresh("schedule", payload, now=utc(value), **kwargs)

    def test_winter_does_not_start_at_0630_berlin(self):
        result = self.decision("2026-11-23T05:30:00Z")
        self.assertFalse(result.refresh)
        self.assertEqual(result.reason, "before_0730_berlin")

    def test_winter_exact_0730_is_due(self):
        self.assertTrue(self.decision("2026-11-23T06:30:00Z").refresh)

    def test_summer_exact_0730_is_due(self):
        self.assertFalse(self.decision("2026-07-01T05:29:59Z").refresh)
        self.assertTrue(self.decision("2026-07-01T05:30:00Z").refresh)

    def test_dst_transition_dates(self):
        for before, due in [
            ("2026-03-28T06:29:59Z", "2026-03-28T06:30:00Z"),
            ("2026-03-29T05:29:59Z", "2026-03-29T05:30:00Z"),
            ("2026-10-24T05:29:59Z", "2026-10-24T05:30:00Z"),
            ("2026-10-25T06:29:59Z", "2026-10-25T06:30:00Z")
        ]:
            with self.subTest(due=due):
                self.assertFalse(self.decision(before).refresh)
                self.assertTrue(self.decision(due).refresh)

    def test_delayed_fallback_recovers_same_day(self):
        self.assertTrue(self.decision("2026-11-23T15:00:00Z").refresh)

    def test_honest_partial_stock_gaps_do_not_duplicate(self):
        result = self.decision("2026-11-23T07:30:00Z", batch())
        self.assertFalse(result.refresh)
        self.assertEqual(result.reason, "batch_already_current")

    def test_naive_or_invalid_batch_timestamps_are_not_fresh(self):
        for timestamp in [None, "2026-11-23T07:30:00", "not-a-date", "2026-99-31T00:00:00Z"]:
            value = batch()
            value["refresh_batch"]["completed_at"] = timestamp
            with self.subTest(timestamp=timestamp):
                self.assertTrue(self.decision("2026-11-23T07:30:00Z", value).refresh)

    def test_yesterday_and_pre_due_batches_do_not_suppress(self):
        for value in [batch("2026-11-22T15:00:00Z", "2026-11-22T15:01:00Z"),
                      batch("2026-11-23T06:29:59Z", "2026-11-23T06:31:00Z")]:
            with self.subTest(value=value):
                self.assertTrue(self.decision("2026-11-23T07:30:00Z", value).refresh)

    def test_reversed_future_and_mismatched_batches_do_not_suppress(self):
        cases = [batch("2026-11-23T06:32:00Z", "2026-11-23T06:31:00Z"),
                 batch("2026-11-23T06:30:00Z", "2026-11-23T08:00:00Z")]
        wrong_id = batch()
        wrong_id["refresh_batch"]["id"] = "other-batch"
        cases.append(wrong_id)
        for value in cases:
            with self.subTest(value=value):
                self.assertTrue(self.decision("2026-11-23T07:30:00Z", value).refresh)

    def test_total_yahoo_outage_does_not_suppress_retry(self):
        value = batch()
        value["refresh_batch"]["providers"]["yahoo"]["status"] = "unavailable"
        self.assertTrue(self.decision("2026-11-23T07:30:00Z", value).refresh)
        value["refresh_batch"]["providers"]["yahoo"] = {"status": "partial", "complete_stock_count": 0}
        self.assertTrue(self.decision("2026-11-23T07:30:00Z", value).refresh)

    def test_stale_cnn_or_ecb_does_not_suppress_retry(self):
        for provider in ("cnn", "ecb"):
            for key, replacement in [("status", "previous"), ("retrieved_at", "2026-11-22T06:30:00Z")]:
                value = batch()
                value["refresh_batch"]["providers"][provider][key] = replacement
                with self.subTest(provider=provider, key=key):
                    self.assertTrue(self.decision("2026-11-23T07:30:00Z", value).refresh)

    def test_manual_without_inputs_forces_even_before_due_and_when_fresh(self):
        for now in [utc("2026-11-23T01:00:00Z"), utc("2026-11-23T07:30:00Z")]:
            result = GUARD.decide_refresh("workflow_dispatch", batch(), now=now)
            self.assertTrue(result.refresh)
            self.assertEqual(result.reason, "manual_force")

    def test_correlated_dispatch_obeys_due_and_deduplication(self):
        args = dict(scheduler_day="2026-11-23", request_id="cf-123_456:789")
        self.assertFalse(GUARD.decide_refresh("workflow_dispatch", None, now=utc("2026-11-23T05:30:00Z"), **args).refresh)
        self.assertTrue(GUARD.decide_refresh("workflow_dispatch", None, now=utc("2026-11-23T06:30:00Z"), **args).refresh)
        self.assertFalse(GUARD.decide_refresh("workflow_dispatch", batch(), now=utc("2026-11-23T07:30:00Z"), **args).refresh)

    def test_correlated_dispatch_for_a_different_day_is_skipped(self):
        for day in ["2026-11-22", "2026-11-24"]:
            result = GUARD.decide_refresh("workflow_dispatch", None, scheduler_day=day, request_id="test", now=utc("2026-11-23T07:30:00Z"))
            self.assertFalse(result.refresh)
            self.assertEqual(result.reason, "different_berlin_day")

    def test_dispatch_day_is_berlin_calendar_day(self):
        result = GUARD.decide_refresh("workflow_dispatch", None, scheduler_day="2026-11-24", request_id="test", now=utc("2026-11-23T23:15:00Z"))
        self.assertEqual(result.scheduler_day, "2026-11-24")
        self.assertEqual(result.reason, "before_0730_berlin")

    def test_invalid_scheduler_inputs_fail_closed(self):
        for day, request in [("2026-11-23", ""), ("", "test"), ("2026-02-31", "test"),
                             ("23.11.2026", "test"), ("2026-11-23", "test\nrefresh=false"),
                             ("2026-11-23", "x" * 97)]:
            with self.subTest(day=day, request=request):
                with self.assertRaises(ValueError):
                    GUARD.decide_refresh("workflow_dispatch", None, scheduler_day=day, request_id=request)

    def test_push_never_refreshes(self):
        result = GUARD.decide_refresh("push", None, now=utc("2026-11-23T07:30:00Z"))
        self.assertFalse(result.refresh)
        self.assertEqual(result.reason, "deploy_only_push")

    def test_unknown_event_and_naive_clock_fail_closed(self):
        with self.assertRaises(ValueError):
            GUARD.decide_refresh("pull_request", None)
        with self.assertRaises(ValueError):
            GUARD.decide_refresh("schedule", None, now=datetime(2026, 11, 23, 8))

    def test_bad_or_missing_json_requests_a_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.json"
            self.assertIsNone(GUARD.read_public_data(path))
            for text in ["not JSON", '{"bad": NaN}']:
                path.write_text(text)
                self.assertIsNone(GUARD.read_public_data(path))

    def test_cli_writes_only_safe_github_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "outputs"
            env = dict(os.environ, GITHUB_OUTPUT=str(output))
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("refresh_due.py")),
                                     "--event-name", "workflow_dispatch", "--data", str(Path(tmp) / "missing.json")],
                                    env=env, text=True, capture_output=True, check=True)
            self.assertEqual(json.loads(result.stdout)["reason"], "manual_force")
            self.assertIn("refresh=true\nreason=manual_force\nscheduler_day=", output.read_text())

    def test_sidecar_is_derived_from_the_staged_dataset(self):
        workflow = Path(__file__).resolve().parents[1] / ".github/workflows/daily_update.yml"
        source = workflow.read_text().split("python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
        script = textwrap.dedent(source)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "_site").mkdir()
            published = batch()
            (root / "_site/data.json").write_text(json.dumps(published))
            (root / "data.json").write_text(json.dumps(batch("2026-11-22T07:00:00Z", "2026-11-22T07:01:00Z")))
            subprocess.run([sys.executable, "-c", script], cwd=root, check=True)
            self.assertEqual(json.loads((root / "_site/refresh-status.json").read_text()), published)


if __name__ == "__main__":
    unittest.main()
