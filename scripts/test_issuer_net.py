"""Financial-context and fallback tests for Cloudflare primary earnings."""
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from issuer_net import (FACTS_URL, SUBMISSIONS_URL, _OfficialRedirectHandler, _allowed_url,
                        fetch_net_earnings, parse_net_quarter_release, parse_sec_net_earnings)

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc)
ACC = "0001477333-26-000061"


def fixtures():
    def fact(start, end, value, accession=ACC, filed="2026-11-05", form="10-Q"):
        return dict(start=start, end=end, val=value, accn=accession, filed=filed, form=form)
    values = [fact("2026-01-01", "2026-09-30", -226973000),
              fact("2025-01-01", "2025-09-30", -90190000),
              fact("2025-01-01", "2025-12-31", -102267000, "0001477333-26-000016", "2026-02-26", "10-K"),
              # The single quarter must not replace the YTD context.
              fact("2026-07-01", "2026-09-30", -34065000)]
    facts = dict(cik=1477333, entityName="Cloudflare, Inc.", facts={"us-gaap": {"NetIncomeLoss": {"units": {"USD": values}}}})
    submissions = dict(cik="0001477333", filings={"recent": dict(form=["10-Q"], reportDate=["2026-09-30"],
                       filingDate=["2026-11-05"], accessionNumber=[ACC], primaryDocument=["cloud-20260930.htm"])})
    return facts, submissions


def release(year, quarter, amount="34.1", kind="loss", published="November 5, 2026"):
    words = {1: "First", 2: "Second", 3: "Third", 4: "Fourth"}
    months = {1: "March 31", 2: "June 30", 3: "September 30", 4: "December 31"}
    quarter_title = f"Fourth Quarter and Fiscal Year {year}" if quarter == 4 else f"{words[quarter]} Quarter {year}"
    quarter_period = "fourth quarter and fiscal year" if quarter == 4 else f"{words[quarter].lower()} quarter"
    return f"""<html><head><script>GAAP net income was $999 million</script></head><body>
    <p>Press release. {published}</p><h1>Cloudflare Announces {quarter_title} Financial Results</h1>
    <p>Cloudflare announced financial results for its {quarter_period} ended {months[quarter]}, {year}.</p>
    <p>Net Income (Loss): GAAP net {kind} was ${amount} million, compared to $1.3 million.
    Non-GAAP net income was $143.7 million.</p></body></html>"""


