"""Prove that the approved DBS close exception cannot widen into a fallback."""
from pathlib import Path
from copy import deepcopy
import sys
import types
import unittest
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import history_corrections as c
import update_data as m


STAMP = "2026-11-27T12:00:00+01:00"


def history(*, maximum=False):
    dates = ["2026-11-19", "2026-11-20", "2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26"]
    closes = [67.339996, 66.110001, float("nan"), 66.239998, 65.440002, 65.849998]
    if maximum:
        dates = ["2005-12-30", "2005-12-31", "2006-01-03"] + dates
        closes = [12.0, float("nan"), 12.1] + closes
    frame = pd.DataFrame({field: closes for field in ("Open", "High", "Low", "Close", "Adj Close")},
                         index=pd.to_datetime(dates).tz_localize("Asia/Singapore"))
    frame["Volume"] = 1000
    frame["Dividends"] = 0.0
    frame["Stock Splits"] = 0.0
    frame["Capital Gains"] = 0.0
    frame.loc[frame["Close"].isna(), "Volume"] = 0
    frame.attrs["provider"] = "Yahoo Finance"
    return frame


def apply(frame, **overrides):
    arguments = dict(ticker="D05.SI", currency="SGD", isin="SG1L01001701",
                     auto_adjust=False, retrieved_at=STAMP)
    arguments.update(overrides)
    return c.apply_history_corrections(frame, **arguments)


