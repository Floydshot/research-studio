"""Synthetic public-data repair checks; no providers, credentials or private state."""

from copy import deepcopy
from pathlib import Path
import sys
from threading import Lock
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from refresh_repair import repair_incomplete_stocks


START = "2026-11-26T07:31:00+01:00"
FETCH = "2026-11-26T07:31:15+01:00"
OLD = "2026-11-25T07:31:15+01:00"
QUOTE = "2026-11-25T16:30:00+00:00"


def record(ticker, missing=(), **overrides):
    value = dict(ticker=ticker, price=100, as_of=QUOTE,
                 field_updated_at={"price": FETCH}, field_sources={"price": "live"},
                 returns=dict(source_status="live", as_of="2026-11-25", retrieved_at=FETCH),
                 dividend_growth=dict(source_status="live", end="2026-11-25", retrieved_at=FETCH),
                 price_returns=dict(source_status="live", as_of="2026-11-25", retrieved_at=FETCH))
    for field in missing:
        if field in {"returns", "dividend_growth", "price_returns"}:
            value[field]["source_status"] = "previous"
            value[field]["retrieved_at"] = OLD
        else:
            value["field_sources"][field] = "previous"
            value["field_updated_at"][field] = OLD
    value.update(overrides)
    return value


def groups(result):
    return [*result.get("portfolios", []), dict(id="universe", stocks=result.get("security_universe", {}).get("stocks", []))]


def finalize(result, started_at):
    for group in groups(result):
        for item in group["stocks"]:
            missing = []
            for field in ["price", "returns", "dividend_growth", *( ["price_returns"] if group["id"] == "universe" else [])]:
                if field == "price":
                    fresh = item.get("price") is not None and item.get("field_sources", {}).get(field) == "live" and item.get("field_updated_at", {}).get(field) == FETCH
                else:
                    fresh = item.get(field, {}).get("source_status") == "live" and item.get(field, {}).get("retrieved_at") == FETCH
                if not fresh:
                    missing.append(field)
            item["refresh"] = dict(status="partial" if missing else "live", missing_fields=missing)