class SecTests(unittest.TestCase):
    def test_exact_ttm_and_provenance(self):
        facts, submissions = fixtures()
        result = parse_sec_net_earnings(facts, submissions, NOW)
        self.assertEqual(result["ttm_net_income_usd"], -239050000)
        self.assertEqual((result["period_start"], result["period_end"]), ("2025-10-01", "2026-09-30"))
        self.assertEqual(result["report_date"], "2026-11-05")
        self.assertFalse(result["approximate"])
        self.assertIn("cloud-20260930.htm", result["source_url"])
        self.assertEqual(len(result["observations"]), 3)

    def test_positive_earnings_are_not_forced_negative(self):
        facts, submissions = fixtures()
        facts["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"][0]["val"] = 400000000
        self.assertEqual(parse_sec_net_earnings(facts, submissions, NOW)["ttm_net_income_usd"], 387923000)

    def test_wrong_issuer(self):
        facts, submissions = fixtures()
        facts["cik"] = 1
        with self.assertRaises(ValueError): parse_sec_net_earnings(facts, submissions, NOW)

    def test_invalid_response_schema(self):
        facts, submissions = fixtures()
        for bad in (None, [], "not a fact mapping"):
            with self.assertRaises(ValueError): parse_sec_net_earnings(bad, submissions, NOW)
            with self.assertRaises(ValueError): parse_sec_net_earnings(facts, bad, NOW)

    def test_non_gaap_only_rejected(self):
        facts, submissions = fixtures()
        facts["facts"]["custom-non-gaap"] = facts["facts"].pop("us-gaap")
        with self.assertRaises(ValueError): parse_sec_net_earnings(facts, submissions, NOW)

    def test_wrong_currency(self):
        facts, submissions = fixtures()
        units = facts["facts"]["us-gaap"]["NetIncomeLoss"]["units"]
        units["EUR"] = units.pop("USD")
        with self.assertRaises(ValueError): parse_sec_net_earnings(facts, submissions, NOW)

    def test_missing_comparable_ytd(self):
        facts, submissions = fixtures()
        facts["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"].pop(1)
        with self.assertRaises(ValueError): parse_sec_net_earnings(facts, submissions, NOW)

    def test_latest_filing_not_facts_lag(self):
        facts, submissions = fixtures()
        submissions["filings"]["recent"]["reportDate"] = ["2026-12-31"]
        submissions["filings"]["recent"]["filingDate"] = ["2027-02-10"]
        with self.assertRaises(ValueError): parse_sec_net_earnings(facts, submissions, datetime(2027, 2, 11, tzinfo=timezone.utc))

    def test_future_or_stale_rejected(self):
        facts, submissions = fixtures()
        with self.assertRaises(ValueError): parse_sec_net_earnings(facts, submissions, datetime(2027, 4, 1, tzinfo=timezone.utc))
        with self.assertRaises(ValueError): parse_sec_net_earnings(facts, submissions, datetime(2026, 11, 4, tzinfo=timezone.utc))

    def test_conflicting_latest_facts_rejected(self):
        facts, submissions = fixtures()
        values = facts["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"]
        values.append({**values[0], "val": 999})
        with self.assertRaises(ValueError): parse_sec_net_earnings(facts, submissions, NOW)

    def test_bool_and_nan_are_not_earnings(self):
        for value in (True, float("nan"), float("inf")):
            facts, submissions = fixtures()
            facts["facts"]["us-gaap"]["NetIncomeLoss"]["units"]["USD"][0]["val"] = value
            with self.assertRaises(ValueError): parse_sec_net_earnings(facts, submissions, NOW)

    def test_full_year_is_direct_ttm(self):
        facts, submissions = fixtures()
        recent = submissions["filings"]["recent"]
        recent.update(form=["10-K"], reportDate=["2025-12-31"], filingDate=["2026-02-26"],
                      accessionNumber=["0001477333-26-000016"], primaryDocument=["cloud-20251231.htm"])
        result = parse_sec_net_earnings(facts, submissions, datetime(2026, 2, 27, tzinfo=timezone.utc))
        self.assertEqual(result["ttm_net_income_usd"], -102267000)
        self.assertEqual(result["period_start"], "2025-01-01")


