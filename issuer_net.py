"""Read Cloudflare's reported GAAP earnings; never substitute adjusted EPS.

The preferred path reconciles SEC company facts with the latest filed 10-Q/10-K.
If SEC is unavailable, four official quarterly press releases can establish the
TTM earnings sign. That fallback preserves the issuer's published rounding and
refuses a near-zero result whose sign the rounded figures cannot establish.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser
import json
import math
import re
from typing import Any, Callable
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

CIK = 1477333
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK0001477333.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK0001477333.json"
USER_AGENT = "ResearchStudio/1.0 research-studio.research-studio.workers.dev"
MAX_BYTES = 5_000_000
MAX_PERIOD_AGE_DAYS = 150
_QUARTERS = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
_WORDS = {1: "first", 2: "second", 3: "third", 4: "fourth"}
_FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A"}


def _clock(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("NET earnings verification requires an aware timestamp")
    return value.astimezone(timezone.utc)


def _day(value: Any) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Invalid earnings date")
    return date.fromisoformat(value)


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("Invalid earnings amount")
    return float(value)


def _quarter_end(year: int, quarter: int) -> date:
    return date(year, *_QUARTERS[quarter])


def _validate_period(end: date, now: datetime) -> None:
    if end != _quarter_end(end.year, (end.month - 1) // 3 + 1):
        raise ValueError("Earnings period is not a calendar quarter end")
    if not 0 <= (now.date() - end).days <= MAX_PERIOD_AGE_DAYS:
        raise ValueError("Latest earnings period is stale or in the future")


def _source_url(accession: str, document: str | None = None) -> str:
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession):
        raise ValueError("Invalid SEC accession")
    filename = document or accession + "-index.htm"
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:htm|html)", filename):
        raise ValueError("Invalid SEC document")
    return f"https://www.sec.gov/Archives/edgar/data/{CIK}/{accession.replace('-', '')}/{filename}"


def _metadata(now: datetime, start: date, end: date, report_date: date,
              value: float, urls: list[str], method: str) -> dict[str, Any]:
    _validate_period(end, now)
    if not end <= report_date <= now.date():
        raise ValueError("Report date does not match the available period")
    stamp = now.isoformat(timespec="seconds")
    return dict(validation_version=1, provider="Cloudflare IR / SEC",
                fact_type="gaap_ttm_earnings", ticker="NET", accounting_basis="US-GAAP",
                currency="USD", share_basis="Class A and Class B common stock",
                ttm_net_income_usd=value, period_start=start.isoformat(),
                period_end=end.isoformat(), as_of=end.isoformat(),
                report_date=report_date.isoformat(), source_url=urls[0], source_urls=urls,
                retrieved_at=stamp, verified_at=stamp, method=method)


def parse_sec_net_earnings(facts: dict[str, Any], submissions: dict[str, Any],
                           now: datetime | None = None) -> dict[str, Any]:
    """Require the exact latest filed period and exact USD NetIncomeLoss contexts."""
    moment = _clock(now)
    if not isinstance(facts, dict) or not isinstance(submissions, dict):
        raise ValueError("Invalid SEC response schema")
    if int(facts.get("cik", 0)) != CIK or int(submissions.get("cik", 0)) != CIK:
        raise ValueError("SEC issuer does not match Cloudflare")
    if "cloudflare" not in str(facts.get("entityName", "")).lower():
        raise ValueError("Unexpected SEC entity")
    filings = submissions.get("filings")
    recent = filings.get("recent") if isinstance(filings, dict) else None
    if not isinstance(recent, dict) or not isinstance(recent.get("form"), list):
        raise ValueError("Invalid SEC filings schema")
    filing_rows = []
    for index, form in enumerate(recent.get("form", [])):
        if form not in _FORMS:
            continue
        try:
            filed = _day(recent["filingDate"][index])
            end = _day(recent["reportDate"][index])
            if end <= filed <= moment.date():
                filing_rows.append((end, filed, recent["accessionNumber"][index],
                                    recent["primaryDocument"][index]))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    if not filing_rows:
        raise ValueError("No eligible SEC financial filing")
    end, report_date, accession, document = max(filing_rows)
    _validate_period(end, moment)
    income_facts = facts
    for key in ("facts", "us-gaap", "NetIncomeLoss", "units"):
        income_facts = income_facts.get(key) if isinstance(income_facts, dict) else None
    observations = income_facts.get("USD") if isinstance(income_facts, dict) else None
    if not isinstance(observations, list):
        raise ValueError("No USD US-GAAP NetIncomeLoss facts")

    def fact(start: date, finish: date, *, exact_accession: str | None = None) -> dict[str, Any]:
        eligible = []
        for item in observations:
            if not isinstance(item, dict) or item.get("form") not in _FORMS:
                continue
            try:
                if (_day(item.get("start")) == start and _day(item.get("end")) == finish
                        and finish <= _day(item.get("filed")) <= moment.date()
                        and (exact_accession is None or item.get("accn") == exact_accession)):
                    eligible.append({**item, "val": _number(item.get("val"))})
            except (ValueError, TypeError):
                continue
        if not eligible:
            raise ValueError("Missing exact SEC income duration")
        latest = max(item["filed"] for item in eligible)
        selected = [item for item in eligible if item["filed"] == latest]
        if len({item["val"] for item in selected}) != 1:
            raise ValueError("Conflicting SEC income facts")
        return selected[0]

    year_start = date(end.year, 1, 1)
    current = fact(year_start, end, exact_accession=accession)
    used = [current]
    if end.month == 12:
        value, start = current["val"], year_start
        method = "US-GAAP NetIncomeLoss für das vollständige Geschäftsjahr; exakter SEC-XBRL-USD-Wert, keine bereinigten Gewinne."
    else:
        prior_end = date(end.year - 1, end.month, end.day)
        prior_year = fact(date(end.year - 1, 1, 1), date(end.year - 1, 12, 31))
        prior_ytd = fact(date(end.year - 1, 1, 1), prior_end)
        used += [prior_year, prior_ytd]
        value = prior_year["val"] - prior_ytd["val"] + current["val"]
        start = prior_end + timedelta(days=1)
        method = ("TTM-US-GAAP-Nettoergebnis = Vorjahres-Geschäftsjahr − vergleichbare Vorjahres-YTD-Periode + aktuelle YTD-Periode; "
                  "exakte SEC-XBRL-USD-Werte, jeweils NetIncomeLoss. Keine Addition gerundeter EPS und keine Non-GAAP-Substitution.")
    urls = [_source_url(accession, document)]
    urls += [_source_url(item["accn"]) for item in used if item["accn"] != accession]
    result = _metadata(moment, start, end, report_date, value, list(dict.fromkeys(urls)), method)
    result.update(approximate=False, source_api_urls=[FACTS_URL, SUBMISSIONS_URL],
                  observations=[dict(start=item["start"], end=item["end"],
                                     net_income_usd=item["val"], filed=item["filed"],
                                     accession=item["accn"]) for item in used])
    return result


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data: str) -> None:
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def _release_url(year: int, quarter: int) -> str:
    segment = (f"fourth-quarter-and-fiscal-year-{year}" if quarter == 4
               else f"{_WORDS[quarter]}-quarter-{year}")
    publication_year = year + (quarter == 4)
    return f"https://www.cloudflare.com/press/press-releases/{publication_year}/cloudflare-announces-{segment}-financial-results/"


def parse_net_quarter_release(html: str, year: int, quarter: int,
                              now: datetime | None = None) -> dict[str, Any]:
    """Parse only the first current-quarter GAAP net-income narrative, not Non-GAAP."""
    moment = _clock(now)
    parser = _Text()
    parser.feed(html)
    text = " ".join(" ".join(parser.parts).split())
    expected_title = (f"Cloudflare Announces Fourth Quarter and Fiscal Year {year} Financial Results" if quarter == 4
                      else f"Cloudflare Announces {_WORDS[quarter].title()} Quarter {year} Financial Results")
    if expected_title not in text:
        raise ValueError("Quarter release title does not match")
    end = _quarter_end(year, quarter)
    # Check the actual reported period, not a date in unrelated guidance.
    period_text = end.strftime("%B %d, %Y").replace(" 0", " ")
    quarter_phrase = ("fourth quarter and fiscal year ended" if quarter == 4
                      else f"{_WORDS[quarter]} quarter ended")
    if f"{quarter_phrase} {period_text}" not in text:
        raise ValueError("Quarter release reported period does not match")
    published = re.search(r"Press release\.\s+([A-Z][a-z]+ \d{1,2}, \d{4})", text)
    if not published:
        raise ValueError("Quarter release publication date missing")
    report_date = datetime.strptime(published.group(1), "%B %d, %Y").date()
    if not end <= report_date <= moment.date() or (report_date - end).days > 100:
        raise ValueError("Quarter release publication date invalid")
    section = re.search(r"Net Income \(Loss\):\s*(?<![\w-])GAAP net (loss|income) was \$([\d,]+(?:\.\d+)?) million\b", text)
    if not section:
        raise ValueError("Current-quarter GAAP net income narrative missing")
    amount = Decimal(section.group(2).replace(",", "")) * 1_000_000
    if section.group(1) == "loss":
        amount = -amount
    decimals = len(section.group(2).partition(".")[2])
    rounding_bound = Decimal(500_000) / (10 ** decimals)
    return dict(period_end=end.isoformat(), report_date=report_date.isoformat(),
                net_income_usd=float(amount), rounding_bound_usd=float(rounding_bound),
                source_url=_release_url(year, quarter))


class _OfficialRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        _allowed_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _network_fetch(url: str) -> bytes:
    _allowed_url(url)
    request = Request(url, headers={"User-Agent": USER_AGENT,
                                   "Accept": "application/json,text/html;q=0.9"})
    with build_opener(_OfficialRedirectHandler()).open(request, timeout=12) as response:
        _allowed_url(response.url)
        return response.read(MAX_BYTES + 1)


def _allowed_url(url: str) -> None:
    try:
        parsed = urlparse(url)
        valid = (parsed.scheme == "https" and parsed.hostname in {"data.sec.gov", "www.sec.gov", "www.cloudflare.com"}
                 and parsed.username is None and parsed.password is None
                 and parsed.port in {None, 443} and "#" not in url)
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Unexpected earnings source host")


def _completed(result: dict[str, Any], explicit_now: datetime | None) -> dict[str, Any]:
    # A no-argument live fetch is verified when all responses have finished.
    # Explicit test clocks remain deterministic across parsers and fetch paths.
    stamp = _clock(explicit_now).isoformat(timespec="seconds")
    return {**result, "retrieved_at": stamp, "verified_at": stamp}


def fetch_net_earnings(now: datetime | None = None,
                       fetcher: Callable[[str], bytes] | None = None) -> dict[str, Any]:
    """Fetch at most two SEC documents plus five official releases; fail closed."""
    moment = _clock(now)
    fetch = fetcher or _network_fetch

    def load(url: str) -> str:
        _allowed_url(url)
        content = fetch(url)
        if isinstance(content, str):
            content = content.encode("utf-8")
        if not isinstance(content, bytes) or len(content) > MAX_BYTES:
            raise ValueError("Earnings response exceeds byte limit")
        return content.decode("utf-8")

    try:
        submissions = json.loads(load(SUBMISSIONS_URL))
        facts = json.loads(load(FACTS_URL))
        return _completed(parse_sec_net_earnings(facts, submissions, moment), now)
    except (OSError, ValueError, TypeError, KeyError):
        pass

    # The newest completed quarter can be unreported. Try it, then the previous
    # quarter; each returned release must prove its exact period and date.
    current_quarter = (moment.month - 1) // 3 + 1
    latest_ordinal = moment.year * 4 + current_quarter - 2
    latest = None
    for ordinal in (latest_ordinal, latest_ordinal - 1):
        year, offset = divmod(ordinal, 4)
        quarter = offset + 1
        end = _quarter_end(year, quarter)
        try:
            _validate_period(end, moment)
            latest = parse_net_quarter_release(load(_release_url(year, quarter)), year, quarter, moment)
            latest_ordinal = ordinal
            break
        except (OSError, ValueError, TypeError):
            continue
    if latest is None:
        raise ValueError("No current Cloudflare GAAP earnings report could be verified")
    observations = [latest]
    for ordinal in range(latest_ordinal - 1, latest_ordinal - 4, -1):
        year, offset = divmod(ordinal, 4)
        observations.append(parse_net_quarter_release(load(_release_url(year, offset + 1)), year, offset + 1, moment))
    value = sum(item["net_income_usd"] for item in observations)
    bound = sum(item["rounding_bound_usd"] for item in observations)
    if abs(value) <= bound:
        raise ValueError("Published rounding cannot establish the TTM earnings sign")
    end = _day(latest["period_end"])
    oldest_end = _day(observations[-1]["period_end"])
    oldest_start_month = oldest_end.month - 2
    start = date(oldest_end.year, oldest_start_month, 1)
    result = _metadata(moment, start, end, _day(latest["report_date"]), value,
                       [item["source_url"] for item in observations],
                       "Summe der vier aufeinanderfolgenden Quartals-Nettoergebnisse aus offiziellen Cloudflare-Mitteilungen, jeweils GAAP. "
                       "Die publizierten Mio.-USD-Beträge sind gerundet; nur ein gegenüber der gesamten Rundungsgrenze eindeutiges Vorzeichen wird akzeptiert. "
                       "Kein exakter TTM-EPS-Ausweis und keine Non-GAAP-Substitution.")
    result.update(approximate=True, rounding_bound_usd=bound, observations=observations)
    return _completed(result, now)
