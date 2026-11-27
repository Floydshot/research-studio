"""Recent invalid prices must not look complete merely because 1D is usable."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import update_data as m


START = "2026-11-27T09:39:00+01:00"
FETCH = "2026-11-27T09:40:00+01:00"
END = "2026-11-27T09:41:00+01:00"


def history(*, bad=False, short=False, holiday=False):
    dates = pd.bdate_range("2026-11-23" if short else "2026-11-02", "2026-11-25" if holiday else "2026-11-26", tz="Asia/Singapore")
    frame = pd.DataFrame({"Close": [100.0 + i for i in range(len(dates))], "Dividends": 0.0}, index=dates)
    if bad:
        frame.loc[pd.Timestamp("2026-11-23", tz="Asia/Singapore"), "Close"] = float("nan")
    return frame


def observation(frame, ticker="D05.SI", currency="SGD", zone="Asia/Singapore"):
    return m.calculate_period_returns(frame, ticker, {"currency": currency, "exchangeTimezoneName": zone}, FETCH)


def stock(returns, *, ticker="D05.SI", currency="SGD", price_returns=None):
    fields = dict(current_pe=15.0, forward_pe=12.0, price=100.0, sma200=95.0,
                  rsi14=50.0, currency=currency, ps_ratio=10.0, div_yield=4.0, roe=17.0)
    result = dict(fields, ticker=ticker, name=ticker, hist_pe=20.0, source_status="live",
                  as_of="2026-11-26T09:00:00+00:00", warnings=[], field_metadata={},
                  field_sources={field: "live" for field in fields},
                  field_updated_at={field: FETCH for field in fields}, returns=returns,
                  dividend_growth=dict(source_status="live", retrieved_at=FETCH))
    if price_returns is not None:
        result["price_returns"] = price_returns
    return result


def assess(current, group="value"):
    payload = dict(security_universe=dict(stocks=[current])) if group == "universe" else dict(portfolios=[dict(id=group, stocks=[current])])
    m.finalize_refresh_batch(payload, START, END)
    return current["refresh"]


class RecentHistoryGapTests(unittest.TestCase):
    def test_recent_price_hole_is_partial_despite_valid_daily_return(self):
        observed = observation(history(bad=True))
        self.assertIsNotNone(m.valid_period_returns(observed, "D05.SI", END))
        self.assertIsNotNone(observed["periods"]["1d"]["value"])
        self.assertIsNone(observed["periods"]["1w"]["value"])
        current = stock(observed)
        before = deepcopy(current)
        refresh = assess(current)
        self.assertEqual(refresh["status"], "partial")
        self.assertEqual(refresh["missing_fields"], ["returns"])
        self.assertEqual({key: value for key, value in current.items() if key != "refresh"}, before)

    def test_reserve_price_returns_uses_same_gap_gate(self):
        frame = history(bad=True)
        info = {"currency": "EUR", "exchangeTimezoneName": "Europe/Paris"}
        returns = m.calculate_period_returns(frame, "TTE.PA", info, FETCH)
        prices = m.calculate_reserve_price_returns(frame, "TTE.PA", info, FETCH)
        self.assertIsNotNone(m.valid_price_returns(prices, "TTE.PA", END))
        current = stock(returns, ticker="TTE.PA", currency="EUR", price_returns=prices)
        self.assertEqual(assess(current, "universe")["missing_fields"], ["returns", "price_returns"])

    def test_short_pre_ipo_history_is_not_a_price_error(self):
        observed = observation(history(short=True))
        self.assertIsNone(observed["periods"]["10y"]["value"])
        self.assertIn("vollständigen Zeitraum", observed["periods"]["10y"]["reason"])
        self.assertFalse(m.recent_return_price_gap(observed))
        self.assertEqual(assess(stock(observed))["status"], "live")

    def test_old_tokio_jump_stays_labelled_without_becoming_a_new_gap(self):
        old = pd.to_datetime(["2005-04-01", "2005-04-04"]).tz_localize("Asia/Tokyo")
        recent = pd.bdate_range("2026-11-16", "2026-11-26", tz="Asia/Tokyo")
        dates = old.append(recent)
        frame = pd.DataFrame({"Close": [100.0] + [300.0] * (len(dates) - 1), "Dividends": 0.0}, index=dates)
        observed = observation(frame, "8766.T", "JPY", "Asia/Tokyo")
        self.assertIsNotNone(m.valid_period_returns(observed, "8766.T", END))
        self.assertIsNone(observed["periods"]["all"]["value"])
        self.assertIn("Tokio", observed["periods"]["all"]["reason"])
        self.assertFalse(m.recent_return_price_gap(observed))
        self.assertEqual(assess(stock(observed, ticker="8766.T", currency="JPY"))["status"], "live")

    def test_verified_corrected_bar_restores_full_freshness(self):
        observed = observation(history())
        self.assertIsNotNone(observed["periods"]["1w"]["value"])
        self.assertFalse(m.recent_return_price_gap(observed))
        self.assertEqual(assess(stock(observed))["status"], "live")

    def test_closed_holiday_without_a_bad_bar_is_not_a_gap(self):
        frame = history(holiday=True).tz_convert("America/New_York")
        # Session dates are local exchange dates; construct them directly rather
        # than shifting Singapore midnight into the preceding US calendar day.
        frame.index = pd.bdate_range("2026-11-02", "2026-11-25", tz="America/New_York")
        observed = observation(frame, "MSFT", "USD", "America/New_York")
        self.assertEqual(observed["as_of"], "2026-11-25")
        self.assertNotIn("quality_notes", observed)
        self.assertEqual(assess(stock(observed, ticker="MSFT", currency="USD"), "tech")["status"], "live")


if __name__ == "__main__":
    unittest.main()