class Clock:
    def __init__(self):
        self.value = 0.0
        self.sleeps = []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()

    def fixture(self, stocks, reserve=()):
        result = dict(portfolios=[dict(id="value", stocks=deepcopy(stocks))],
                      security_universe=dict(stocks=deepcopy(list(reserve))))
        finalize(result, START)
        return result

    def run_repair(self, result, fetcher, merge=None, **kwargs):
        seeds = {(group["id"], item["ticker"]): dict(ticker=item["ticker"], marker="seed")
                 for group in groups(result) for item in group["stocks"]}
        previous = {(group["id"], item["ticker"]): dict(ticker=item["ticker"], marker="original previous")
                    for group in groups(result) for item in group["stocks"]}
        return repair_incomplete_stocks(result, seeds=seeds, previous=previous,
                                        fetcher=fetcher, merge_stock=merge or (lambda seed, prior, payload, group, stamp: deepcopy(payload)),
                                        finalize_refresh_batch=finalize, started_at=START,
                                        clock=self.clock, sleeper=self.clock.sleep, **kwargs)

    def test_complete_snapshot_fetches_nothing(self):
        result = self.fixture([record("A")])
        before = deepcopy(result["portfolios"])
        metadata = self.run_repair(result, lambda ticker: self.fail("unexpected request"))
        self.assertEqual(result["portfolios"], before)
        self.assertEqual(metadata, dict(version=1, rounds=0, attempts=0, recovered_tickers=[], unresolved=[]))

    def test_recovery_replaces_one_coherent_record_only(self):
        result = self.fixture([record("A", ["returns"]), record("B")])
        untouched = deepcopy(result["portfolios"][0]["stocks"][1])
        candidate = record("A", price=105)
        metadata = self.run_repair(result, lambda ticker: candidate)
        self.assertEqual(metadata["recovered_tickers"], ["A"])
        self.assertEqual(metadata["attempts"], 1)
        self.assertEqual(metadata["unresolved"], [])
        self.assertEqual(result["portfolios"][0]["stocks"][0]["price"], 105)
        self.assertEqual(result["portfolios"][0]["stocks"][1], untouched)

    def test_duplicate_ticker_is_fetched_once_but_merges_each_group(self):
        result = self.fixture([record("A", ["returns"])])
        result["portfolios"].append(dict(id="classic", stocks=[record("A", ["returns"])]))
        finalize(result, START)
        calls, merged = [], []
        def merge(seed, previous, payload, group, stamp):
            merged.append((group, seed["marker"], previous["marker"], stamp))
            return deepcopy(payload)
        def fetch(ticker):
            calls.append(ticker)
            return record(ticker)
        metadata = self.run_repair(result, fetch, merge)
        self.assertEqual(calls, ["A"])
        self.assertEqual([row[0] for row in merged], ["value", "classic"])
        self.assertTrue(all(row[1:] == ("seed", "original previous", START) for row in merged))
        self.assertEqual(metadata["recovered_tickers"], ["A"])

    def test_reserve_history_is_included_and_repaired(self):
        result = self.fixture([], [record("O", ["price_returns"])])
        metadata = self.run_repair(result, lambda ticker: record(ticker))
        self.assertEqual(metadata["recovered_tickers"], ["O"])
        self.assertEqual(result["security_universe"]["stocks"][0]["refresh"]["status"], "live")

    def test_partial_improvement_can_finish_in_second_round(self):
        result = self.fixture([record("A", ["returns", "dividend_growth"])])
        calls = []
        def fetch(ticker):
            calls.append(ticker)
            return record(ticker, ["dividend_growth"] if len(calls) == 1 else [])
        metadata = self.run_repair(result, fetch)
        self.assertEqual(metadata["rounds"], 2)
        self.assertEqual(metadata["attempts"], 2)
        self.assertEqual(metadata["recovered_tickers"], ["A"])
        self.assertEqual(self.clock.sleeps, [2.0])

    def test_permanent_failure_is_bounded_and_preserves_exact_record(self):
        result = self.fixture([record("A", ["returns"])])
        original = deepcopy(result["portfolios"])
        def fail(ticker):
            raise RuntimeError("upstream detail must not become public metadata")
        metadata = self.run_repair(result, fail, max_rounds=100)
        self.assertEqual(metadata["rounds"], 2)
        self.assertEqual(metadata["attempts"], 2)
        self.assertEqual(metadata["unresolved"], [dict(ticker="A", fields=["returns"])])
        self.assertEqual(result["portfolios"], original)
        self.assertNotIn("upstream", str(metadata))

    def test_rejected_payload_never_relabels_old_timestamp(self):
        result = self.fixture([record("A", ["returns"])])
        original = deepcopy(result["portfolios"][0]["stocks"][0])
        candidate = record("A", ["returns"])
        metadata = self.run_repair(result, lambda ticker: candidate)
        self.assertEqual(result["portfolios"][0]["stocks"][0], original)
        self.assertEqual(result["portfolios"][0]["stocks"][0]["returns"]["retrieved_at"], OLD)
        self.assertEqual(metadata["recovered_tickers"], [])

    def test_tradeoff_is_not_an_improvement(self):
        result = self.fixture([record("A", ["returns"])])
        original = deepcopy(result["portfolios"])
        metadata = self.run_repair(result, lambda ticker: record(ticker, ["dividend_growth"]))
        self.assertEqual(metadata["recovered_tickers"], [])
        self.assertEqual(result["portfolios"], original)

    def test_no_older_or_invalid_quote_even_when_missing_field_recovers(self):
        for quote in ["2026-11-24T16:30:00+00:00", None, "2026-11-25T16:30:00"]:
            with self.subTest(quote=quote):
                result = self.fixture([record("A", ["returns"])])
                original = deepcopy(result["portfolios"])
                self.run_repair(result, lambda ticker: record(ticker, as_of=quote))
                self.assertEqual(result["portfolios"], original)

    def test_previously_fresh_history_cannot_roll_back(self):
        for field, date_key, missing, reserve in [
            ("returns", "as_of", ["price"], False),
            ("dividend_growth", "end", ["returns"], False),
            ("price_returns", "as_of", ["returns"], True),
        ]:
            for older in ["2026-11-24", None]:
                with self.subTest(field=field, older=older):
                    item = record("A", missing)
                    result = self.fixture([] if reserve else [item], [item] if reserve else [])
                    original = deepcopy(result)
                    candidate = record("A")
                    candidate[field][date_key] = older
                    metadata = self.run_repair(result, lambda ticker: candidate)
                    self.assertEqual(result["portfolios"], original["portfolios"])
                    self.assertEqual(result["security_universe"], original["security_universe"])
                    self.assertEqual(metadata["recovered_tickers"], [])

    def test_fetch_and_merge_mutations_cannot_change_seed_or_original_record(self):
        result = self.fixture([record("A", ["returns"])])
        original = deepcopy(result["portfolios"])
        def merge(seed, previous, payload, group, stamp):
            seed["marker"] = "changed"
            previous["marker"] = "changed"
            payload["returns"]["retrieved_at"] = "invalid"
            raise ValueError("reject")
        self.run_repair(result, lambda ticker: record(ticker), merge)
        self.assertEqual(result["portfolios"], original)

    def test_invalid_or_wrong_ticker_payload_is_ignored(self):
        for payload in [None, [], "not a payload", record("WRONG")]:
            with self.subTest(payload=payload):
                result = self.fixture([record("A", ["returns"])])
                original = deepcopy(result["portfolios"])
                self.run_repair(result, lambda ticker: deepcopy(payload))
                self.assertEqual(result["portfolios"], original)

    def test_budget_stops_before_next_wave_and_allows_only_last_inflight_wave(self):
        result = self.fixture([record(str(i), ["returns"]) for i in range(9)])
        calls, lock = [], Lock()
        def fetch(ticker):
            with lock:
                calls.append(ticker)
                if len(calls) % 4 == 0:
                    self.clock.value += 45
            return record(ticker)
        metadata = self.run_repair(result, fetch, budget_seconds=60)
        self.assertEqual(len(calls), 8)
        self.assertEqual(metadata["rounds"], 1)
        self.assertEqual(metadata["attempts"], 8)
        self.assertEqual(metadata["unresolved"], [dict(ticker="8", fields=["returns"])])
        self.assertEqual(self.clock.value, 90)

    def test_zero_budget_or_rounds_performs_no_fetch(self):
        for options in [dict(budget_seconds=0), dict(max_rounds=0)]:
            result = self.fixture([record("A", ["returns"])])
            metadata = self.run_repair(result, lambda ticker: self.fail("budget exhausted"), **options)
            self.assertEqual(metadata["attempts"], 0)
            self.assertEqual(metadata["rounds"], 0)

    def test_unresolved_duplicate_fields_are_unioned_without_duplicate_tickers(self):
        result = self.fixture([record("A", ["returns"])])
        result["portfolios"].append(dict(id="classic", stocks=[record("A", ["dividend_growth"])]))
        finalize(result, START)
        def fail(ticker):
            raise RuntimeError("permanent")
        metadata = self.run_repair(result, fail)
        self.assertEqual(metadata["unresolved"], [dict(ticker="A", fields=["dividend_growth", "returns"])])
        self.assertEqual(metadata["attempts"], 2)

    def test_missing_seed_does_not_fetch_or_modify_the_record(self):
        result = self.fixture([record("A", ["returns"])])
        original = deepcopy(result["portfolios"])
        metadata = repair_incomplete_stocks(
            result, seeds={}, previous={}, fetcher=lambda ticker: self.fail("no merge seed"),
            merge_stock=lambda *args: self.fail("no merge seed"),
            finalize_refresh_batch=finalize, started_at=START,
            clock=self.clock, sleeper=self.clock.sleep)
        self.assertEqual(metadata["attempts"], 0)
        self.assertEqual(metadata["unresolved"], [dict(ticker="A", fields=["returns"])])
        self.assertEqual(result["portfolios"], original)

    def test_expanded_options_cannot_bypass_hard_worker_or_budget_caps(self):
        result = self.fixture([record(str(i), ["returns"]) for i in range(9)])
        calls, lock = [], Lock()
        def fetch(ticker):
            with lock:
                calls.append(ticker)
                if len(calls) == 4:
                    self.clock.value += 200
            return record(ticker)
        metadata = self.run_repair(result, fetch, workers=100, budget_seconds=1_000_000, max_rounds=100)
        self.assertEqual(len(calls), 4)
        self.assertEqual(metadata["attempts"], 4)
        self.assertEqual(metadata["rounds"], 1)
        self.assertEqual(len(metadata["unresolved"]), 5)

    def test_recovered_ticker_requires_all_of_its_groups_complete(self):
        result = self.fixture([record("A", ["returns"])])
        result["portfolios"].append(dict(id="classic", stocks=[record("A", ["dividend_growth"])]))
        finalize(result, START)
        def merge(seed, prior, payload, group, stamp):
            return record("A", ["dividend_growth"] if group == "classic" else [])
        metadata = self.run_repair(result, lambda ticker: {}, merge)
        self.assertEqual(result["portfolios"][0]["stocks"][0]["refresh"]["status"], "live")
        self.assertEqual(metadata["recovered_tickers"], [])
        self.assertEqual(metadata["unresolved"], [dict(ticker="A", fields=["dividend_growth"])])

    def test_invalid_limits_fail_before_fetching(self):
        for options in [dict(max_rounds=-1), dict(max_rounds=True), dict(workers=0),
                        dict(workers=True), dict(budget_seconds=-1), dict(budget_seconds=True),
                        dict(budget_seconds=float("inf")), dict(budget_seconds=float("nan"))]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.run_repair(self.fixture([record("A", ["returns"])]), lambda ticker: self.fail("invalid limits"), **options)


if __name__ == "__main__":
    unittest.main()
