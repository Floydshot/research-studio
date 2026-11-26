"""A rejected history must stay distinguishable from a missing history."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import update_data as m


class ReturnsRejectionTests(unittest.TestCase):
    def setUp(self):
        self.stamp = "2026-11-26T07:31:20+01:00"
        history = pd.DataFrame(
            {"Close": [100.0, 101.0, 102.0, 103.0], "Dividends": 0.0},
            index=pd.to_datetime(["2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25"]).tz_localize("Europe/Paris"),
        )
        info = {"currency": "EUR", "exchangeTimezoneName": "Europe/Paris"}
        self.prior = m.calculate_period_returns(history.iloc[:-1], "CS.PA", info, "2026-11-25T07:31:20+01:00")
        self.fresh = m.calculate_period_returns(history, "CS.PA", info, self.stamp)

    def test_rejected_candidate_preserves_prior_dates_and_values(self):
        candidate = deepcopy(self.fresh)
        candidate["periods"]["1d"]["end"] = "2026-11-27"
        previous = {"returns": deepcopy(self.prior)}
        before = deepcopy(previous)
        stock = {"ticker": "CS.PA"}
        m.apply_period_returns(stock, previous, {"returns": candidate}, self.stamp)
        result = stock["returns"]
        self.assertEqual(result["source_status"], "previous")
        self.assertIn("Prüfung", result["refresh_note"])
        for field in ("as_of", "retrieved_at", "periods", "available_history"):
            self.assertEqual(result[field], self.prior[field])
        self.assertEqual(previous, before)

    def test_valid_candidate_can_replace_prior_without_mutating_input(self):
        payload = {"returns": deepcopy(self.fresh)}
        before = deepcopy(payload)
        stock = {"ticker": "CS.PA"}
        m.apply_period_returns(stock, {"returns": self.prior}, payload, self.stamp)
        self.assertEqual(stock["returns"], self.fresh)
        self.assertEqual(payload, before)
        self.assertNotIn("refresh_note", stock["returns"])

    def test_rejected_first_response_never_creates_observed_returns(self):
        stock = {"ticker": "CS.PA"}
        m.apply_period_returns(stock, {}, {"returns": {}}, self.stamp)
        result = stock["returns"]
        self.assertEqual(result["source_status"], "unavailable")
        self.assertTrue(all(period["value"] is None for period in result["periods"].values()))
        self.assertTrue(all("Prüfung" in period["reason"] for period in result["periods"].values()))

    def test_provider_error_still_retains_its_specific_diagnosis(self):
        stock = {"ticker": "CS.PA"}
        m.apply_period_returns(stock, {"returns": self.prior}, {"returns_error": "Gesamtrendite-Historie: Timeout"}, self.stamp)
        self.assertEqual(stock["returns"]["refresh_note"], "Gesamtrendite-Historie: Timeout")
        self.assertEqual(stock["returns"]["retrieved_at"], self.prior["retrieved_at"])


if __name__ == "__main__":
    unittest.main()