class HistoryCorrectionTests(unittest.TestCase):
    def assert_unchanged(self, frame, **overrides):
        original = frame.copy(deep=True)
        result, metadata = apply(frame, **overrides)
        self.assertIsNot(result, frame)
        pd.testing.assert_frame_equal(frame, original)
        pd.testing.assert_frame_equal(result, original)
        self.assertEqual(metadata, [])

    def test_only_the_missing_close_changes_on_a_copy(self):
        frame = history(maximum=True)
        original = frame.copy(deep=True)
        result, metadata = apply(frame)
        pd.testing.assert_frame_equal(frame, original)
        pd.testing.assert_frame_equal(result.drop(columns="Close"), original.drop(columns="Close"))
        other_dates = result.index.date != pd.Timestamp(c.DBS_CLOSE_DATE).date()
        pd.testing.assert_series_equal(result.loc[other_dates, "Close"], original.loc[other_dates, "Close"])
        self.assertEqual(result.loc[c.DBS_CLOSE_DATE, "Close"], 65.76)
        self.assertEqual(len(result), len(original))
        self.assertTrue(pd.isna(result.loc["2005-12-31", "Close"]))
        self.assertEqual(len(metadata), 1)
        self.assertEqual(metadata[0]["source_url"], c.DBS_HISTORY_SOURCE)
        self.assertEqual(metadata[0]["evidence_retrieved_on"], "2026-11-27")
        self.assertEqual(metadata[0]["source_as_of"], "2026-11-23")
        self.assertEqual(metadata[0]["applied_at"], STAMP)
        self.assertEqual(len(metadata[0]["overlap_checks"]), 5)
        self.assertIsNone(metadata[0]["original"])

    def test_identity_and_raw_price_basis_are_required(self):
        for overrides in (
            dict(ticker="DBSDY"), dict(ticker="D05.SI "), dict(currency="USD"),
            dict(currency=None), dict(isin=None), dict(isin="US23304Y1001"),
            dict(auto_adjust=True), dict(auto_adjust=None), dict(auto_adjust=0),
        ):
            with self.subTest(overrides=overrides):
                self.assert_unchanged(history(), **overrides)

    def test_no_valid_positive_yahoo_close_is_overwritten(self):
        for close in (65.76, 60.0, 0.01, float("inf"), "unrecognized"):
            with self.subTest(close=close):
                frame = history()
                if isinstance(close, str):
                    frame["Close"] = frame["Close"].astype(object)
                frame.loc[c.DBS_CLOSE_DATE, "Close"] = close
                self.assert_unchanged(frame)

    def test_zero_and_negative_price_can_be_replaced(self):
        for close in (0.0, -1.0):
            with self.subTest(close=close):
                frame = history()
                frame.loc[c.DBS_CLOSE_DATE, "Close"] = close
                result, metadata = apply(frame)
                self.assertEqual(result.loc[c.DBS_CLOSE_DATE, "Close"], 65.76)
                self.assertEqual(metadata[0]["original"], close)

    def test_every_one_of_five_overlap_prices_is_required(self):
        for session, _ in c.DBS_OVERLAP_CLOSES:
            for value in (float("nan"), 0.0, 10.0):
                with self.subTest(session=session, value=value):
                    frame = history()
                    frame.loc[session, "Close"] = value
                    self.assert_unchanged(frame)
            with self.subTest(missing_session=session):
                self.assert_unchanged(history().drop(pd.Timestamp(session, tz="Asia/Singapore")))

    def test_quote_rounding_tolerance_does_not_allow_a_changed_share_basis(self):
        frame = history()
        frame.loc["2026-11-19", "Close"] = 67.3449
        result, metadata = apply(frame)
        self.assertEqual(len(metadata), 1)
        frame.loc["2026-11-19", "Close"] = 67.3451
        self.assert_unchanged(frame)
        self.assert_unchanged(history().assign(Close=history()["Close"] / 2))

    def test_does_not_add_missing_target_or_repair_another_day(self):
        frame = history()
        frame.loc["2026-11-20", "Close"] = float("nan")
        self.assert_unchanged(frame)
        self.assert_unchanged(history().drop(pd.Timestamp(c.DBS_CLOSE_DATE, tz="Asia/Singapore")))

    def test_timezone_aware_utc_and_naive_session_dates_are_supported(self):
        for zone in ("UTC", "America/New_York", None):
            with self.subTest(zone=zone):
                frame = history()
                frame.index = frame.index.tz_localize(None) if zone is None else frame.index.tz_convert(zone)
                original_index = frame.index.copy()
                result, metadata = apply(frame)
                self.assertEqual(len(metadata), 1)
                pd.testing.assert_index_equal(result.index, original_index)
                target = 2
                self.assertEqual(result.iloc[target]["Close"], 65.76)

    def test_duplicate_or_invalid_sessions_are_not_silently_selected(self):
        frame = history()
        self.assert_unchanged(pd.concat([frame, frame.loc[[pd.Timestamp(c.DBS_CLOSE_DATE, tz="Asia/Singapore")]]]))
        different_time = frame.loc[[pd.Timestamp(c.DBS_CLOSE_DATE, tz="Asia/Singapore")]].copy()
        different_time.index += pd.Timedelta(hours=1)
        self.assert_unchanged(pd.concat([frame, different_time]))
        invalid = frame.copy()
        invalid.index = pd.DatetimeIndex([pd.NaT, *list(frame.index[1:])])
        self.assert_unchanged(invalid)

    def test_cannot_apply_before_the_evidence_or_without_an_aware_timestamp(self):
        for stamp in ("2026-11-26T12:00:00+08:00", "2026-11-27T12:00:00", "invalid", None):
            with self.subTest(stamp=stamp):
                self.assert_unchanged(history(), retrieved_at=stamp)

    def test_future_application_keeps_original_source_evidence_date(self):
        _, metadata = apply(history(), retrieved_at="2027-01-02T08:00:00+01:00")
        self.assertEqual(metadata[0]["evidence_retrieved_on"], "2026-11-27")
        self.assertEqual(metadata[0]["date"], "2026-11-23")

    def test_cash_fields_are_never_repaired_or_zeroed(self):
        frame = history()
        frame.loc[c.DBS_CLOSE_DATE, ["Dividends", "Stock Splits", "Capital Gains"]] = [1.0, 2.0, 0.3]
        result, metadata = apply(frame)
        self.assertEqual(len(metadata), 1)
        pd.testing.assert_frame_equal(result[["Dividends", "Stock Splits", "Capital Gains"]],
                                      frame[["Dividends", "Stock Splits", "Capital Gains"]])

    def test_actual_returns_recover_recent_window_but_preserve_older_gap(self):
        frame = history(maximum=True)
        info = dict(currency="SGD", exchangeTimezoneName="Asia/Singapore")
        before = m.calculate_period_returns(frame, "D05.SI", info, STAMP)
        self.assertIsNone(before["periods"]["1w"]["value"])
        self.assertEqual(before["available_history"]["start"], "2026-11-24")
        corrected, _ = apply(frame)
        after = m.calculate_period_returns(corrected, "D05.SI", info, STAMP)
        self.assertIsNotNone(m.valid_period_returns(after, "D05.SI", STAMP))
        self.assertAlmostEqual(after["periods"]["1w"]["value"], (65.849998 / 67.339996 - 1) * 100, places=5)
        self.assertEqual(after["available_history"]["start"], "2006-01-03")
        self.assertEqual([note["date"] for note in after["quality_notes"]], ["2005-12-31"])
        self.assertIsNone(after["periods"]["all"]["value"])

    def test_correction_is_idempotent(self):
        first, first_metadata = apply(history())
        self.assertEqual(len(first_metadata), 1)
        second, second_metadata = apply(first)
        pd.testing.assert_frame_equal(second, first)
        self.assertEqual(second_metadata, [])

    def test_worker_uses_the_same_corrected_close_for_technicals_and_returns(self):
        frame = history()
        original = frame.copy(deep=True)
        calls, messages = [], []

        class Security:
            info = dict(currency="SGD", symbol="D05.SI", currentPrice=65.85,
                        trailingPE=12, exchangeTimezoneName="Asia/Singapore")

            def __init__(self, ticker):
                pass

            def history(self, **kwargs):
                calls.append(kwargs)
                return frame.copy(deep=True)

            def get_history_metadata(self):
                return dict(self.info)

        class Pipe:
            def send(self, value):
                messages.append(value)

            def close(self):
                pass

        provider = types.ModuleType("yfinance")
        provider.Ticker = Security
        issuer = types.ModuleType("issuer_dbs")
        issuer.fetch_dbs_roe = lambda: {}
        with patch.dict(sys.modules, {"yfinance": provider, "issuer_dbs": issuer}), patch.object(m, "iso_now", return_value=STAMP):
            m._fetch_worker("D05.SI", Pipe(), 1)
        payload = {key: value for message in messages for key, value in message.items()}
        self.assertNotIn("returns_error", payload)
        self.assertTrue(all(call["keepna"] for call in calls))
        self.assertEqual(payload["technicals"]["price_corrections"][0]["value"], 65.76)
        self.assertEqual(payload["returns"]["price_corrections"][0]["date"], c.DBS_CLOSE_DATE)
        self.assertEqual(payload["returns"]["available_history"]["price_corrections"], payload["returns"]["price_corrections"])
        self.assertIn(c.DBS_HISTORY_SOURCE, payload["returns"]["method"])
        self.assertIsNotNone(payload["returns"]["periods"]["1w"]["value"])
        pd.testing.assert_frame_equal(frame, original)

    def test_fallback_technicals_preserve_correction_evidence_and_old_timestamp(self):
        seed = next(stock for group in m.build_seed_data()["portfolios"] for stock in group["stocks"] if stock["ticker"] == "D05.SI")
        previous = deepcopy(seed)
        previous.update(currency="SGD", sma200=60.0, rsi14=45.0)
        _, corrections = apply(history())
        previous["field_metadata"] = {}
        for field in ("sma200", "rsi14"):
            previous["field_sources"][field] = "live"
            previous["field_updated_at"][field] = "2026-11-26T08:00:00+01:00"
            previous["field_metadata"][field] = dict(price_corrections=deepcopy(corrections), retrieved_at=previous["field_updated_at"][field])
        result = m.merge_stock(seed, previous, {}, "value", STAMP)
        for field in ("sma200", "rsi14"):
            self.assertEqual(result[field], previous[field])
            self.assertEqual(result["field_sources"][field], "previous")
            self.assertEqual(result["field_updated_at"][field], previous["field_updated_at"][field])
            self.assertEqual(result["field_metadata"][field], previous["field_metadata"][field])


if __name__ == "__main__":
    unittest.main()
