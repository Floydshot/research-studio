"""Keep provider-reported missing closed bars visible to real return validation.

The fixture models the observed 27 November Yahoo response: the previous
European session has no closing price, while today's live bar may exist.
Only the network boundary is replaced; worker calculations and validators run.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import update_data as m


def history(*, recovered=False, intraday=True, holiday=False, zone="Europe/Paris"):
    dates = ["2026-11-23", "2026-11-24", "2026-11-25"]
    closes = [45.36, 45.32, 44.88]
    if not holiday:
        dates.append("2026-11-26")
        closes.append(45.72 if recovered else float("nan"))
    if intraday:
        dates.append("2026-11-27")
        closes.append(45.80)
    frame = pd.DataFrame({field: closes for field in ("Open", "High", "Low", "Close", "Adj Close")},
                         index=pd.to_datetime(dates).tz_localize(zone))
    frame["Volume"] = 1000
    frame["Dividends"] = 0.0
    frame["Stock Splits"] = 0.0
    missing = frame["Close"].isna()
    frame.loc[missing, "Volume"] = 0
    return frame


class CapturedPipe:
    def __init__(self):
        self.messages = []
        self.closed = False

    def send(self, value):
        self.messages.append(deepcopy(value))

    def close(self):
        self.closed = True

    def payload(self):
        value = {"warnings": []}
        for message in self.messages:
            value["warnings"].extend(message.pop("warnings", []))
            value.update(message)
        return value


class HistoryCompletenessTests(unittest.TestCase):
    def worker(self, frame, *, stamp="2026-11-27T10:00:00+01:00", ticker="CS.PA"):
        zone = str(frame.index.tz)
        currency = "USD" if zone == "America/New_York" else "EUR"
        close_hour = "16:00:00-05:00" if currency == "USD" else "17:30:00+01:00"
        info = dict(symbol=ticker, currency=currency, exchangeTimezoneName=zone,
                    currentPrice=45.80, regularMarketPrice=45.80, trailingPE=15,
                    regularMarketTime=int(datetime.fromisoformat("2026-11-27T09:15:00+01:00").timestamp()),
                    currentTradingPeriod={"regular": {"end": int(datetime.fromisoformat("2026-11-27T" + close_hour).timestamp())}})
        calls = []
        original = frame.copy(deep=True)

        class Security:
            def __init__(self, symbol):
                self.symbol = symbol

            @property
            def info(self):
                return deepcopy(info)

            def get_history_metadata(self):
                return deepcopy(info)

            def history(self, **kwargs):
                calls.append(deepcopy(kwargs))
                result = frame.copy(deep=True)
                # yfinance's default can remove an all-NaN OHLC row, hiding a
                # closed-session hole once the following live bar appears.
                if not kwargs.get("keepna", False):
                    price_columns = ["Open", "High", "Low", "Close", "Adj Close"]
                    result = result.loc[~result[price_columns].isna().all(axis=1)]
                return result

        provider = types.ModuleType("yfinance")
        provider.Ticker = Security
        pipe = CapturedPipe()
        with patch.dict(sys.modules, {"yfinance": provider}), patch.object(m, "iso_now", return_value=stamp):
            m._fetch_worker(ticker, pipe, 1)
        self.assertTrue(pipe.closed)
        pd.testing.assert_frame_equal(frame, original)
        return pipe.payload(), calls

    def test_before_open_missing_closed_price_is_retained_and_rejected(self):
        stamp = "2026-11-27T08:35:00+01:00"
        result, calls = self.worker(history(intraday=False), stamp=stamp)
        maximum = [call for call in calls if call.get("period") == "max"]
        self.assertEqual(len(maximum), 1)
        self.assertIs(maximum[0].get("keepna"), True)
        self.assertFalse(maximum[0]["auto_adjust"])
        self.assertTrue(maximum[0]["actions"])
        self.assertEqual(result["returns"]["quality_notes"][0]["date"], "2026-11-26")
        self.assertIsNone(m.valid_period_returns(result["returns"], "CS.PA", stamp))

    def test_after_open_live_bar_cannot_hide_the_missing_closed_session(self):
        stamp = "2026-11-27T10:00:00+01:00"
        result, _ = self.worker(history(), stamp=stamp)
        candidate = result["returns"]
        self.assertEqual(candidate["as_of"], "2026-11-25")
        self.assertIn("2026-11-26", [note["date"] for note in candidate["quality_notes"]])
        self.assertIsNone(m.valid_period_returns(candidate, "CS.PA", stamp))

    def test_default_provider_dropping_would_otherwise_appear_valid(self):
        stamp = "2026-11-27T10:00:00+01:00"
        frame = history()
        dropped = frame.loc[frame["Close"].notna()]
        info = {"currency": "EUR", "exchangeTimezoneName": "Europe/Paris"}
        old_candidate = m.calculate_period_returns(dropped, "CS.PA", info, stamp)
        self.assertIsNotNone(m.valid_period_returns(old_candidate, "CS.PA", stamp))
        result, _ = self.worker(frame, stamp=stamp)
        self.assertIsNone(m.valid_period_returns(result["returns"], "CS.PA", stamp))

    def test_recovered_closed_bar_is_accepted_and_today_is_excluded(self):
        stamp = "2026-11-27T10:00:00+01:00"
        result, _ = self.worker(history(recovered=True), stamp=stamp)
        candidate = m.valid_period_returns(result["returns"], "CS.PA", stamp)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["as_of"], "2026-11-26")
        self.assertNotIn("quality_notes", candidate)
        self.assertAlmostEqual(candidate["periods"]["1d"]["value"], (45.72 / 44.88 - 1) * 100, places=5)

    def test_us_holiday_without_a_reported_bad_bar_is_not_a_missing_price(self):
        stamp = "2026-11-27T10:00:00+01:00"
        result, _ = self.worker(history(holiday=True, zone="America/New_York"), stamp=stamp, ticker="MSFT")
        candidate = m.valid_period_returns(result["returns"], "MSFT", stamp)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["as_of"], "2026-11-25")
        self.assertNotIn("quality_notes", candidate)

    def test_reserve_price_and_total_returns_both_reject_the_hole(self):
        stamp = "2026-11-27T10:00:00+01:00"
        result, _ = self.worker(history(), stamp=stamp, ticker="TTE.PA")
        self.assertIsNone(m.valid_period_returns(result["returns"], "TTE.PA", stamp))
        self.assertIsNone(m.valid_price_returns(result["price_returns"], "TTE.PA", stamp))
        self.assertEqual(result["price_returns"]["quality_notes"][0]["date"], "2026-11-26")

    def test_recovered_reserve_keeps_distinct_observed_return_bases(self):
        stamp = "2026-11-27T10:00:00+01:00"
        result, _ = self.worker(history(recovered=True), stamp=stamp, ticker="TTE.PA")
        total = m.valid_period_returns(result["returns"], "TTE.PA", stamp)
        price = m.valid_price_returns(result["price_returns"], "TTE.PA", stamp)
        self.assertIsNotNone(total)
        self.assertIsNotNone(price)
        self.assertEqual(total["as_of"], "2026-11-26")
        self.assertEqual(price["as_of"], "2026-11-26")
        self.assertEqual(total["basis"], "total_return")
        self.assertEqual(price["basis"], "price_return")

    def test_rejected_worker_response_does_not_redate_a_valid_prior(self):
        stamp = "2026-11-27T10:00:00+01:00"
        prior_stamp = "2026-11-26T08:00:00+01:00"
        frame = history().iloc[:3]
        prior = m.calculate_period_returns(frame, "CS.PA", {"currency": "EUR", "exchangeTimezoneName": "Europe/Paris"}, prior_stamp)
        result, _ = self.worker(history(), stamp=stamp)
        stock = {"ticker": "CS.PA"}
        m.apply_period_returns(stock, {"returns": prior}, result, stamp)
        self.assertEqual(stock["returns"]["source_status"], "previous")
        for field in ("retrieved_at", "as_of", "periods"):
            self.assertEqual(stock["returns"][field], prior[field])


if __name__ == "__main__":
    unittest.main()
