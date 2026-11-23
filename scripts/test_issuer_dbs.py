"""Issuer ROE facts and layout guards; minimal synthetic tables, no copied report."""
from datetime import date, datetime, timezone
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import issuer_dbs as dbs


NOW = datetime(2026, 11, 23, 11, tzinfo=timezone.utc)
PDF_URL = "https://www.dbs.com/iwov-resources/images/investors/quarterly-financials/2026/3Q26_trading_update.pdf"
INDEX = ('<a href="' + PDF_URL + '">Trading update</a>').encode()


def columns(prefix, values, ends):
    output = prefix
    for value, end in zip(values, ends):
        output += str(value).rjust(end - len(output))
    return output


def fixture(quarter=3, year=2026):
    ends = [68, 82, 107] if quarter in (1, 2) else [68, 82, 107, 131, 145]
    years = [year, year - 1, year - 1] if quarter in (1, 2) else [year, year - 1, year, year, year - 1]
    period_end, report_date, values = {
        1: (f"31 March {year}", f"5 May {year}", [17.0, 17.3, 13.5]),
        2: (f"30 June {year}", f"6 August {year}", [17.5, 17.0, 15.3]),
        3: (f"30 September {year}", f"5 November {year}", [16.0, 17.1, 17.9, 17.0, 17.0]),
        4: (f"31 December {year}", f"9 February {year + 1}", [15.3, 17.2, 17.0, 16.2, 18.0]),
    }[quarter]
    cover = ("DBS Group Holdings Ltd\nFinancial Results for the "
             + ("Year" if quarter == 4 else "Quarter") + " Ended " + period_end
             + "\nBy order of the Board\nGroup Secretary\n" + report_date)
    table = "\n".join([
        columns("", dbs.TABLE_PERIODS[quarter], ends),
        columns("", years, ends),
        "Selected income statement items ($m)",
        "Key financial ratios (%)1",
        columns("Return on equity2,3", values, ends),
        columns("Return on tangible equity2,3,4", [x + 1.7 for x in values], ends),
    ])
    notes = ("Return on assets, return on equity, return on tangible equity and per share data "
             "are computed on an annualised basis.\n"
             "Calculated based on net profit attributable to the shareholders net of dividends on other equity instruments.\n"
             "Non-controlling interests and other equity instruments are not included as equity in the computation.")
    return [cover, table, notes]


class FakePage:
    def __init__(self, text):
        self.text = text

    def extract_text(self, *, extraction_mode):
        assert extraction_mode == "layout"
        return self.text


def fake_reader(pages=None):
    return SimpleNamespace(is_encrypted=False, pages=[FakePage(text) for text in (pages or fixture())])