class ReleaseTests(unittest.TestCase):
    def test_primary_quarter_not_non_gaap_or_script(self):
        result = parse_net_quarter_release(release(2026, 3), 2026, 3, NOW)
        self.assertEqual(result["net_income_usd"], -34100000)
        self.assertEqual(result["rounding_bound_usd"], 50000)

    def test_non_breaking_date_whitespace(self):
        html = release(2026, 2, "170.0", published="August 6, 2026").replace("June 30", "June\xa030")
        self.assertEqual(parse_net_quarter_release(html, 2026, 2, NOW)["net_income_usd"], -170000000)

    def test_wrong_title_period_and_future_date_rejected(self):
        for html in (release(2026, 3).replace("Third Quarter 2026", "Second Quarter 2026"),
                     release(2026, 3).replace("September 30, 2026", "June 30, 2026"),
                     release(2026, 3, published="November 25, 2026")):
            with self.assertRaises(ValueError): parse_net_quarter_release(html, 2026, 3, NOW)

    def test_non_gaap_only_rejected(self):
        html = release(2026, 3).replace("GAAP net loss was $34.1 million, compared to $1.3 million.", "")
        with self.assertRaises(ValueError): parse_net_quarter_release(html, 2026, 3, NOW)

    def test_fallback_reconciles_four_dated_quarters(self):
        documents = {"third-quarter-2026": release(2026, 3),
                     "second-quarter-2026": release(2026, 2, "170.0", published="August 6, 2026"),
                     "first-quarter-2026": release(2026, 1, "22.9", published="May 7, 2026"),
                     "fourth-quarter-and-fiscal-year-2025": release(2025, 4, "12.1", published="February 10, 2026")}
        called = []
        def fetch(url):
            called.append(url)
            for key, html in documents.items():
                if key in url: return html.encode()
            raise OSError("SEC unavailable")
        result = fetch_net_earnings(NOW, fetch)
        self.assertEqual(result["ttm_net_income_usd"], -239100000)
        self.assertEqual(result["rounding_bound_usd"], 200000)
        self.assertTrue(result["approximate"])
        self.assertEqual(result["period_start"], "2025-10-01")
        self.assertEqual(len(result["observations"]), 4)
        self.assertLessEqual(len(called), 7)

    def test_rounding_ambiguity_rejected(self):
        def fetch(url):
            for quarter, word, published in [(3, "third", "November 5, 2026"), (2, "second", "August 6, 2026"), (1, "first", "May 7, 2026")]:
                if word + "-quarter-2026" in url:
                    return release(2026, quarter, "0.0", published=published).encode()
            if "fourth-quarter" in url: return release(2025, 4, "0.0", published="February 10, 2026").encode()
            raise OSError("Unavailable")
        with self.assertRaises(ValueError): fetch_net_earnings(NOW, fetch)

    def test_sec_preferred_and_request_count(self):
        import json
        facts, submissions = fixtures()
        called = []
        def fetch(url):
            called.append(url)
            return json.dumps(facts if url == FACTS_URL else submissions).encode()
        result = fetch_net_earnings(NOW, fetch)
        self.assertFalse(result["approximate"])
        self.assertEqual(called, [SUBMISSIONS_URL, FACTS_URL])

    def test_live_retrieval_timestamp_is_after_both_responses(self):
        import json
        facts, submissions = fixtures()
        clock = [NOW]
        completed = datetime(2026, 11, 23, 10, 1, tzinfo=timezone.utc)
        def fetch(url):
            if url == FACTS_URL:
                clock[0] = completed
                return json.dumps(facts).encode()
            return json.dumps(submissions).encode()
        with patch("issuer_net._clock", side_effect=lambda value: value if value is not None else clock[0]):
            result = fetch_net_earnings(fetcher=fetch)
        self.assertEqual(result["retrieved_at"], completed.isoformat(timespec="seconds"))
        self.assertEqual(result["verified_at"], result["retrieved_at"])

    def test_source_urls_reject_credentials_ports_fragments_and_foreign_hosts(self):
        _allowed_url(FACTS_URL)
        for url in ("https://user@data.sec.gov/file", "https://data.sec.gov:444/file",
                    "https://data.sec.gov:wrong/file", "https://data.sec.gov/file#part",
                    "https://www.cloudflare.com/file#", "https://data.sec.gov.evil.example/file",
                    "http://data.sec.gov/file", "https://[bad/file"):
            with self.assertRaises(ValueError): _allowed_url(url)

    def test_foreign_redirect_rejected_before_following(self):
        with self.assertRaises(ValueError):
            _OfficialRedirectHandler().redirect_request(None, None, 302, "Found", {}, "https://foreign.example/facts")

    def test_oversized_sources_fail_closed(self):
        with self.assertRaises(ValueError): fetch_net_earnings(NOW, lambda url: b"x" * 5000001)

    def test_naive_now_rejected(self):
        with self.assertRaises(ValueError): fetch_net_earnings(datetime(2026, 11, 23), lambda url: b"")


if __name__ == "__main__":
    unittest.main()
