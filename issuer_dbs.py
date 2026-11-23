"""Bounded, fail-closed DBS issuer ROE lookup; no dependency on dashboard state.

The latest cumulative disclosed period is deliberately not called TTM.  ``fetch``
may be injected as ``fetch(url, *, max_bytes, timeout) -> bytes`` for offline tests;
``opener`` may instead be a urllib-compatible opener or open callable.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from io import BytesIO
import math
import re
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


INDEX_URL = "https://www.dbs.com/investors/financials/quarterly-financials"
TIMEOUT_SECONDS = 12
MAX_INDEX_BYTES = 2_000_000
MAX_PDF_BYTES = 4_000_000
MAX_REPORT_AGE_DAYS = 185
MAX_PAGES = 80
READ_PAGES = 10
PDF_PATH = re.compile(r"^/iwov-resources/images/investors/quarterly-financials/"
                      r"(?P<year>20\d{2})/(?P<quarter>[1-4])Q(?P<short>\d{2})_"
                      r"(?P<kind>trading_update|performance_summary)\.pdf$", re.I)
MONTHS = {name.lower(): n for n, name in enumerate(
    ("January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"), 1)}
DATE_PATTERN = r"(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(20\d{2})"
TABLE_PERIODS = {
    1: ["1st Qtr", "1st Qtr", "4th Qtr"],
    2: ["1st Half", "1st Half", "2nd Half"],
    3: ["3rd Qtr", "3rd Qtr", "2nd Qtr", "9 Mths", "9 Mths"],
    4: ["2nd Half", "2nd Half", "1st Half", "Year", "Year"],
}
PERIOD_TOKEN = re.compile(r"\b(?:[1-4](?:st|nd|rd|th)\s+(?:Qtr|Half)|9\s+Mths|Year)\b")


class DBSValidationError(ValueError):
    """The issuer source could not be safely confirmed in its expected layout."""


def _official_url(url: str) -> str:
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname != "www.dbs.com"
                or parts.username or parts.password or parts.port not in (None, 443)
                or parts.fragment):
            raise DBSValidationError("dbs_untrusted_source_url")
    except (ValueError, TypeError) as error:
        raise DBSValidationError("dbs_untrusted_source_url") from error
    if parts.path != urlsplit(INDEX_URL).path and not PDF_PATH.fullmatch(parts.path):
        raise DBSValidationError("dbs_unexpected_source_path")
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


class _OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _official_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read(url: str, max_bytes: int, *, opener=None, fetch=None) -> bytes:
    url = _official_url(url)
    if fetch is not None:
        value = fetch(url, max_bytes=max_bytes, timeout=TIMEOUT_SECONDS)
    else:
        client = opener if opener is not None else build_opener(_OfficialRedirect())
        open_call = client.open if hasattr(client, "open") else client
        request = Request(url, headers={"User-Agent": "ResearchStudio/1.0", "Accept": "text/html, application/pdf"})
        with open_call(request, timeout=TIMEOUT_SECONDS) as response:
            if hasattr(response, "geturl"):
                _official_url(response.geturl())
            value = response.read(max_bytes + 1)
    if not isinstance(value, bytes) or not value or len(value) > max_bytes:
        raise DBSValidationError("dbs_source_size_or_type_invalid")
    return value


class _ReportLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.reports = {}
        self.count = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "a":
            return
        self.count += 1
        if self.count > 10_000:
            raise DBSValidationError("dbs_index_link_limit")
        href = dict(attrs).get("href", "")
        if not isinstance(href, str):
            return
        candidate = urljoin(INDEX_URL, href)
        try:
            clean = _official_url(candidate)
        except DBSValidationError:
            return
        match = PDF_PATH.fullmatch(urlsplit(clean).path)
        if not match:
            return
        year, quarter = int(match["year"]), int(match["quarter"])
        if int(match["short"]) != year % 100:
            raise DBSValidationError("dbs_report_year_mismatch")
        expected_kind = "trading_update" if quarter % 2 else "performance_summary"
        if match["kind"].lower() == expected_kind:
            self.reports[(year, quarter)] = clean


def _report_url(html: bytes) -> tuple[str, int, int]:
    try:
        parser = _ReportLinks()
        parser.feed(html.decode("utf-8", errors="strict"))
    except (UnicodeError, ValueError) as error:
        raise DBSValidationError("dbs_invalid_index") from error
    if not parser.reports:
        raise DBSValidationError("dbs_no_supported_issuer_report")
    year, quarter = max(parser.reports)
    return parser.reports[(year, quarter)], year, quarter


def _date_from_match(match) -> date:
    if not match:
        raise DBSValidationError("dbs_report_date_missing")
    try:
        return date(int(match[3]), MONTHS[match[2].lower()], int(match[1]))
    except (KeyError, ValueError) as error:
        raise DBSValidationError("dbs_report_date_invalid") from error


def _parse_pages(pages: list[str], *, year: int, quarter: int, today: date) -> dict[str, Any]:
    if not pages or not all(isinstance(page, str) for page in pages):
        raise DBSValidationError("dbs_empty_pdf_text")
    cover = re.sub(r"\s+", " ", pages[0])
    if not re.search(r"DBS(?:H|\s+Group|\s+GROUP)|www\.dbs\.com", cover):
        raise DBSValidationError("dbs_issuer_identity_missing")
    period_end = _date_from_match(re.search(r"(?:Quarter|Year)\s+Ended\s+" + DATE_PATTERN, cover, re.I))
    month = quarter * 3
    expected_end = date(year, month, monthrange(year, month)[1])
    if period_end != expected_end:
        raise DBSValidationError("dbs_report_period_mismatch")
    report_date = _date_from_match(re.search(r"Group\s+Secretary\s+" + DATE_PATTERN, cover, re.I))
    if not period_end <= report_date <= today or not 0 <= (report_date - period_end).days <= 100:
        raise DBSValidationError("dbs_future_or_invalid_report_date")
    if ((today - period_end).days > MAX_REPORT_AGE_DAYS
            or (today - report_date).days > MAX_REPORT_AGE_DAYS):
        raise DBSValidationError("dbs_stale_report")
    all_text = re.sub(r"\s+", " ", " ".join(pages))
    if not re.search(r"Return on assets, return on equity,.*?computed on an annualised basis", all_text, re.I):
        raise DBSValidationError("dbs_annualisation_not_confirmed")
    if (not re.search(r"net profit attributable to the shareholders net of dividends on other equity instruments", all_text, re.I)
            or not re.search(r"Non-controlling interests and other equity instruments are not included as equity", all_text, re.I)):
        raise DBSValidationError("dbs_ordinary_shareholder_basis_not_confirmed")

    expected_periods = TABLE_PERIODS[quarter]
    expected_years = [year, year - 1, year - 1] if quarter in (1, 2) else [year, year - 1, year, year, year - 1]
    selected_column = 0 if quarter in (1, 2) else 3
    confirmed = []
    for page in pages:
        lines = page.splitlines()
        # Tables contain one exact ROE row; prose and RoTE never qualify.
        roe_rows = [(i, re.match(r"^\s*Return on equity(?:\d+(?:,\s*\d+)*)?\s{2,}(.+)$", line, re.I))
                    for i, line in enumerate(lines)]
        roe_rows = [(i, match) for i, match in roe_rows if match]
        if not roe_rows:
            continue
        if len(roe_rows) != 1:
            raise DBSValidationError("dbs_ambiguous_roe_rows")
        row_index, roe_match = roe_rows[0]
        headers = []
        for i, line in enumerate(lines[:row_index]):
            matches = list(PERIOD_TOKEN.finditer(line))
            labels = [re.sub(r"\s+", " ", match.group()) for match in matches]
            if labels == expected_periods and i + 1 < len(lines):
                years = list(re.finditer(r"\b20\d{2}\b", lines[i + 1]))
                if [int(match.group()) for match in years] != expected_years:
                    raise DBSValidationError("dbs_financial_table_years_changed")
                if any(abs(label.end() - year_token.end()) > 5 for label, year_token in zip(matches, years)):
                    raise DBSValidationError("dbs_financial_table_header_alignment_changed")
                headers.append(years)
        if not headers:
            # A separate quarter-only table in a half/full-year PDF is expected.
            continue
        if len(headers) != 1 or not any("Key financial ratios" in line for line in lines[:row_index]):
            raise DBSValidationError("dbs_financial_table_structure_changed")
        values = list(re.finditer(r"[+-]?\d+(?:\.\d+)?", roe_match[1]))
        residue = re.sub(r"[+-]?\d+(?:\.\d+)?", "", roe_match[1])
        if residue.strip() or len(values) != len(expected_years):
            raise DBSValidationError("dbs_roe_columns_changed")
        absolute_ends = [roe_match.start(1) + value.end() for value in values]
        if any(abs(end - year_token.end()) > 3 for end, year_token in zip(absolute_ends, headers[0])):
            raise DBSValidationError("dbs_roe_column_alignment_changed")
        row_values = [float(match.group()) for match in values]
        if not all(math.isfinite(value) and -100 <= value <= 100 for value in row_values):
            raise DBSValidationError("dbs_roe_value_out_of_range")
        confirmed.append(row_values[selected_column])
    if len(confirmed) != 1:
        raise DBSValidationError("dbs_cumulative_roe_table_missing_or_ambiguous")
    label = {1: "1Q", 2: "1H", 3: "9M", 4: "FY"}[quarter] + " " + str(year)
    return {"value": confirmed[0], "label": "ROE · " + label,
            "period": label + " · annualisiert", "period_start": date(year, 1, 1).isoformat(),
            "period_end": period_end.isoformat(), "as_of": period_end.isoformat(),
            "report_date": report_date.isoformat()}


def fetch_dbs_roe(now: datetime | None = None, *, opener=None, fetch=None) -> dict[str, Any]:
    """Confirm the newest cumulative issuer ROE, or raise DBSValidationError.

    At most the official HTML index and one PDF are fetched.  The resulting
    observation stays dated to the financial period even when rechecked daily.
    """
    instant = now or datetime.now(timezone.utc)
    if not isinstance(instant, datetime) or instant.tzinfo is None or instant.utcoffset() is None:
        raise DBSValidationError("dbs_timezone_aware_now_required")
    instant = instant.astimezone(timezone.utc)
    try:
        index = _read(INDEX_URL, MAX_INDEX_BYTES, opener=opener, fetch=fetch)
        source_url, year, quarter = _report_url(index)
        pdf = _read(source_url, MAX_PDF_BYTES, opener=opener, fetch=fetch)
        if not pdf.startswith(b"%PDF-"):
            raise DBSValidationError("dbs_invalid_pdf_signature")
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(pdf))
        if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PAGES:
            raise DBSValidationError("dbs_pdf_page_limit_or_encryption")
        pages = [(page.extract_text(extraction_mode="layout") or "") for page in reader.pages[:READ_PAGES]]
        if sum(map(len, pages)) > 500_000:
            raise DBSValidationError("dbs_pdf_text_limit")
        observation = _parse_pages(pages, year=year, quarter=quarter, today=instant.date())
    except DBSValidationError:
        raise
    except Exception as error:
        raise DBSValidationError("dbs_source_unavailable_or_unreadable") from error
    stamp = (instant if now is not None else datetime.now(timezone.utc)).isoformat(timespec="seconds")
    return {**observation, "provider": "DBS Group Holdings", "validation_version": 1,
            "fact_type": "issuer_reported_roe", "annualized": True,
            "accounting_basis": "IFRS", "currency": "SGD", "share_basis": "ordinary shareholders",
            "method": "Gemeldeter kumulierter ROE, annualisiert; Nettogewinn der Stammaktionäre nach Ausschüttungen auf andere Eigenkapitalinstrumente, ohne Minderheiten und andere Eigenkapitalinstrumente im Eigenkapital. Kein TTM-Wert.",
            "source_url": source_url, "source_urls": [source_url, INDEX_URL], "index_url": INDEX_URL,
            "retrieved_at": stamp, "verified_at": stamp}