class DBSIssuerTests(unittest.TestCase):
    def parse(self, pages=None, **kwargs):
        return dbs._parse_pages(pages or fixture(), year=kwargs.pop("year", 2026),
                                quarter=kwargs.pop("quarter", 3), today=kwargs.pop("today", NOW.date()), **kwargs)

    def test_latest_cumulative_roe_not_quarter_or_tangible(self):
        result = self.parse()
        self.assertEqual(result["value"], 17.0)
        self.assertEqual(result["label"], "ROE · 9M 2026")
        self.assertEqual(result["period_end"], "2026-09-30")
        self.assertEqual(result["report_date"], "2026-11-05")

    def test_q1_half_year_and_full_year_cumulative_columns(self):
        for quarter, year, today, expected, label in [
            (1, 2026, date(2026, 5, 6), 17.0, "1Q 2026"),
            (2, 2026, date(2026, 8, 7), 17.5, "1H 2026"),
            (4, 2025, date(2026, 2, 10), 16.2, "FY 2025"),
        ]:
            with self.subTest(quarter=quarter):
                result = self.parse(fixture(quarter, year), quarter=quarter, year=year, today=today)
                self.assertEqual(result["value"], expected)
                self.assertIn(label, result["period"])

    def test_rejects_quarter_only_table_for_nine_month_report(self):
        pages = fixture()
        pages[1] = fixture(1)[1]
        with self.assertRaises(dbs.DBSValidationError):
            self.parse(pages)

    def test_tangible_equity_alone_is_not_roe(self):
        pages = fixture()
        pages[1] = "\n".join(line for line in pages[1].splitlines() if not line.startswith("Return on equity"))
        with self.assertRaises(dbs.DBSValidationError):
            self.parse(pages)

    def test_rejects_year_header_reordered(self):
        pages = fixture()
        lines = pages[1].splitlines()
        lines[1] = columns("", [2026, 2025, 2026, 2025, 2026], [68, 82, 107, 131, 145])
        pages[1] = "\n".join(lines)
        with self.assertRaisesRegex(dbs.DBSValidationError, "years_changed"):
            self.parse(pages)

    def test_rejects_shifted_value_columns(self):
        pages = fixture()
        pages[1] = pages[1].replace("Return on equity2,3", "    Return on equity2,3")
        with self.assertRaisesRegex(dbs.DBSValidationError, "column_alignment"):
            self.parse(pages)

    def test_rejects_ambiguous_duplicate_roe_rows(self):
        pages = fixture()
        row = next(line for line in pages[1].splitlines() if line.startswith("Return on equity"))
        pages[1] += "\n" + row
        with self.assertRaisesRegex(dbs.DBSValidationError, "ambiguous"):
            self.parse(pages)

    def test_rejects_missing_or_extra_numeric_columns(self):
        for replacement in ["", "17.0 19.0"]:
            pages = fixture()
            lines = pages[1].splitlines()
            lines[4] = lines[4][:-4] + replacement
            pages[1] = "\n".join(lines)
            with self.subTest(replacement=replacement), self.assertRaises(dbs.DBSValidationError):
                self.parse(pages)

    def test_annualisation_and_shareholder_basis_required(self):
        for text in ["annualised", "Non-controlling interests", "net of dividends on other equity instruments"]:
            pages = fixture()
            pages[2] = pages[2].replace(text, "not confirmed")
            with self.subTest(text=text), self.assertRaises(dbs.DBSValidationError):
                self.parse(pages)

    def test_future_publication_rejected(self):
        with self.assertRaisesRegex(dbs.DBSValidationError, "future"):
            self.parse(today=date(2026, 11, 4))

    def test_wrong_period_or_date_rejected(self):
        pages = fixture()
        pages[0] = pages[0].replace("30 September", "30 June")
        with self.assertRaisesRegex(dbs.DBSValidationError, "period_mismatch"):
            self.parse(pages)

    def test_report_age_cannot_be_hidden_by_fresh_fetch_timestamp(self):
        with self.assertRaisesRegex(dbs.DBSValidationError, "stale"):
            self.parse(today=date(2027, 5, 10))

    def test_latest_link_is_selected_not_first(self):
        old = PDF_URL.replace("3Q26", "1Q26")
        html = ('<a href="' + old + '">old</a><a href="' + PDF_URL + '?pid=tracking">new</a>').encode()
        self.assertEqual(dbs._report_url(html), (PDF_URL, 2026, 3))

    def test_external_or_unsafe_urls_are_never_fetched(self):
        for url in [PDF_URL.replace("www.dbs.com", "evil.example"),
                    PDF_URL.replace("https:", "http:"), PDF_URL.replace("www.dbs.com", "www.dbs.com.evil.example"),
                    PDF_URL.replace("www.dbs.com", "user:pass@www.dbs.com"),
                    "file:///etc/passwd", PDF_URL.replace(".pdf", ".pdf/../../elsewhere")]:
            with self.subTest(url=url), self.assertRaises(dbs.DBSValidationError):
                dbs._official_url(url)
            with self.subTest(index=url), self.assertRaises(dbs.DBSValidationError):
                dbs._report_url(('<a href="' + url + '">bad</a>').encode())

    def test_cross_host_redirect_rejected_before_following(self):
        with self.assertRaises(dbs.DBSValidationError):
            dbs._OfficialRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example/report.pdf")

    def test_response_size_is_bounded(self):
        with self.assertRaisesRegex(dbs.DBSValidationError, "size"):
            dbs._read(dbs.INDEX_URL, 3, fetch=lambda *a, **k: b"1234")

    def test_full_fetch_schema_and_two_requests_only(self):
        calls = []
        def fetch(url, **limits):
            calls.append((url, limits))
            return INDEX if url == dbs.INDEX_URL else b"%PDF-fixture"
        with patch("pypdf.PdfReader", return_value=fake_reader()):
            result = dbs.fetch_dbs_roe(NOW, fetch=fetch)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["value"], 17.0)
        self.assertEqual(result["fact_type"], "issuer_reported_roe")
        self.assertEqual(result["validation_version"], 1)
        self.assertEqual(result["accounting_basis"], "IFRS")
        self.assertEqual(result["source_urls"], [PDF_URL, dbs.INDEX_URL])
        self.assertEqual(result["retrieved_at"], NOW.isoformat(timespec="seconds"))
        self.assertTrue(result["annualized"])
        self.assertIn("Kein TTM", result["method"])
        self.assertEqual(calls[0][1]["max_bytes"], dbs.MAX_INDEX_BYTES)

    def test_latest_future_report_does_not_silently_use_older_report(self):
        latest = PDF_URL.replace("3Q26_trading_update", "4Q26_performance_summary")
        index = INDEX + ('<a href="' + latest + '">future</a>').encode()
        calls = []
        def fetch(url, **kwargs):
            calls.append(url)
            return index if url == dbs.INDEX_URL else b"%PDF-fixture"
        with patch("pypdf.PdfReader", return_value=fake_reader(fixture(4))):
            with self.assertRaises(dbs.DBSValidationError):
                dbs.fetch_dbs_roe(NOW, fetch=fetch)
        self.assertEqual(calls, [dbs.INDEX_URL, latest])

    def test_invalid_pdf_and_naive_now_rejected(self):
        with self.assertRaisesRegex(dbs.DBSValidationError, "signature"):
            dbs.fetch_dbs_roe(NOW, fetch=lambda url, **k: INDEX if url == dbs.INDEX_URL else b"bad")
        with self.assertRaisesRegex(dbs.DBSValidationError, "timezone"):
            dbs.fetch_dbs_roe(datetime(2026, 11, 23))

    def test_pdf_page_limit_and_source_failure(self):
        reader = fake_reader()
        reader.pages *= 28
        with patch("pypdf.PdfReader", return_value=reader):
            with self.assertRaisesRegex(dbs.DBSValidationError, "page_limit"):
                dbs.fetch_dbs_roe(NOW, fetch=lambda url, **k: INDEX if url == dbs.INDEX_URL else b"%PDF-fixture")
        def broken(*args, **kwargs):
            raise OSError("offline")
        with self.assertRaisesRegex(dbs.DBSValidationError, "unavailable"):
            dbs.fetch_dbs_roe(NOW, fetch=broken)


if __name__ == "__main__":
    unittest.main()
