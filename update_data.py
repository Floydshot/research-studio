#!/usr/bin/env python3
"""Refresh the dashboard, retaining the provenance of every fallback.

Run ``python update_data.py`` for one atomic Yahoo Finance/CNN/ECB snapshot,
or add ``--seed`` for supplied starting values without network or dependencies.
``refresh_batch`` identifies the full run; original field and source timestamps
remain unchanged when a provider fails and confirmed previous values are kept.
Historical multiples and buy_trigger strings below are user-supplied fixed
references unless separate issuer evidence and calculation metadata are supplied.
Stock tranches implement the requested forward-P/E thresholds, not the free-text
company-specific triggers. ETFs are assessed separately without stock thresholds.
All missing prices/technicals remain JSON null.
"""

from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from io import BytesIO
import json
import math
import multiprocessing as mp
import os
from pathlib import Path
import re
import ssl
from statistics import median
import sys
import tempfile
import time
from typing import Any, Callable
from urllib.request import Request, urlopen
from urllib.parse import quote, urljoin, urlparse
from zoneinfo import ZoneInfo
from refresh_repair import repair_incomplete_stocks
from history_corrections import apply_history_corrections


BERLIN = ZoneInfo("Europe/Berlin")
REFERENCE_WARNING = (
    "Historische Multiples, Dividendenreferenzen und Nachkauf-Indikatoren sind "
    "vorgegebene, nicht unabhängig geprüfte Referenzwerte, sofern nicht separat "
    "belegt; der 5J/10J-Zeitraum ist nicht je Aktie belegt. "
    "Tranchen folgen nur den Forward-KGV-Schwellen."
)
INITIAL_WARNING = "Startwerte aus dem Briefing; keine aktuellen Yahoo-Finance-Daten."
NUMERIC_FIELDS = (
    "current_pe", "forward_pe", "ps_ratio", "div_yield", "roe", "price",
    "sma200", "rsi14",
)
ETF_NUMERIC_FIELDS = ("distribution_yield_ttm", "distributions_per_share_ttm")
ETF_SOURCE = "https://am.jpmorgan.com/content/dam/jpm-am-aem/americas/us/en/literature/fact-sheet/etfs/FS-JEPQ.PDF"
TSM_TICKER = "TSM"
TSM_FX_SOURCE = "https://finance.yahoo.com/quote/TWD%3DX/"
TSM_PS_SOURCE = "https://finance.yahoo.com/quote/TSM/"
TSM_PS_VERSION = 1
FEAR_GREED_SOURCE = "https://edition.cnn.com/markets/fear-and-greed"
FEAR_GREED_ENDPOINT = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
FEAR_GREED_VERSION = 1
FEAR_GREED_MAX_AGE = 4 * 86400
FEAR_GREED_COMPARISONS = {"previous_close": "previous_close", "previous_week": "previous_1_week",
                         "previous_month": "previous_1_month", "previous_year": "previous_1_year"}
FEAR_GREED_RATINGS = {"extreme fear", "fear", "neutral", "greed", "extreme greed"}
TOKIO_TICKER = "8766.T"
TOKIO_DIVIDEND_SOURCE = "https://www.tokiomarinehd.com/ir/stock/return.html"
TOKIO_REPORT_INDEX = "https://www.tokiomarinehd.com/en/ir/download/{year}.html"
TOKIO_SPLIT_FACTOR = 15
TOKIO_PRE_SPLIT_ISSUED = 1_934_000_000
TOKIO_POST_SPLIT_ISSUED = 29_010_000_000
TOKIO_METRIC_VERSION = 1
TOKIO_HIST_DIV_SOURCE = "https://www.tokiomarinehd.com/en/ir/download/Integrated_Report_2026_Print_e.pdf"
TOKIO_HIST_PE_OBSERVATIONS = ((2021, 11.62), (2022, 13.66), (2023, 13.38), (2024, 10.58), (2025, 14.18))
TOKIO_HIST_PE_MEAN = round(math.fsum(pe for _, pe in TOKIO_HIST_PE_OBSERVATIONS) / len(TOKIO_HIST_PE_OBSERVATIONS), 6)
TOKYO = ZoneInfo("Asia/Tokyo")
RETURN_PERIODS = ("1d", "1w", "1m", "6m", "ytd", "1y", "3y", "5y", "10y", "all")
RETURN_VERSION = 1
PRICE_RETURN_METHOD = "split_adjusted_price_return_excluding_distributions"
JEPQ_RETURN_CASH_SOURCE = "https://am.jpmorgan.com/FundsMarketingHandler/historicalData?cusip=46654Q203&country=us&role=adv&userLoggedIn=false&language=en"
RETURN_METHOD = (
    "Gesamtrendite in Handelswährung aus splitbereinigten Schlusskursen und tatsächlichen "
    "Barausschüttungen; Ausschüttungen werden am Ex-Tag zum Schlusskurs wiederangelegt. "
    "Keine Gebühren oder Steuern. Kalenderzeiträume verwenden den letzten Handelsschluss "
    "am oder vor dem Stichtag; 1D verwendet die vorherige Handelssitzung. "
    "3J, 5J, 10J und All Time ab einem Jahr sind mit tatsächlichen Tagen / 365,2425 annualisiert. "
    "All Time beginnt beim ersten verfügbaren Kurs, nicht bei einem persönlichen Kaufdatum."
)
AVAILABLE_HISTORY_METHOD = (
    "Längster zusammenhängender Zeitraum ohne erkannte Datenbrüche; "
    "Gesamtrendite in Handelswährung aus splitbereinigten Schlusskursen und tatsächlichen "
    "Barausschüttungen mit Wiederanlage am Ex-Tag. Ab einem Jahr mit tatsächlichen Tagen "
    "/ 365,2425 annualisiert. Keine rückwirkend ergänzte 5J-/10J- oder All-Time-Historie."
)


def tokio_historical_pe_metadata() -> dict[str, Any]:
    """Audited issuer year-end PER series; distinct from the current IFRS ratios."""
    return dict(
        provider="Tokio Marine IR", fact_type="historical_year_end_pe_mean",
        label="Hist. KGV 5J", accounting_basis="J-GAAP", period="FY2021–FY2025",
        source_url=TOKIO_HIST_DIV_SOURCE + "#page=75", source_page=75,
        source_printed_page=74, verified_at="2026-10-22", sample_count=len(TOKIO_HIST_PE_OBSERVATIONS),
        observations=[dict(fiscal_year=fy, pe=pe) for fy, pe in TOKIO_HIST_PE_OBSERVATIONS],
        method=("Arithmetisches Mittel der fünf vom Emittenten veröffentlichten Jahresend-KGVs "
                "FY2021–FY2025: (11,62 + 13,66 + 13,38 + 10,58 + 14,18) ÷ 5 = 12,684×. "
                "J-GAAP; kein Durchschnitt täglicher KGVs und keine Neuberechnung aus gerundeten EPS. "
                "Laut Berichtsfußnote wenden ausländische IFRS-Tochtergesellschaften seit FY2023 IFRS 17 "
                "an; betroffene FY2022-Angaben sind rückwirkend angepasst. Die KGV-Verhältnisse sind "
                "bei einheitlicher Kurs-/EPS-Basis unabhängig von Aktiensplits."),
        comparable_to_current=False,
        comparison_note="Aktuelles KGV: IFRS; historische Referenz: J-GAAP. Methodenwechsel; kein direkter Bewertungsaufschlag ableitbar.")


# Verified fiscal-year diluted GAAP EPS, paired with split-adjusted (not
# dividend-adjusted) USD Close on the final trading session of that year.
# These are five ex-post annual observations, not a daily TTM backtest.
US_PE_REFERENCES = {
    "GS": dict(provider="Goldman Sachs IR + Yahoo Finance", eps=(59.45, 30.06, 22.87, 40.54, 51.32),
               closes=(382.55, 343.38, 385.77, 572.62, 879.00),
               early_source="https://www.goldmansachs.com/investor-relations/financials/10k/2023/2023-10-k.pdf#page=71",
               late_source="https://www.goldmansachs.com/investor-relations/financials/8k/2026/8k-01-15-26.pdf#page=15",
               note="2023 enthält Belastungen aus dem Rückzug aus dem Konsumentengeschäft. Investmentbanking-Gewinne sind zyklisch; der Median glättet einzelne Jahre, beseitigt diese Risiken aber nicht."),
    "JPM": dict(provider="JPMorgan Chase IR + Yahoo Finance", eps=(15.36, 12.09, 16.23, 19.75, 20.02),
                closes=(158.35, 134.10, 170.10, 239.71, 322.22),
                early_source="https://www.jpmorganchase.com/ir/annual-report/2025/ar-ceo-letters",
                late_source="https://www.jpmorganchase.com/ir/annual-report/2025/ar-ceo-letters",
                note="2021 profitierte von Reserveauflösungen; 2024 enthält einen Netto-Visa-Sondereffekt. Kreditkosten und Kapitalquoten vor einem Nachkauf separat prüfen."),
    "MS": dict(provider="Morgan Stanley IR + Yahoo Finance", eps=(8.03, 6.15, 5.18, 7.95, 10.21),
               closes=(98.16, 85.02, 93.25, 125.72, 177.53),
               early_source="https://www.morganstanley.com/about-us-ir/shareholder/10k2023/10k1223.pdf#page=31",
               late_source="https://www.morganstanley.com/content/dam/msdotcom/en/about-us-ir/shareholder/10k2025/10k1225.pdf#page=29",
               note="2023 enthält zusätzliche FDIC-, Rechts-, Personal- und Integrationskosten. GAAP-ROE und bereinigtes ROTCE sind unterschiedliche Kennzahlen; ein fehlendes ROE wird nicht ersetzt."),
    "AFL": dict(provider="Aflac IR + Yahoo Finance", eps=(6.25, 6.93, 7.78, 9.63, 6.82),
                closes=(58.39, 71.94, 82.50, 103.44, 110.27),
                early_source="https://investors.aflac.com/files/doc_downloads/shareholder-meeting/2024/AFL-12-31-23-10-K.pdf#page=88",
                late_source="https://investors.aflac.com/press-releases/press-release-details/2026/Aflac-Incorporated-Announces-Fourth-Quarter-Results-Reports-Fourth-Quarter-Net-Earnings-of-1-4-Billion-Reiterates-Increase-in-First-Quarter-Dividend-of-5-2/default.aspx",
                note="2021–2022 sind nach LDTI rückwirkend angepasst (2021: 6,25 USD). Anlage-, Derivate- und Währungseffekte beeinflussen GAAP-Gewinne erheblich; deshalb zusätzlich die Forward-Bewertung prüfen."),
}
US_PE_DATES = ("2021-12-31", "2022-12-30", "2023-12-29", "2024-12-31", "2025-12-31")


def us_historical_pe_metadata(ticker: str) -> dict[str, Any]:
    ref = US_PE_REFERENCES[ticker]
    observations = [dict(fiscal_year=2021 + i, price_date=US_PE_DATES[i], close=close,
                         diluted_eps=eps, pe=round(close / eps, 9),
                         eps_source_url=ref["early_source"] if i < 3 else ref["late_source"])
                    for i, (close, eps) in enumerate(zip(ref["closes"], ref["eps"]))]
    return dict(provider=ref["provider"], fact_type="historical_year_end_pe_median",
                label="5J-Median · JE", accounting_basis="US-GAAP", period="FY2021–FY2025",
                source_url=ref["late_source"], price_source_url=f"https://finance.yahoo.com/quote/{ticker}/history/",
                verified_at="2026-11-04", sample_count=5, observations=observations,
                method="Median der fünf Jahresend-KGVs 2021–2025: letzter splitbereinigter Schlusskurs des Jahres in USD ÷ verwässertes GAAP-Jahres-EPS in USD. Kurse ohne Dividendenbereinigung. Nachträglich rekonstruierte Referenz; kein Durchschnitt täglicher TTM-KGVs und kein historisch handelbarer Backtest. Bei Aflac sind 2021–2022 nach LDTI rückwirkend angepasst.",
                comparable_to_current=True, comparison_note=ref["note"])


def apply_us_historical_reference(stock: dict[str, Any]) -> None:
    if stock.get("ticker") not in US_PE_REFERENCES:
        return
    meta = us_historical_pe_metadata(stock["ticker"])
    stock["hist_pe"] = round(median(close / eps for close, eps in zip(
        US_PE_REFERENCES[stock["ticker"]]["closes"], US_PE_REFERENCES[stock["ticker"]]["eps"])), 6)
    stock.setdefault("field_metadata", {})["hist_pe"] = meta
    stock["buy_trigger"] = "TTM-KGV mindestens 15% unter eigenem 5J-Jahresend-Median, Forward-KGV höchstens auf Referenzniveau, Kurs ≤ SMA 200 und RSI (14) ≤ 50."


# Audited Taiwan-IFRS annual USD EPS per ADS and USD revenue. The issuer
# translates annual results at the annual weighted-average FX rate. One ADS
# already represents five ordinary shares; never multiply the USD EPS by five.
TSM_HISTORICAL_INPUTS = ({'fiscal_year': 2021,
  'price_date': '2021-12-31',
  'close': 120.31,
  'diluted_eps': 4.12,
  'eps_unit': 'USD je ADS',
  'revenue_usd': 56822000000,
  'ordinary_shares_issued': 25930380000,
  'ads_ratio': 5,
  'annual_average_usd_twd': 27.937,
  'eps_source_url': 'https://investor.tsmc.com/english/encrypt/files/encrypt_file/reports/2022-02/b59a98158c3db59117bf162d05d7778874715e4b/FS.pdf',
  'shares_source_url': 'https://investor.tsmc.com/sites/ir/financial-report/2022/TSMC%202022Q4%20Consolidated%20Financial%20Statements_E_1.pdf'},
 {'fiscal_year': 2022,
  'price_date': '2022-12-30',
  'close': 74.49,
  'diluted_eps': 6.57,
  'eps_unit': 'USD je ADS',
  'revenue_usd': 75881000000,
  'ordinary_shares_issued': 25930380000,
  'ads_ratio': 5,
  'annual_average_usd_twd': 29.835,
  'eps_source_url': 'https://investor.tsmc.com/english/encrypt/files/encrypt_file/reports/2023-02/8dd8b6bbd00c6c6fc6b6880790247c714597212c/FS.pdf',
  'shares_source_url': 'https://investor.tsmc.com/sites/ir/financial-report/2022/TSMC%202022Q4%20Consolidated%20Financial%20Statements_E_1.pdf'},
 {'fiscal_year': 2023,
  'price_date': '2023-12-29',
  'close': 104.0,
  'diluted_eps': 5.18,
  'eps_unit': 'USD je ADS',
  'revenue_usd': 69298000000,
  'ordinary_shares_issued': 25932071000,
  'ads_ratio': 5,
  'annual_average_usd_twd': 31.195,
  'eps_source_url': 'https://investor.tsmc.com/english/encrypt/files/encrypt_file/reports/2024-02/eb3eca87abc2668d3c8d5ca2eaa005b8f3f582b4/FS_audited.pdf',
  'shares_source_url': 'https://investor.tsmc.com/sites/ir/financial-report/2023/TSMC%202023Q4%20Consolidated%20Financial%20Statements_E.pdf'},
 {'fiscal_year': 2024,
  'price_date': '2024-12-31',
  'close': 197.49,
  'diluted_eps': 7.04,
  'eps_unit': 'USD je ADS',
  'revenue_usd': 90083000000,
  'ordinary_shares_issued': 25932733000,
  'ads_ratio': 5,
  'annual_average_usd_twd': 32.129,
  'eps_source_url': 'https://investor.tsmc.com/english/encrypt/files/encrypt_file/reports/2025-02/5619e02969294761af7084c9aa9b702f04ed9bee/FS_audited.pdf',
  'shares_source_url': 'https://investor.tsmc.com/sites/ir/financial-report/2025/TSMC%202025Q4%20Consolidated%20Financial%20Statements_E_1.pdf'},
 {'fiscal_year': 2025,
  'price_date': '2025-12-31',
  'close': 303.89,
  'diluted_eps': 10.65,
  'eps_unit': 'USD je ADS',
  'revenue_usd': 122424000000,
  'ordinary_shares_issued': 25932524000,
  'ads_ratio': 5,
  'annual_average_usd_twd': 31.114,
  'eps_source_url': 'https://investor.tsmc.com/english/encrypt/files/encrypt_file/reports/2026-02/cb73d5f4e019a8f6d7a494a0f8f6c6da2dfc4ee2/FS.pdf',
  'shares_source_url': 'https://investor.tsmc.com/sites/ir/financial-report/2025/TSMC%202025Q4%20Consolidated%20Financial%20Statements_E_1.pdf'})


def tsm_historical_metadata(field: str) -> dict[str, Any]:
    observations = [dict(item, pe=round(item["close"] / item["diluted_eps"], 9),
                         ps=round(item["close"] * (item["ordinary_shares_issued"] / item["ads_ratio"]) / item["revenue_usd"], 9))
                    for item in TSM_HISTORICAL_INPUTS]
    pe = field == "hist_pe"
    return dict(provider="TSMC IR + Yahoo Finance", validation_version=1,
                fact_type="historical_year_end_pe_median" if pe else "historical_year_end_ps_median",
                label="5J-Median · JE" if pe else "Hist. P/S 5J",
                source_label="TIFRS · IR / Kurse", accounting_basis="Taiwan-IFRS",
                period="FY2021–FY2025", verified_at="2026-11-17", sample_count=5,
                source_url=TSM_HISTORICAL_INPUTS[-1]["eps_source_url"],
                price_source_url="https://finance.yahoo.com/quote/TSM/history/",
                observations=observations,
                method=("Median der fünf Jahresend-KGVs 2021–2025: letzter splitbereinigter NYSE-Schlusskurs je ADS in USD ÷ veröffentlichtes verwässertes Taiwan-IFRS-Jahres-EPS je ADS in USD. "
                        if pe else "Median der fünf Jahresend-KUVs 2021–2025: letzter splitbereinigter NYSE-Schlusskurs je ADS in USD × ausgegebene Stammaktien zum Jahresende ÷ 5 ÷ veröffentlichter Taiwan-IFRS-Jahresumsatz in USD. Aktienzahlen auf berichtete Tausend gerundet. ")
                       + "USD-Jahreszahlen des Emittenten zum gewichteten Jahresdurchschnittskurs; keine erneute Währungs- oder ADS-Umrechnung. Schlusskurse ohne Dividendenbereinigung. Nachträglich rekonstruierte Jahresend-Stichprobe; kein Durchschnitt täglicher TTM-Multiples und kein historisch handelbarer Backtest.",
                comparable_to_current=True,
                comparison_note=("Jahresend-Referenz auf USD-ADS-Basis; aktuelles Yahoo-KGV verwendet rollierende TTM-Gewinne, Forward-KGV Analystenschätzungen. Wechselkursübersetzung und Berichtszeitraum können leicht abweichen. Der Bewertungsabstand ist kein Kurspotenzial." if pe else "Historische USD-Jahresumsätze verwenden den gewichteten Jahresdurchschnittskurs; das aktuelle KUV verwendet TTM-Umsatz zum zuletzt abgerufenen USD/TWD-Kurs. Zeitraum und FX-Basis unterscheiden sich. Keine punktgenau vergleichbare Bewertungsprämie und kein Kursziel."))


def apply_tsm_historical_reference(stock: dict[str, Any]) -> None:
    if stock.get("ticker") != TSM_TICKER:
        return
    for field, ratio in (("hist_pe", "pe"), ("hist_ps", "ps")):
        meta = tsm_historical_metadata(field)
        stock[field] = round(median(item[ratio] for item in meta["observations"]), 6)
        stock.setdefault("field_metadata", {})[field] = meta


def add_buy_indicator(stock: dict[str, Any], now: datetime | None = None) -> None:
    """Evaluate an explicit, conservative screen; never turn old data green."""
    is_tsm = stock.get("ticker") == TSM_TICKER
    if stock.get("ticker") not in US_PE_REFERENCES and not is_tsm:
        return
    now = now or datetime.now(timezone.utc)
    stamp = iso_now(now)
    reference_pe = finite_number(stock.get("hist_pe"))
    target = 25.0 if is_tsm else reference_pe * 0.85 if reference_pe and reference_pe > 0 else None
    fields = ("current_pe", "forward_pe", "price", "sma200", "rsi14", "currency")

    def fresh(field: str) -> bool:
        if stock.get("field_sources", {}).get(field) != "live":
            return False
        try:
            instant = datetime.fromisoformat(stock.get("field_updated_at", {}).get(field) or "")
            if instant.tzinfo is None:
                return False
            return -300 <= (now - instant).total_seconds() <= 4 * 86400
        except (ValueError, TypeError):
            return False

    values = {field: finite_number(stock.get(field)) for field in fields if field != "currency"}
    valid = target is not None and stock.get("currency") == "USD" and all(fresh(field) for field in fields)
    valid = valid and all(values[field] is not None and values[field] > 0 for field in ("current_pe", "forward_pe", "price", "sma200"))
    valid = valid and values["rsi14"] is not None and 0 <= values["rsi14"] <= 100
    try:
        quote_time = datetime.fromisoformat(stock.get("as_of") or "")
        valid = valid and quote_time.tzinfo is not None and -300 <= (now - quote_time).total_seconds() <= 4 * 86400
    except (TypeError, ValueError):
        valid = False
    if is_tsm:
        try:
            technical_time = datetime.fromisoformat(stock.get("technicals_as_of") or "")
            valid = valid and technical_time.tzinfo is not None and -300 <= (now - technical_time).total_seconds() <= 4 * 86400
        except (TypeError, ValueError):
            valid = False
    checks = [("current_pe", "TTM-KGV ≤ 25" if is_tsm else "TTM-KGV ≤ 85% der eigenen Referenz", target),
              ("forward_pe", "Forward-KGV ≤ 20" if is_tsm else "Forward-KGV ≤ eigene Referenz", 20.0 if is_tsm else reference_pe),
              ("sma200", "Kurs ≤ SMA 200", values["sma200"]),
              ("rsi14", "RSI (14) ≤ 45" if is_tsm else "RSI (14) ≤ 50", 45 if is_tsm else 50)]
    conditions = []
    for key, label, threshold in checks:
        actual = values["price"] if key == "sma200" else values[key]
        conditions.append(dict(key=key, label=label, actual=actual, threshold=threshold,
                               passed=actual <= threshold if valid else None))
    passed = sum(item["passed"] is True for item in conditions)
    state = "unavailable" if not valid else "eligible" if passed == len(conditions) else "watch"
    target_price = values["price"] / values["current_pe"] * target if valid else None
    target_text = format(target, ".2f").replace(".", ",") if valid else ""
    reference_text = format(20.0 if is_tsm else reference_pe, ".2f").replace(".", ",") if valid else ""
    summary = (f"{passed}/4 Bedingungen erfüllt. TTM ≤ {target_text}×; Forward ≤ {reference_text}×; Kurs ≤ SMA 200; RSI ≤ {45 if is_tsm else 50}." if valid
               else "Keine Auswertung: positive aktuelle KGVs, Kurs, SMA 200, RSI und USD-Kursbasis müssen frisch bestätigt sein.")
    stock["buy_indicator"] = dict(version=1, status=state,
        label={"eligible": "Nachkauf prüfen", "watch": "Abwarten", "unavailable": "Daten prüfen"}[state],
        summary=summary, rule=(
            "Vorgeschlagene TSMC-Regel: " + stock.get("buy_trigger", "") +
            " Bewertung und Rücksetzer müssen gleichzeitig passen. Die KGV-Grenzen sind bewusst gewählte absolute Obergrenzen für den profitablen, aber zyklischen Chiphersteller; RSI ≤ 45 und Kurs ≤ SMA 200 verlangen einen Rücksetzer. "
            "Eigener Vorschlag, nicht rückgetestet; die Schwellen sind eigenständig gewählt und nicht aus der historischen Referenz abgeleitet. Forward-KGV beruht auf veränderlichen Analystenschätzungen. "
            "TTM-Prüfschwelle = aktueller USD-ADS-Kurs ÷ TTM-KGV × 25 bei unverändertem TTM-EPS; kein Kursziel. "
            "Alle vier Bedingungen müssen mit frischen USD-Daten erfüllt sein. Vor einem Nachkauf Gewinnprognosen, Investitionsbedarf und Taiwan-Risiko prüfen."
            if is_tsm else stock.get("buy_trigger", "") + " Eigene Screening-Regel mit gewähltem 15%-Puffer, nicht rückgetestet. Forward-KGV ist eine Konsensschätzung und kann auf bereinigten Gewinnen beruhen. TTM-Prüfkurs = aktueller Kurs ÷ TTM-KGV × Prüfschwelle bei unverändertem TTM-EPS; kein Kursziel. Bilanz, Kapitalausstattung und Gewinnqualität vor einem Kauf separat prüfen. " + US_PE_REFERENCES[stock["ticker"]]["note"]),
        target_pe=round(target, 6) if target is not None else None,
        target_price=round(target_price, 2) if target_price is not None else None,
        currency=stock.get("currency"), discount_pct=None if is_tsm else 15, conditions=conditions,
        as_of=stock.get("as_of"), evaluated_at=stamp, reference_period=None if is_tsm else "FY2021–FY2025")
    if is_tsm:
        stock["buy_indicator"].update(
            basis="proposed_absolute_thresholds",
            sources=[dict(label="TSMC Q3 2026", url="https://investor.tsmc.com/english/quarterly-results/2026/q3"),
                     dict(label="TSMC Geschäftsrisiken", url="https://www.sec.gov/Archives/edgar/data/1046179/000162828026025362/tsm-20251231.htm")])


def reference(name: str, ticker: str, cluster: str, hist_pe: float | None,
              buy_trigger: str, current_pe: float | None, forward_pe: float | None,
              *, hist_ps: float | None = None, ps_ratio: float | None = None,
              hist_div: float | None = None, div_yield: float | None = None,
              roe: float | None = None) -> dict[str, Any]:
    return dict(name=name, ticker=ticker, cluster=cluster, hist_pe=hist_pe,
                hist_ps=hist_ps, hist_div=hist_div, buy_trigger=buy_trigger,
                initial=dict(current_pe=current_pe, forward_pe=forward_pe,
                             ps_ratio=ps_ratio, div_yield=div_yield, roe=roe))


BIG_TECH = "Big Tech"
SEMICONDUCTOR = "Semiconducter"
CYBERSECURITY = "Cybersecurity"
SAAS = "SaaS"
CHART_CAPPED_TICKERS = frozenset({"NOW", "PANW", "CRWD", "NET"})
EU_INSURANCE = "Erstversicherer Europa"
ASIA = "Asien & Japan Finanzen"
REINSURANCE = "Rückversicherer"
US_BANKS = "Banken USA"
US_INSURANCE = "Versicherer USA"
PORTFOLIOS = [
    dict(id="tech", name="Tech Stonks", cluster_order=[BIG_TECH, SEMICONDUCTOR, CYBERSECURITY, SAAS], stocks=[
        reference("Alphabet", "GOOGL", BIG_TECH, 24.3, "Forward P/E < 17x oder Wochen-EMA 50", 16.9, 16.8, hist_ps=6.2, ps_ratio=6.5),
        reference("Meta", "META", BIG_TECH, 25.9, "Forward P/E 18x–19x & Tages-RSI < 40", 27.7, 19.7, hist_ps=7.8, ps_ratio=9.2),
        reference("Broadcom", "AVGO", BIG_TECH, 65.7, "Forward P/E 19x–21x oder Tages-SMA 100/200", 41.4, 21.0, hist_ps=8.1, ps_ratio=18.3),
        reference("Amazon", "AMZN", BIG_TECH, 63.0, "Forward P/E < 24x oder P/OCF < 18x", 21.1, 23.6, hist_ps=3.5, ps_ratio=3.8),
        reference("Nvidia", "NVDA", SEMICONDUCTOR, 54.9, "Forward P/E 22x–24x oder Tages-SMA 200", 27.4, 24.9, hist_ps=20.5, ps_ratio=24.0),
        reference("Microsoft", "MSFT", BIG_TECH, 33.0, "Forward P/E 23x–24x oder Test SMA 200", 29.6, 25.1, hist_ps=11.2, ps_ratio=13.5),
        reference("ServiceNow", "NOW", SAAS, 147.5, "Forward P/E < 28x oder P/FCF < 30x", 87.5, 27.8, hist_ps=15.8, ps_ratio=16.5),
        reference("Apple", "AAPL", BIG_TECH, 33.0, "Abwarten auf Forward P/E < 28x", 39.3, 35.2, hist_ps=7.4, ps_ratio=9.8),
        reference("AMD", "AMD", SEMICONDUCTOR, 134.5, "Forward P/E 28x–32x oder Wochen-SMA 50", 137.1, 39.5, hist_ps=8.5, ps_ratio=11.5),
        reference("Arista Networks", "ANET", SEMICONDUCTOR, 46.5, "Rücksetzer auf Forward P/E < 38x–40x", 67.9, 48.5, hist_ps=14.5, ps_ratio=22.0),
        reference("TSMC", "TSM", SEMICONDUCTOR, None, "Forward-KGV ≤ 20, TTM-KGV ≤ 25, Kurs ≤ SMA 200 und RSI (14) ≤ 45; alle vier Bedingungen gemeinsam.", None, None),
        reference("Palo Alto Networks", "PANW", CYBERSECURITY, 173.8, "EV/FCF < 30x–32x (Earnings-Dip nutzen)", 271.9, 93.5, hist_ps=11.5, ps_ratio=18.2),
        reference("CrowdStrike", "CRWD", CYBERSECURITY, 200.0, "Strikte Disziplin: P/S < 25x oder P/FCF < 35x", 250.0, 208.3, hist_ps=27.1, ps_ratio=43.2),
        reference("Cloudflare", "NET", CYBERSECURITY, 210.0, "Kompression auf P/S 20x–25x abwarten", None, 226.9, hist_ps=31.1, ps_ratio=41.7),
    ]),
    dict(id="value", name="Value Stonks", cluster_order=[EU_INSURANCE, REINSURANCE, ASIA, US_BANKS, US_INSURANCE, "Income-ETFs"], stocks=[
        reference("AXA SA", "CS.PA", EU_INSURANCE, 11.2, "Forward P/E < 9,5x & Div.-Rendite > 5,7%", 8.5, 8.3, hist_div=5.4, div_yield=6.0, roe=15.0),
        reference("Sompo Holdings", "8630.T", ASIA, 11.5, "Forward P/E < 10x oder P/B < 1,25x", 8.9, 9.5, hist_div=3.1, div_yield=3.4, roe=14.0),
        reference("Zurich Insurance", "ZURN.SW", EU_INSURANCE, 14.9, "P/E < 11,5x & Div.-Rendite > 5,0%", 9.0, 9.8, hist_div=5.1, div_yield=5.2, roe=36.0),
        reference("Swiss Re", "SREN.SW", REINSURANCE, 13.8, "Forward P/E < 10x oder Div.-Rendite > 4,5%", 10.4, 10.1, hist_div=5.5, div_yield=4.2, roe=17.0),
        reference("Munich Re", "MUV2.DE", REINSURANCE, 13.5, "Forward P/E < 11x oder Test Tages-SMA 200", 11.2, 10.5, hist_div=4.3, div_yield=4.1, roe=19.0),
        reference("Hannover Rück", "HNR1.DE", REINSURANCE, 14.3, "Forward P/E < 11,5x & Div.-Rendite > 4,5%", 11.7, 11.2, hist_div=4.2, div_yield=4.6, roe=22.0),
        reference("MS&AD Insurance", "8725.T", ASIA, 9.0, "Forward P/E < 10,5x oder Div.-Rendite > 3,6%", 8.8, 12.3, hist_div=3.5, div_yield=3.2, roe=17.5),
        reference("Allianz", "ALV.DE", EU_INSURANCE, 12.5, "Forward P/E < 11,5x oder Div.-Rendite > 5,5%", 13.9, 12.4, hist_div=5.2, div_yield=5.2, roe=16.0),
        reference("Tokio Marine", "8766.T", ASIA, TOKIO_HIST_PE_MEAN, "Rücksetzer auf Forward P/E 12x–13x abwarten", None, None, hist_div=3.220123649, div_yield=None, roe=None),
        reference("DBS Group Holdings", "D05.SI", ASIA, 11.8, "Disziplin: Warten auf Forward P/E < 12,5x", 17.8, 16.6, hist_div=4.9, div_yield=4.3, roe=16.2),
        reference("Swiss Life", "SLHN.SW", EU_INSURANCE, 14.2, "Forward P/E < 14,5x oder Div.-Rendite > 5,2%", 19.5, 17.2, hist_div=5.1, div_yield=4.8, roe=15.0),
        reference("Goldman Sachs", "GS", US_BANKS, None, "Noch kein individueller Nachkauf-Indikator hinterlegt.", None, None),
        reference("JPMorgan Chase", "JPM", US_BANKS, None, "Noch kein individueller Nachkauf-Indikator hinterlegt.", None, None),
        reference("Morgan Stanley", "MS", US_BANKS, None, "Noch kein individueller Nachkauf-Indikator hinterlegt.", None, None),
        reference("Aflac", "AFL", US_INSURANCE, None, "Noch kein individueller Nachkauf-Indikator hinterlegt.", None, None),
        dict(name="JPMorgan Nasdaq Equity Premium Income ETF", ticker="JEPQ",
             cluster="Income-ETFs", asset_type="etf", fund_kind="equity_income_options",
             fund_inception="2022-05-03", fund_source=ETF_SOURCE,
             hist_pe=None, hist_ps=None, hist_div=None,
             buy_trigger="ETF separat bewerten; kein automatisches Kaufsignal.",
             distribution_yield_label="Ausschüttungsrendite (12M)",
             distribution_yield_method="Ausschüttungen je Anteil der letzten 12 Monate nach Ex-Tag / aktueller Kurs × 100; keine Gesamtrendite.",
             initial={}),
    ]),
]


# Primary-source references for A24. Values are retrospective fiscal-year-end
# observations with disclosed exclusions, never a fabricated daily P/E series.
CLASSIC_REFERENCES = [{'name': 'Adidas',
  'ticker': 'ADS.DE',
  'cluster': 'Konsum',
  'isin': 'DE000A1EWWW0',
  'share_class': 'Registered ordinary share; German listing; no ADS conversion',
  'reference_set': 'classic-2026',
  'hist_pe': 44.9,
  'hist_div': 1.183082,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 20x und RSI (14) ≤ 45',
  'buy_trigger_rationale': 'Vorschlag: künftige Marken- und Margenerholung über Forward P/E bewerten; ein Rücksetzer beim RSI soll den Einstieg '
                           'disziplinieren. Die Schwellen sind selbst gewählt, kein bewiesenes Optimum. Eigener Vorschlag, kein nachgewiesenes '
                           'Optimum und nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.adidas-group.com/en/media/press-releases/adidas-achieves-record-results-in-the-third-quarter-of-2026-and-increases-full-year-profit-outlook',
                           'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '5J-Median · n=4',
                                 'source_label': 'IFRS · IR / Kurse',
                                 'accounting_basis': 'IFRS',
                                 'currency': 'EUR',
                                 'share_basis': 'Registered ordinary share; German listing; no ADS conversion',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 4,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'diluted_eps': 7.47,
                                                   'close': 253.2,
                                                   'pe': 33.9,
                                                   'pe_is_published': True,
                                                   'dividend_per_share': 3.3,
                                                   'source_url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                                                   'price_source_url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                                                   'price_date': '2021-12-30',
                                                   'eps_source_url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'diluted_eps': 1.25,
                                                   'close': 127.46,
                                                   'pe': 102.4,
                                                   'pe_is_published': True,
                                                   'dividend_per_share': 0.7,
                                                   'source_url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                                                   'price_source_url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                                                   'price_date': '2022-12-30',
                                                   'eps_source_url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'diluted_eps': -0.67,
                                                   'close': 184.16,
                                                   'pe': None,
                                                   'pe_is_published': True,
                                                   'dividend_per_share': 0.7,
                                                   'source_url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                                                   'price_source_url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                                                   'price_date': '2023-12-29',
                                                   'eps_source_url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                                                   'currency': 'EUR',
                                                   'exclusion_reason': 'Berichtetes EPS ist nicht positiv; kein sinnvoll positives KGV.'},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'diluted_eps': 4.24,
                                                   'close': 236.8,
                                                   'pe': 55.9,
                                                   'pe_is_published': True,
                                                   'dividend_per_share': 2,
                                                   'source_url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                                                   'price_source_url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                                                   'price_date': '2024-12-30',
                                                   'eps_source_url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'diluted_eps': 7.46,
                                                   'close': 169.05,
                                                   'pe': 22.7,
                                                   'pe_is_published': True,
                                                   'dividend_per_share': 2.8,
                                                   'source_url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                                                   'price_source_url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                                                   'price_date': '2025-12-30',
                                                   'eps_source_url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                                                   'currency': 'EUR'}],
                                 'method': 'Median der direkt vom Emittenten veröffentlichten positiven Jahresend-KGVs FY2021–FY2025. Gerundete EPS '
                                           'dienen nur zur Dokumentation, nicht zur Neuberechnung dieser veröffentlichten Verhältnisse. 4 positive '
                                           'Beobachtungen aus 5 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. IFRS reported diluted EPS, exact listed share class; no adjusted earnings. Registered '
                                           'ordinary share; German listing; no ADS conversion. Kurse ohne Dividendenbereinigung. Wenn Yahoo '
                                           'nachträglich splitbereinigte Kurse liefert, werden diese anhand der nachfolgenden Splits auf die '
                                           'veröffentlichte EPS-Aktienbasis zurückgeführt. Nachträglich rekonstruierte Jahresend-Stichprobe, kein '
                                           'Durchschnitt täglicher TTM-KGVs und keine rückgetestete Strategie.',
                                 'source_url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                                 'price_source_url': 'https://report.adidas-group.com/2025/en/additional-information/ten-year-overview.html',
                                 'comparable_to_current': True,
                                 'comparison_note': '2021/2022 beziehen sich auf fortgeführte Aktivitäten nach dem Reebok-Verkauf. 2023 war ein '
                                                    'Verlustjahr und bleibt außerhalb des positiven KGV-Medians; 2022 erzeugen geringe Gewinne ein '
                                                    'KGV von 102,4. Verwendet werden die direkt veröffentlichten KGVs, keine Neuberechnung aus '
                                                    'gerundeten EPS. Die Referenz ist kein Fair-Value-Ziel.',
                                 'excluded_years': [{'fiscal_year': 2023,
                                                     'reason': 'Berichtetes EPS ist nicht positiv; kein sinnvoll positives KGV.'}]},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'EUR',
                                  'share_basis': 'Registered ordinary share; German listing; no ADS conversion',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:00+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-30',
                                                    'close': 253.199997,
                                                    'dividends_per_share': 3.0,
                                                    'dividend_yield': 1.184834138,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/ADS.DE/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 127.459999,
                                                    'dividends_per_share': 3.3,
                                                    'dividend_yield': 2.589047563,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/ADS.DE/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 184.160004,
                                                    'dividends_per_share': 0.7,
                                                    'dividend_yield': 0.38010425,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/ADS.DE/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-30',
                                                    'close': 236.800003,
                                                    'dividends_per_share': 0.7,
                                                    'dividend_yield': 0.295608104,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/ADS.DE/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-30',
                                                    'close': 169.050003,
                                                    'dividends_per_share': 2.0,
                                                    'dividend_yield': 1.183081907,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/ADS.DE/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/ADS.DE/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://www.adidas-group.com/en/media/press-releases/adidas-achieves-record-results-in-the-third-quarter-of-2026-and-increases-full-year-profit-outlook',
                       'as_of': '2026-10-29',
                       'facts': {'nine_month_basic_and_diluted_eps_continuing_operations': 8.85,
                                 'fy2026_operating_profit_guidance_eur_billion': 2.5}}},
 {'name': 'Munich Re',
  'ticker': 'MUV2.DE',
  'cluster': 'Versicherungen',
  'isin': 'DE0008430026',
  'share_class': 'Xetra registeredordinaryshares; nootherclassorADSconversion',
  'reference_set': 'classic-2026',
  'hist_pe': 11.346378,
  'hist_div': 3.557453,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 11x oder Dividendenrendite ≥ 4,5%',
  'buy_trigger_rationale': 'Rückversicherer benötigen einen Puffer für Großschäden und Reservierungszyklen. Höchstens 11x Forward-KGV liegt knapp '
                           'unter der jüngeren IFRS-9/17-Jahresend-Referenz; 4,5% Dividendenrendite verlangt bei 24 EUR Ausschüttung einen '
                           'Preisabschlag. Eigener Vorschlag, nicht rückgetestet. Eigener Vorschlag, kein nachgewiesenes Optimum und nicht '
                           'rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://www.munichre.com/content/dam/munichre/mrwebsiteslaunches/2025-annual-report/MunichRe-Group-Annual-Report-2025-en.pdf/_jcr_content/renditions/original./MunichRe-Group-Annual-Report-2025-en.pdf#page=2',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.munichre.com/en/company/media-relations/media-information-and-corporate-news/media-information/2026/media-release-2026-10-23.html',
                           'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '3J-Median · JE',
                                 'source_label': 'IFRS 9/17 · IR / Kurse',
                                 'accounting_basis': 'IFRS 9/17',
                                 'currency': 'EUR',
                                 'share_basis': 'Xetra registeredordinaryshares; nootherclassorADSconversion',
                                 'period': 'FY2023–FY2025',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 3,
                                 'window_count': 3,
                                 'observations': [{'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'diluted_eps': 33.88,
                                                   'close': 375.1,
                                                   'dividend_per_share': 15.0,
                                                   'source_url': 'https://www.munichre.com/content/dam/munichre/mrwebsiteslaunches/2025-annual-report/MunichRe-Group-Annual-Report-2025-en.pdf/_jcr_content/renditions/original./MunichRe-Group-Annual-Report-2025-en.pdf#page=2',
                                                   'source_location': 'Insidefrontcover/PDFp2 keyfigures; EPSNote66 printedp373–374/PDFp375–376',
                                                   'accounting_basis': 'IFRS9/17',
                                                   'pe': 11.071428571,
                                                   'dividend_reference_yield_pct': 3.99893362,
                                                   'price_source_url': 'https://www.munichre.com/content/dam/munichre/mrwebsiteslaunches/2025-annual-report/MunichRe-Group-Annual-Report-2025-en.pdf/_jcr_content/renditions/original./MunichRe-Group-Annual-Report-2025-en.pdf#page=2',
                                                   'price_date': '2023-12-29',
                                                   'eps_source_url': 'https://www.munichre.com/content/dam/munichre/mrwebsiteslaunches/2025-annual-report/MunichRe-Group-Annual-Report-2025-en.pdf/_jcr_content/renditions/original./MunichRe-Group-Annual-Report-2025-en.pdf#page=2',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'diluted_eps': 42.93,
                                                   'close': 487.1,
                                                   'dividend_per_share': 20.0,
                                                   'source_url': 'https://www.munichre.com/content/dam/munichre/mrwebsiteslaunches/2025-annual-report/MunichRe-Group-Annual-Report-2025-en.pdf/_jcr_content/renditions/original./MunichRe-Group-Annual-Report-2025-en.pdf#page=2',
                                                   'source_location': 'Insidefrontcover/PDFp2; no dilution perNote66',
                                                   'accounting_basis': 'IFRS9/17',
                                                   'pe': 11.346377824,
                                                   'dividend_reference_yield_pct': 4.10593307,
                                                   'price_source_url': 'https://www.munichre.com/content/dam/munichre/mrwebsiteslaunches/2025-annual-report/MunichRe-Group-Annual-Report-2025-en.pdf/_jcr_content/renditions/original./MunichRe-Group-Annual-Report-2025-en.pdf#page=2',
                                                   'price_date': '2024-12-30',
                                                   'eps_source_url': 'https://www.munichre.com/content/dam/munichre/mrwebsiteslaunches/2025-annual-report/MunichRe-Group-Annual-Report-2025-en.pdf/_jcr_content/renditions/original./MunichRe-Group-Annual-Report-2025-en.pdf#page=2',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'diluted_eps': 47.15,
                                                   'close': 562.2,
                                                   'dividend_per_share': 24.0,
                                                   'source_url': 'https://www.munichre.com/content/dam/munichre/mrwebsiteslaunches/2025-annual-report/MunichRe-Group-Annual-Report-2025-en.pdf/_jcr_content/renditions/original./MunichRe-Group-Annual-Report-2025-en.pdf#page=2',
                                                   'source_location': 'Insidefrontcover/PDFp2; no dilution perNote66',
                                                   'accounting_basis': 'IFRS9/17',
                                                   'pe': 11.923647932,
                                                   'dividend_reference_yield_pct': 4.26894344,
                                                   'price_source_url': 'https://www.munichre.com/content/dam/munichre/mrwebsiteslaunches/2025-annual-report/MunichRe-Group-Annual-Report-2025-en.pdf/_jcr_content/renditions/original./MunichRe-Group-Annual-Report-2025-en.pdf#page=2',
                                                   'price_date': '2025-12-30',
                                                   'eps_source_url': 'https://www.munichre.com/content/dam/munichre/mrwebsiteslaunches/2025-annual-report/MunichRe-Group-Annual-Report-2025-en.pdf/_jcr_content/renditions/original./MunichRe-Group-Annual-Report-2025-en.pdf#page=2',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False}],
                                 'method': 'Median der positiven Geschäftsjahresend-KGVs FY2023–FY2025: letzter Schlusskurs am oder vor dem '
                                           'jeweiligen Geschäftsjahresende ÷ veröffentlichtes Jahres-EPS derselben Aktie in derselben Währung. 3 '
                                           'positive Beobachtungen aus 3 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. Berichtetes IFRS-Ergebnis je Aktie; laut Note 66 keine Verwässerungseffekte in '
                                           'FY2025/FY2024 und laut AR2023 auch nicht in FY2023/FY2022.. Xetra registeredordinaryshares; '
                                           'nootherclassorADSconversion. Kurse ohne Dividendenbereinigung. Wenn Yahoo nachträglich splitbereinigte '
                                           'Kurse liefert, werden diese anhand der nachfolgenden Splits auf die veröffentlichte EPS-Aktienbasis '
                                           'zurückgeführt. Nachträglich rekonstruierte Jahresend-Stichprobe, kein Durchschnitt täglicher TTM-KGVs '
                                           'und keine rückgetestete Strategie.',
                                 'source_url': 'https://www.munichre.com/content/dam/munichre/mrwebsiteslaunches/2025-annual-report/MunichRe-Group-Annual-Report-2025-en.pdf/_jcr_content/renditions/original./MunichRe-Group-Annual-Report-2025-en.pdf#page=2',
                                 'price_source_url': 'https://www.munichre.com/content/dam/munichre/mrwebsiteslaunches/2025-annual-report/MunichRe-Group-Annual-Report-2025-en.pdf/_jcr_content/renditions/original./MunichRe-Group-Annual-Report-2025-en.pdf#page=2',
                                 'comparable_to_current': True,
                                 'comparison_note': 'Drei vollständig auf IFRS-9/17-Basis berichtete Jahre. 2022 war ein Übergangsjahr mit IFRS 17 '
                                                    'und überwiegend IAS 39; 2021 wurde nicht entsprechend angepasst. Deshalb keine vermeintlich '
                                                    'homogene Fünfjahresreihe.',
                                 'excluded_years': []},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'EUR',
                                  'share_basis': 'Xetra registeredordinaryshares; nootherclassorADSconversion',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:00+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-30',
                                                    'close': 260.5,
                                                    'dividends_per_share': 9.8,
                                                    'dividend_yield': 3.761996161,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/MUV2.DE/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 304.0,
                                                    'dividends_per_share': 11.0,
                                                    'dividend_yield': 3.618421053,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/MUV2.DE/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 375.100006,
                                                    'dividends_per_share': 11.6,
                                                    'dividend_yield': 3.092508614,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/MUV2.DE/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-30',
                                                    'close': 487.100006,
                                                    'dividends_per_share': 15.0,
                                                    'dividend_yield': 3.079449766,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/MUV2.DE/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-30',
                                                    'close': 562.200012,
                                                    'dividends_per_share': 20.0,
                                                    'dividend_yield': 3.557452787,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/MUV2.DE/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/MUV2.DE/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://www.munichre.com/en/company/media-relations/media-information-and-corporate-news/media-information/2026/media-release-2026-10-23.html',
                       'as_of': '2026-10-23',
                       'facts': 'Vorläufiges Q3 2026: Quartalsgewinn 1,7 Mrd. EUR; 9M-Ergebnis 5,7 Mrd. EUR; Jahresgewinnprognose von 6,3 auf 6,6 '
                                'Mrd. EUR erhöht.'}},
 {'name': 'Microsoft',
  'ticker': 'MSFT',
  'cluster': 'Technologie',
  'isin': 'US5949181045',
  'share_class': 'NASDAQ-Stammaktie, Nennwert 0,00000625 USD; keine ADS-Umrechnung; letzter Split 2003 vor dem Beobachtungszeitraum.',
  'reference_set': 'classic-2026',
  'hist_pe': 35.179753,
  'hist_div': 0.781112,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E 23x–24x oder Test Tages-SMA 200',
  'buy_trigger_rationale': 'Übernimmt den bisherigen kurzen Microsoft-Stil. Wiederkehrende Cloud-/Softwareerlöse stehen hohen AI-Investitionen und '
                           'volatilen Beteiligungsergebnissen gegenüber. 23x–24x Forward-KGV ist ein eigener disziplinierter Einstiegsvorschlag; die '
                           'SMA-200-Alternative beschreibt einen Rücksetzer. Keine optimale oder rückgetestete Schwelle. Eigener Vorschlag, kein '
                           'nachgewiesenes Optimum und nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://www.microsoft.com/investor/reports/ar23/', 'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.microsoft.com/investor/reports/ar26/', 'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.microsoft.com/en-us/investor/earnings/fy-2027-q1/press-release-webcast/',
                           'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '5J-Median · JE',
                                 'source_label': 'US-GAAP · IR / Kurse',
                                 'accounting_basis': 'US-GAAP',
                                 'currency': 'USD',
                                 'share_basis': 'NASDAQ-Stammaktie, Nennwert 0,00000625 USD; keine ADS-Umrechnung; letzter Split 2003 vor dem '
                                                'Beobachtungszeitraum.',
                                 'period': 'FY2022–FY2026',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2022,
                                                   'fiscal_end': '2022-06-30',
                                                   'diluted_eps': 9.65,
                                                   'dividend_per_share': 2.48,
                                                   'source_url': 'https://www.microsoft.com/investor/reports/ar23/',
                                                   'source_location': 'IncomeStatements/Note2EPS andStatementsofStockholdersEquity',
                                                   'close': 256.829987,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/MSFT/history/',
                                                   'split_adjusted_close': 256.829987,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2022-06-30',
                                                   'eps_source_url': 'https://www.microsoft.com/investor/reports/ar23/',
                                                   'currency': 'USD',
                                                   'pe_is_published': False,
                                                   'pe': 26.614506425},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-06-30',
                                                   'diluted_eps': 9.68,
                                                   'dividend_per_share': 2.72,
                                                   'source_url': 'https://www.microsoft.com/investor/reports/ar23/',
                                                   'source_location': 'IncomeStatements/Note2EPS andStatementsofStockholdersEquity',
                                                   'close': 340.540009,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/MSFT/history/',
                                                   'split_adjusted_close': 340.540009,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2023-06-30',
                                                   'eps_source_url': 'https://www.microsoft.com/investor/reports/ar23/',
                                                   'currency': 'USD',
                                                   'pe_is_published': False,
                                                   'pe': 35.179752996},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-06-30',
                                                   'diluted_eps': 11.8,
                                                   'dividend_per_share': 3.0,
                                                   'source_url': 'https://www.microsoft.com/investor/reports/ar26/',
                                                   'source_location': 'IncomeStatements;Note2EPS;StatementsofStockholdersEquity',
                                                   'close': 446.950012,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/MSFT/history/',
                                                   'split_adjusted_close': 446.950012,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2024-06-28',
                                                   'eps_source_url': 'https://www.microsoft.com/investor/reports/ar26/',
                                                   'currency': 'USD',
                                                   'pe_is_published': False,
                                                   'pe': 37.877119661},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-06-30',
                                                   'diluted_eps': 13.64,
                                                   'dividend_per_share': 3.32,
                                                   'source_url': 'https://www.microsoft.com/investor/reports/ar26/',
                                                   'source_location': 'IncomeStatements;Note2EPS;StatementsofStockholdersEquity',
                                                   'close': 497.410004,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/MSFT/history/',
                                                   'split_adjusted_close': 497.410004,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2025-06-30',
                                                   'eps_source_url': 'https://www.microsoft.com/investor/reports/ar26/',
                                                   'currency': 'USD',
                                                   'pe_is_published': False,
                                                   'pe': 36.467009091},
                                                  {'fiscal_year': 2026,
                                                   'fiscal_end': '2026-06-30',
                                                   'diluted_eps': 17.95,
                                                   'dividend_per_share': 3.64,
                                                   'source_url': 'https://www.microsoft.com/investor/reports/ar26/',
                                                   'source_location': 'IncomeStatements;Note2EPS;StatementsofStockholdersEquity',
                                                   'close': 373.019989,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/MSFT/history/',
                                                   'split_adjusted_close': 373.019989,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2026-06-30',
                                                   'eps_source_url': 'https://www.microsoft.com/investor/reports/ar26/',
                                                   'currency': 'USD',
                                                   'pe_is_published': False,
                                                   'pe': 20.781057883}],
                                 'method': 'Median der positiven Geschäftsjahresend-KGVs FY2022–FY2026: letzter Schlusskurs am oder vor dem '
                                           'jeweiligen Geschäftsjahresende ÷ veröffentlichtes Jahres-EPS derselben Aktie in derselben Währung. 5 '
                                           'positive Beobachtungen aus 5 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. Berichtetes verwässertes US-GAAP-EPS; Gewinne und Verluste aus der OpenAI-Beteiligung '
                                           'bleiben enthalten.. NASDAQ-Stammaktie, Nennwert 0,00000625 USD; keine ADS-Umrechnung; letzter Split 2003 '
                                           'vor dem Beobachtungszeitraum.. Kurse ohne Dividendenbereinigung. Wenn Yahoo nachträglich splitbereinigte '
                                           'Kurse liefert, werden diese anhand der nachfolgenden Splits auf die veröffentlichte EPS-Aktienbasis '
                                           'zurückgeführt. Nachträglich rekonstruierte Jahresend-Stichprobe, kein Durchschnitt täglicher TTM-KGVs '
                                           'und keine rückgetestete Strategie.',
                                 'source_url': 'https://www.microsoft.com/investor/reports/ar26/',
                                 'price_source_url': 'https://finance.yahoo.com/quote/MSFT/history/',
                                 'comparable_to_current': True,
                                 'comparison_note': 'Schlusskurse passend zum Geschäftsjahresende im Juni. Berichtete US-GAAP-Gewinne einschließlich '
                                                    'positiver und negativer Effekte aus OpenAI-Anlagen; keine Vermischung mit bereinigtem EPS. '
                                                    'Jahresend-KGV und aktuelles TTM-KGV haben unterschiedliche Gewinnzeiträume.',
                                 'excluded_years': []},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'USD',
                                  'share_basis': 'NASDAQ-Stammaktie, Nennwert 0,00000625 USD; keine ADS-Umrechnung; letzter Split 2003 vor dem '
                                                 'Beobachtungszeitraum.',
                                  'period': 'FY2022–FY2026',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:00+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2022,
                                                    'price_date': '2022-06-30',
                                                    'close': 256.829987,
                                                    'dividends_per_share': 2.42,
                                                    'dividend_yield': 0.942257574,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/MSFT/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-06-30',
                                                    'close': 340.540009,
                                                    'dividends_per_share': 2.66,
                                                    'dividend_yield': 0.781112331,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/MSFT/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-06-28',
                                                    'close': 446.950012,
                                                    'dividends_per_share': 2.93,
                                                    'dividend_yield': 0.655554295,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/MSFT/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-06-30',
                                                    'close': 497.410004,
                                                    'dividends_per_share': 3.24,
                                                    'dividend_yield': 0.651374113,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/MSFT/history/'},
                                                   {'fiscal_year': 2026,
                                                    'price_date': '2026-06-30',
                                                    'close': 373.019989,
                                                    'dividends_per_share': 3.56,
                                                    'dividend_yield': 0.954372448,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/MSFT/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/MSFT/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://www.microsoft.com/en-us/investor/earnings/fy-2027-q1/press-release-webcast/',
                       'as_of': '2026-10-28',
                       'facts': 'FY2027 Q1: Umsatz 92,279 Mrd. USD (+19%), berichtetes verwässertes EPS 4,92 USD, bereinigtes EPS 4,89 USD, '
                                'Azure-Umsatz +48%. OpenAI-Gewinn erhöhte berichtetes EPS um 0,03 USD. Primärdaten belegen Cloud-Wachstum und die '
                                'Notwendigkeit der Trennung von GAAP und bereinigten Werten.'}},
 {'name': 'Allianz',
  'ticker': 'ALV.DE',
  'cluster': 'Versicherungen',
  'isin': 'DE0008404005',
  'share_class': 'Xetra registered ordinary shares, ISINDE0008404005',
  'reference_set': 'classic-2026',
  'hist_pe': 12.364713,
  'hist_div': 4.663738,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 11,5x oder Dividendenrendite ≥ 5,5%',
  'buy_trigger_rationale': 'Übernimmt den bisherigen kurzen Allianz-Stil. 11,5x liegt unter dem rekonstruierten IFRS-9/17-Jahresend-Median von rund '
                           '12,36x; 5,5% Dividendenrendite verlangt einen größeren Preiseinstiegspuffer. Eigener Vorschlag, keine optimale oder '
                           'rückgetestete Strategie. Eigener Vorschlag, kein nachgewiesenes Optimum und nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://www.allianz.com/en/investor_relations/results-reports/key-figures.html',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.allianz.com/en/mediacenter/news/media-releases/financials/261112-3q-2026-earnings-release.html',
                           'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '4J-Median · JE',
                                 'source_label': 'IFRS 9/17 · IR / Kurse',
                                 'accounting_basis': 'IFRS 9/17',
                                 'currency': 'EUR',
                                 'share_basis': 'Xetra registered ordinary shares, ISINDE0008404005',
                                 'period': 'FY2022–FY2025',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 4,
                                 'window_count': 4,
                                 'observations': [{'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'diluted_eps': 15.48,
                                                   'basic_eps': 15.57,
                                                   'close': 200.9,
                                                   'dividend_per_share': 11.4,
                                                   'source_url': 'https://www.allianz.com/en/investor_relations/results-reports/key-figures.html',
                                                   'price_source_url': 'https://www.allianz.com/en/investor_relations/share/key-indicators.html',
                                                   'accounting_basis': 'IFRS9/17 restated',
                                                   'pe': 12.978036176,
                                                   'dividend_reference_yield_pct': 5.67446491,
                                                   'price_date': '2022-12-30',
                                                   'eps_source_url': 'https://www.allianz.com/en/investor_relations/results-reports/key-figures.html',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'diluted_eps': 21.18,
                                                   'basic_eps': 21.2,
                                                   'close': 241.95,
                                                   'dividend_per_share': 13.8,
                                                   'source_url': 'https://www.allianz.com/en/investor_relations/results-reports/key-figures.html',
                                                   'price_source_url': 'https://www.allianz.com/en/investor_relations/share/key-indicators.html',
                                                   'accounting_basis': 'IFRS9/17',
                                                   'pe': 11.423512748,
                                                   'dividend_reference_yield_pct': 5.70365778,
                                                   'price_date': '2023-12-29',
                                                   'eps_source_url': 'https://www.allianz.com/en/investor_relations/results-reports/key-figures.html',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'diluted_eps': 25.18,
                                                   'basic_eps': 25.2,
                                                   'close': 295.9,
                                                   'dividend_per_share': 15.4,
                                                   'source_url': 'https://www.allianz.com/en/investor_relations/results-reports/key-figures.html',
                                                   'price_source_url': 'https://www.allianz.com/en/investor_relations/share/key-indicators.html',
                                                   'accounting_basis': 'IFRS9/17',
                                                   'pe': 11.751389992,
                                                   'dividend_reference_yield_pct': 5.20446097,
                                                   'price_date': '2024-12-30',
                                                   'eps_source_url': 'https://www.allianz.com/en/investor_relations/results-reports/key-figures.html',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'diluted_eps': 27.67,
                                                   'basic_eps': 27.69,
                                                   'close': 390.5,
                                                   'dividend_per_share': 17.1,
                                                   'source_url': 'https://www.allianz.com/en/investor_relations/results-reports/key-figures.html',
                                                   'price_source_url': 'https://www.allianz.com/en/investor_relations/share/key-indicators.html',
                                                   'accounting_basis': 'IFRS9/17',
                                                   'pe': 14.112757499,
                                                   'dividend_reference_yield_pct': 4.37900128,
                                                   'price_date': '2025-12-30',
                                                   'eps_source_url': 'https://www.allianz.com/en/investor_relations/results-reports/key-figures.html',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False}],
                                 'method': 'Median der positiven Geschäftsjahresend-KGVs FY2022–FY2025: letzter Schlusskurs am oder vor dem '
                                           'jeweiligen Geschäftsjahresende ÷ veröffentlichtes Jahres-EPS derselben Aktie in derselben Währung. 4 '
                                           'positive Beobachtungen aus 4 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. Verwässertes EPS nach IFRS 9/IFRS 17; FY2022 auf diese Basis rückwirkend angepasst.. '
                                           'Xetra registered ordinary shares, ISINDE0008404005. Kurse ohne Dividendenbereinigung. Wenn Yahoo '
                                           'nachträglich splitbereinigte Kurse liefert, werden diese anhand der nachfolgenden Splits auf die '
                                           'veröffentlichte EPS-Aktienbasis zurückgeführt. Nachträglich rekonstruierte Jahresend-Stichprobe, kein '
                                           'Durchschnitt täglicher TTM-KGVs und keine rückgetestete Strategie.',
                                 'source_url': 'https://www.allianz.com/en/investor_relations/results-reports/key-figures.html',
                                 'price_source_url': 'https://www.allianz.com/en/investor_relations/share/key-indicators.html',
                                 'comparable_to_current': True,
                                 'comparison_note': 'Vier vergleichbare Jahre auf IFRS-9/17-Basis, einschließlich rückwirkend angepasstem 2022. 2021 '
                                                    'ist ausgeschlossen. Das ältere vom Unternehmen veröffentlichte KGV 2022 beruht noch auf IFRS 4 '
                                                    'und wird nicht in diese neue Reihe gemischt.',
                                 'excluded_years': []},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'EUR',
                                  'share_basis': 'Xetra registered ordinary shares, ISINDE0008404005',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:00+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-30',
                                                    'close': 207.649994,
                                                    'dividends_per_share': 9.6,
                                                    'dividend_yield': 4.623164114,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/ALV.DE/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 200.899994,
                                                    'dividends_per_share': 10.8,
                                                    'dividend_yield': 5.375809023,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/ALV.DE/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 241.949997,
                                                    'dividends_per_share': 11.4,
                                                    'dividend_yield': 4.711717356,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/ALV.DE/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-30',
                                                    'close': 295.899994,
                                                    'dividends_per_share': 13.8,
                                                    'dividend_yield': 4.663737845,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/ALV.DE/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-30',
                                                    'close': 390.5,
                                                    'dividends_per_share': 15.4,
                                                    'dividend_yield': 3.943661972,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/ALV.DE/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/ALV.DE/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://www.allianz.com/en/mediacenter/news/media-releases/financials/261112-3q-2026-earnings-release.html',
                       'as_of': '2026-11-12',
                       'facts': '9M 2026: operatives Ergebnis 14,1 Mrd. EUR, Core-EPS 24,15 EUR und Solvency-II-Quote 219%. Oberes Ende der '
                                'operativen Jahresgewinnprognose auf rund 18,6 Mrd. EUR angehoben.'}},
 {'name': 'Volkswagen (Vorz.)',
  'ticker': 'VOW3.DE',
  'cluster': 'Automobil & Nutzfahrzeuge',
  'isin': 'DE0007664039',
  'share_class': 'Non-voting preference share; NOT ordinary VOW.DE',
  'reference_set': 'classic-2026',
  'hist_pe': 4.2,
  'hist_div': 7.83542,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 6x und RSI (14) ≤ 40',
  'buy_trigger_rationale': 'Vorschlag: für den zyklischen Konzern mit Umbau- und Porsche-Risiken einen niedrigen Gewinnmultiplikator und einen '
                           'ausgeprägteren Rücksetzer verlangen; kein rückgetestetes Optimum. Eigener Vorschlag, kein nachgewiesenes Optimum und '
                           'nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.volkswagen-group.com/en/press-releases/volkswagen-group-nine-month-results-underline-urgent-need-for-action-20799',
                           'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '5J-Median · JE',
                                 'source_label': 'IFRS · IR / Kurse',
                                 'accounting_basis': 'IFRS',
                                 'currency': 'EUR',
                                 'share_basis': 'Non-voting preference share; NOT ordinary VOW.DE',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'diluted_eps': 29.65,
                                                   'close': 177.48,
                                                   'pe': 6,
                                                   'pe_is_published': True,
                                                   'dividend_per_share': 7.56,
                                                   'source_url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                                                   'price_source_url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                                                   'price_date': '2021-12-30',
                                                   'eps_source_url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'diluted_eps': 29.72,
                                                   'close': 116.42,
                                                   'pe': 3.9,
                                                   'pe_is_published': True,
                                                   'dividend_per_share': 8.76,
                                                   'source_url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                                                   'price_source_url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                                                   'price_date': '2022-12-30',
                                                   'eps_source_url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'diluted_eps': 31.85,
                                                   'close': 111.8,
                                                   'pe': 3.5,
                                                   'pe_is_published': True,
                                                   'dividend_per_share': 9.06,
                                                   'source_url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                                                   'price_source_url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                                                   'price_date': '2023-12-29',
                                                   'eps_source_url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'diluted_eps': 21.42,
                                                   'close': 89.04,
                                                   'pe': 4.2,
                                                   'pe_is_published': True,
                                                   'dividend_per_share': 6.36,
                                                   'source_url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                                                   'price_source_url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                                                   'price_date': '2024-12-30',
                                                   'eps_source_url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'diluted_eps': 13.35,
                                                   'close': 103.55,
                                                   'pe': 7.8,
                                                   'pe_is_published': True,
                                                   'dividend_per_share': 5.26,
                                                   'source_url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                                                   'price_source_url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                                                   'price_date': '2025-12-30',
                                                   'eps_source_url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                                                   'currency': 'EUR'}],
                                 'method': 'Median der direkt vom Emittenten veröffentlichten positiven Jahresend-KGVs FY2021–FY2025. Gerundete EPS '
                                           'dienen nur zur Dokumentation, nicht zur Neuberechnung dieser veröffentlichten Verhältnisse. 5 positive '
                                           'Beobachtungen aus 5 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. IFRS reported diluted EPS, exact listed share class; no adjusted earnings. Non-voting '
                                           'preference share; NOT ordinary VOW.DE. Kurse ohne Dividendenbereinigung. Wenn Yahoo nachträglich '
                                           'splitbereinigte Kurse liefert, werden diese anhand der nachfolgenden Splits auf die veröffentlichte '
                                           'EPS-Aktienbasis zurückgeführt. Nachträglich rekonstruierte Jahresend-Stichprobe, kein Durchschnitt '
                                           'täglicher TTM-KGVs und keine rückgetestete Strategie.',
                                 'source_url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                                 'price_source_url': 'https://annualreport2025.volkswagen-group.com/group-management-report/shares-and-bonds/share-key-figures.html',
                                 'comparable_to_current': True,
                                 'comparison_note': 'Ausschließlich Vorzugsaktien und deren berichtete EPS; die Stammaktienkurse werden nicht '
                                                    'verwendet. Rückwirkend angepasste EPS 2022/2023 berücksichtigt. 2026 beeinflussen Sondereffekte '
                                                    'und die Automobilkonjunktur die Gewinne. Ein niedriges KGV allein belegt keine Unterbewertung.',
                                 'excluded_years': []},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'EUR',
                                  'share_basis': 'Non-voting preference share; NOT ordinary VOW.DE',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:02+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-30',
                                                    'close': 177.479996,
                                                    'dividends_per_share': 4.86,
                                                    'dividend_yield': 2.73833678,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/VOW3.DE/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 116.419998,
                                                    'dividends_per_share': 26.62,
                                                    'dividend_yield': 22.865487389,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/VOW3.DE/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 111.800003,
                                                    'dividends_per_share': 8.76,
                                                    'dividend_yield': 7.83542018,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/VOW3.DE/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-30',
                                                    'close': 89.040001,
                                                    'dividends_per_share': 9.06,
                                                    'dividend_yield': 10.175202052,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/VOW3.DE/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-30',
                                                    'close': 103.550003,
                                                    'dividends_per_share': 6.36,
                                                    'dividend_yield': 6.141960225,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/VOW3.DE/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/VOW3.DE/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://www.volkswagen-group.com/en/press-releases/volkswagen-group-nine-month-results-underline-urgent-need-for-action-20799',
                       'as_of': '2026-10-29',
                       'facts': {'nine_month_revenue_eur_billion': 233.4,
                                 'nine_month_operating_profit_eur_billion': 0.1,
                                 'fy2026_return_on_sales_guidance_max_percent': 1.0}}},
 {'name': 'Johnson & Johnson',
  'ticker': 'JNJ',
  'cluster': 'Gesundheit',
  'isin': 'US4781601046',
  'share_class': 'NYSE common stock, USD1 par; no ADS conversion',
  'reference_set': 'classic-2026',
  'hist_pe': 21.90397,
  'hist_div': 2.519106,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 16x und Dividendenrendite ≥ 3,0%',
  'buy_trigger_rationale': 'Forward-KGV und Ausschüttungsrendite sind für das Pharma-/MedTech-Geschäft zweckmäßiger als das durch Talc-Sondereffekte '
                           'verzerrte TTM-KGV. 16x und 3,0% sind ein eigener konservativer Einstiegsvorschlag mit Puffer für Patentabläufe, '
                           'Pipeline- und Rechtsrisiken; keine optimale oder rückgetestete Schwelle. Eigener Vorschlag, kein nachgewiesenes Optimum '
                           'und nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://www.investor.jnj.com/files/doc_financials/2023/q4/form-10-k-2023-final.pdf#page=51',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.jnj.com/download/johnson-johnson-2025-annual-report#page=56',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.jnj.com/media-center/press-releases/johnson-johnson-reports-q3-2026-results-raises-2026-guidance',
                           'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '5J-Median · JE',
                                 'source_label': 'US-GAAP · IR / Kurse',
                                 'accounting_basis': 'US-GAAP',
                                 'currency': 'USD',
                                 'share_basis': 'NYSE common stock, USD1 par; no ADS conversion',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2022-01-02',
                                                   'diluted_eps': 7.81,
                                                   'continuing_diluted_eps': 6.66,
                                                   'dividend_per_share': 4.19,
                                                   'source_url': 'https://www.investor.jnj.com/files/doc_financials/2023/q4/form-10-k-2023-final.pdf#page=51',
                                                   'source_location': '2023 Form10-K printedp44/PDFp51 consolidatedearnings; Note15 '
                                                                      'printedp77/PDFp84 gives EPS and exact FYenddates; equity statement PDFp53 DPS',
                                                   'close': 171.070007,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/JNJ/history/',
                                                   'split_adjusted_close': 171.070007,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2021-12-31',
                                                   'eps_source_url': 'https://www.investor.jnj.com/files/doc_financials/2023/q4/form-10-k-2023-final.pdf#page=51',
                                                   'currency': 'USD',
                                                   'pe_is_published': False,
                                                   'pe': 21.903970166},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2023-01-01',
                                                   'diluted_eps': 6.73,
                                                   'continuing_diluted_eps': 6.14,
                                                   'dividend_per_share': 4.45,
                                                   'source_url': 'https://www.investor.jnj.com/files/doc_financials/2023/q4/form-10-k-2023-final.pdf#page=51',
                                                   'source_location': '2023 Form10-K consolidatedearnings and Note15; DPS Note12 printedp76/PDFp82',
                                                   'close': 176.649994,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/JNJ/history/',
                                                   'split_adjusted_close': 176.649994,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2022-12-30',
                                                   'eps_source_url': 'https://www.investor.jnj.com/files/doc_financials/2023/q4/form-10-k-2023-final.pdf#page=51',
                                                   'currency': 'USD',
                                                   'pe_is_published': False,
                                                   'pe': 26.248141753},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'diluted_eps': 13.72,
                                                   'continuing_diluted_eps': 5.2,
                                                   'dividend_per_share': 4.7,
                                                   'source_url': 'https://www.jnj.com/download/johnson-johnson-2025-annual-report#page=56',
                                                   'source_location': '2025 AR printedp44/PDFp56 consolidatedearnings and printedp80/PDFp92 Note15; '
                                                                      'equity statement PDFp58',
                                                   'close': 156.740005,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/JNJ/history/',
                                                   'split_adjusted_close': 156.740005,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2023-12-29',
                                                   'eps_source_url': 'https://www.jnj.com/download/johnson-johnson-2025-annual-report#page=56',
                                                   'currency': 'USD',
                                                   'pe_is_published': False,
                                                   'pe': 11.424198615},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-29',
                                                   'diluted_eps': 5.79,
                                                   'continuing_diluted_eps': 5.79,
                                                   'dividend_per_share': 4.91,
                                                   'source_url': 'https://www.jnj.com/download/johnson-johnson-2025-annual-report#page=56',
                                                   'source_location': '2025 AR printedp44/PDFp56; Note15 printedp80/PDFp92',
                                                   'close': 145.050003,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/JNJ/history/',
                                                   'split_adjusted_close': 145.050003,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2024-12-27',
                                                   'eps_source_url': 'https://www.jnj.com/download/johnson-johnson-2025-annual-report#page=56',
                                                   'currency': 'USD',
                                                   'pe_is_published': False,
                                                   'pe': 25.05181399},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-28',
                                                   'diluted_eps': 11.03,
                                                   'continuing_diluted_eps': 11.03,
                                                   'dividend_per_share': 5.14,
                                                   'source_url': 'https://www.jnj.com/download/johnson-johnson-2025-annual-report#page=56',
                                                   'source_location': '2025 AR printedp44/PDFp56; Note15 printedp80/PDFp92',
                                                   'close': 207.630005,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/JNJ/history/',
                                                   'split_adjusted_close': 207.630005,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2025-12-26',
                                                   'eps_source_url': 'https://www.jnj.com/download/johnson-johnson-2025-annual-report#page=56',
                                                   'currency': 'USD',
                                                   'pe_is_published': False,
                                                   'pe': 18.8241165}],
                                 'method': 'Median der positiven Geschäftsjahresend-KGVs FY2021–FY2025: letzter Schlusskurs am oder vor dem '
                                           'jeweiligen Geschäftsjahresende ÷ veröffentlichtes Jahres-EPS derselben Aktie in derselben Währung. 5 '
                                           'positive Beobachtungen aus 5 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. US GAAP total diluted net earnings per common share; includes discontinued operations in '
                                           'historical totals. NYSE common stock, USD1 par; no ADS conversion. Kurse ohne Dividendenbereinigung. '
                                           'Wenn Yahoo nachträglich splitbereinigte Kurse liefert, werden diese anhand der nachfolgenden Splits auf '
                                           'die veröffentlichte EPS-Aktienbasis zurückgeführt. Nachträglich rekonstruierte Jahresend-Stichprobe, '
                                           'kein Durchschnitt täglicher TTM-KGVs und keine rückgetestete Strategie.',
                                 'source_url': 'https://www.jnj.com/download/johnson-johnson-2025-annual-report#page=56',
                                 'price_source_url': 'https://finance.yahoo.com/quote/JNJ/history/',
                                 'comparable_to_current': True,
                                 'comparison_note': 'Berichtetes verwässertes US-GAAP-Gesamt-EPS. 2023 enthält den Kenvue-Abspaltungsgewinn; '
                                                    '2025/2026 beeinflussen auch Rechts- und Talc-Sondereffekte die Gewinne. Der Median dämpft '
                                                    'einzelne Ausreißer, beseitigt aber weder Strukturänderungen noch Rechtsrisiken.',
                                 'excluded_years': []},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'USD',
                                  'share_basis': 'NYSE common stock, USD1 par; no ADS conversion',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:02+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-31',
                                                    'close': 171.070007,
                                                    'dividends_per_share': 4.19,
                                                    'dividend_yield': 2.44928966,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/JNJ/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 176.649994,
                                                    'dividends_per_share': 4.45,
                                                    'dividend_yield': 2.519105663,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/JNJ/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 156.740005,
                                                    'dividends_per_share': 4.7,
                                                    'dividend_yield': 2.998596297,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/JNJ/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-27',
                                                    'close': 145.050003,
                                                    'dividends_per_share': 4.91,
                                                    'dividend_yield': 3.38503957,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/JNJ/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-26',
                                                    'close': 207.630005,
                                                    'dividends_per_share': 5.14,
                                                    'dividend_yield': 2.475557424,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/JNJ/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/JNJ/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://www.jnj.com/media-center/press-releases/johnson-johnson-reports-q3-2026-results-raises-2026-guidance',
                       'as_of': '2026-10-13',
                       'facts': 'Q3 2026: berichtetes EPS 0,09 USD, bereinigtes EPS 2,59 USD; neue Talc-Sonderbelastung und M&A-Effekte. Bereinigte '
                                'EPS-Prognose für 2026 auf 11,05–11,15 USD angehoben (Mitte 11,10).'}},
 {'name': 'MTU Aero Engines',
  'ticker': 'MTX.DE',
  'cluster': 'Industrie & Logistik',
  'isin': 'DE000A0D9PT0',
  'share_class': 'Registered ordinary share; official exchange symbol MTX, ISIN DE000A0D9PT0',
  'reference_set': 'classic-2026',
  'hist_pe': 30.646742,
  'hist_div': 0.696767,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 22x und Kurs ≤ SMA 200',
  'buy_trigger_rationale': 'Vorschlag: MRO- und Triebwerkswachstum rechtfertigen mehr als einen klassischen Zykliker-Multiplikator; GTF-Risiken und '
                           'Rückschläge sprechen für Bewertungs- und Trenddisziplin. Schwellen selbst gewählt, nicht rückgetestet. Eigener '
                           'Vorschlag, kein nachgewiesenes Optimum und nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://www.mtu.de/fileadmin/EN/5_Investors/7_Financial_Reports/MTU_AR2022_en_mLinks_locked.pdf',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.mtu.de/fileadmin/DE/5_Investoren/Financial_Report/MTUAeroEnginesAG_AnnualReport_2024_en_locked.pdf',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.mtu.de/fileadmin/DE/5_Investoren/Financial_Report/2025_Results/MTUAeroEnginesAG_AnnualReport_2025_en_locked.pdf',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.mtu.de/fileadmin/DE/5_Investoren/Financial_Report/2026_Results/2026_11_29_9M_2026_Results_en.pdf',
                           'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '5J-Median · n=4',
                                 'source_label': 'IFRS · IR / Kurse',
                                 'accounting_basis': 'IFRS',
                                 'currency': 'EUR',
                                 'share_basis': 'Registered ordinary share; official exchange symbol MTX, ISIN DE000A0D9PT0',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 4,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'diluted_eps': 4.09,
                                                   'close': 179.4,
                                                   'pe': 43.863080685,
                                                   'pe_is_published': False,
                                                   'dividend_per_share': 2.1,
                                                   'source_url': 'https://www.mtu.de/fileadmin/EN/5_Investors/7_Financial_Reports/MTU_AR2022_en_mLinks_locked.pdf',
                                                   'price_source_url': 'https://www.mtu.de/fileadmin/EN/5_Investors/7_Financial_Reports/MTU_AR2022_en_mLinks_locked.pdf',
                                                   'price_date': '2021-12-30',
                                                   'eps_source_url': 'https://www.mtu.de/fileadmin/EN/5_Investors/7_Financial_Reports/MTU_AR2022_en_mLinks_locked.pdf',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'diluted_eps': 6.06,
                                                   'close': 202.2,
                                                   'pe': 33.366336634,
                                                   'pe_is_published': False,
                                                   'dividend_per_share': 3.2,
                                                   'source_url': 'https://www.mtu.de/fileadmin/EN/5_Investors/7_Financial_Reports/MTU_AR2022_en_mLinks_locked.pdf',
                                                   'price_source_url': 'https://www.mtu.de/fileadmin/EN/5_Investors/7_Financial_Reports/MTU_AR2022_en_mLinks_locked.pdf',
                                                   'price_date': '2022-12-30',
                                                   'eps_source_url': 'https://www.mtu.de/fileadmin/EN/5_Investors/7_Financial_Reports/MTU_AR2022_en_mLinks_locked.pdf',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'diluted_eps': -1.9,
                                                   'close': 195.25,
                                                   'pe': None,
                                                   'pe_is_published': False,
                                                   'dividend_per_share': 2,
                                                   'source_url': 'https://www.mtu.de/fileadmin/DE/5_Investoren/Financial_Report/MTUAeroEnginesAG_AnnualReport_2024_en_locked.pdf',
                                                   'price_source_url': 'https://www.mtu.de/fileadmin/DE/5_Investoren/Financial_Report/MTUAeroEnginesAG_AnnualReport_2024_en_locked.pdf',
                                                   'price_date': '2023-12-29',
                                                   'eps_source_url': 'https://www.mtu.de/fileadmin/DE/5_Investoren/Financial_Report/MTUAeroEnginesAG_AnnualReport_2024_en_locked.pdf',
                                                   'currency': 'EUR',
                                                   'exclusion_reason': 'Berichtetes EPS ist nicht positiv; kein sinnvoll positives KGV.'},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'diluted_eps': 11.53,
                                                   'close': 322,
                                                   'pe': 27.927146574,
                                                   'pe_is_published': False,
                                                   'dividend_per_share': 2.2,
                                                   'source_url': 'https://www.mtu.de/fileadmin/DE/5_Investoren/Financial_Report/2025_Results/MTUAeroEnginesAG_AnnualReport_2025_en_locked.pdf',
                                                   'price_source_url': 'https://www.mtu.de/fileadmin/DE/5_Investoren/Financial_Report/2025_Results/MTUAeroEnginesAG_AnnualReport_2025_en_locked.pdf',
                                                   'price_date': '2024-12-30',
                                                   'eps_source_url': 'https://www.mtu.de/fileadmin/DE/5_Investoren/Financial_Report/2025_Results/MTUAeroEnginesAG_AnnualReport_2025_en_locked.pdf',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'diluted_eps': 18.49,
                                                   'close': 355.3,
                                                   'pe': 19.21579232,
                                                   'pe_is_published': False,
                                                   'dividend_per_share': 3.6,
                                                   'source_url': 'https://www.mtu.de/fileadmin/DE/5_Investoren/Financial_Report/2025_Results/MTUAeroEnginesAG_AnnualReport_2025_en_locked.pdf',
                                                   'price_source_url': 'https://www.mtu.de/fileadmin/DE/5_Investoren/Financial_Report/2025_Results/MTUAeroEnginesAG_AnnualReport_2025_en_locked.pdf',
                                                   'price_date': '2025-12-30',
                                                   'eps_source_url': 'https://www.mtu.de/fileadmin/DE/5_Investoren/Financial_Report/2025_Results/MTUAeroEnginesAG_AnnualReport_2025_en_locked.pdf',
                                                   'currency': 'EUR'}],
                                 'method': 'Median der positiven Geschäftsjahresend-KGVs FY2021–FY2025: letzter Schlusskurs am oder vor dem '
                                           'jeweiligen Geschäftsjahresende ÷ veröffentlichtes Jahres-EPS derselben Aktie in derselben Währung. 4 '
                                           'positive Beobachtungen aus 5 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. IFRS reported diluted EPS, exact listed share class; no adjusted earnings. Registered '
                                           'ordinary share; official exchange symbol MTX, ISIN DE000A0D9PT0. Kurse ohne Dividendenbereinigung. Wenn '
                                           'Yahoo nachträglich splitbereinigte Kurse liefert, werden diese anhand der nachfolgenden Splits auf die '
                                           'veröffentlichte EPS-Aktienbasis zurückgeführt. Nachträglich rekonstruierte Jahresend-Stichprobe, kein '
                                           'Durchschnitt täglicher TTM-KGVs und keine rückgetestete Strategie.',
                                 'source_url': 'https://www.mtu.de/fileadmin/DE/5_Investoren/Financial_Report/2025_Results/MTUAeroEnginesAG_AnnualReport_2025_en_locked.pdf',
                                 'price_source_url': 'https://www.mtu.de/fileadmin/DE/5_Investoren/Financial_Report/2025_Results/MTUAeroEnginesAG_AnnualReport_2025_en_locked.pdf',
                                 'comparable_to_current': True,
                                 'comparison_note': '2023 entstand durch das GTF-Flottenmanagement ein Verlust; dieses Jahr bleibt ohne positives '
                                                    'KGV. Durchgehend verwässerte berichtete EPS statt höherer unverwässerter oder bereinigter EPS. '
                                                    'Forward-KGVs können auf bereinigten Analystenschätzungen beruhen.',
                                 'excluded_years': [{'fiscal_year': 2023,
                                                     'reason': 'Berichtetes EPS ist nicht positiv; kein sinnvoll positives KGV.'}]},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'EUR',
                                  'share_basis': 'Registered ordinary share; official exchange symbol MTX, ISIN DE000A0D9PT0',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:42:34+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-30',
                                                    'close': 179.399994,
                                                    'dividends_per_share': 1.25,
                                                    'dividend_yield': 0.696767025,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/MTX.DE/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 202.199997,
                                                    'dividends_per_share': 2.1,
                                                    'dividend_yield': 1.038575683,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/MTX.DE/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 195.25,
                                                    'dividends_per_share': 3.2,
                                                    'dividend_yield': 1.638924456,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/MTX.DE/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-30',
                                                    'close': 322.0,
                                                    'dividends_per_share': 2.0,
                                                    'dividend_yield': 0.621118012,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/MTX.DE/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-30',
                                                    'close': 355.299988,
                                                    'dividends_per_share': 2.2,
                                                    'dividend_yield': 0.619195068,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/MTX.DE/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/MTX.DE/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://www.mtu.de/fileadmin/DE/5_Investoren/Financial_Report/2026_Results/2026_11_29_9M_2026_Results_en.pdf',
                       'as_of': '2026-10-29',
                       'facts': {'nine_month_operating_profit_eur_billion_min': 1.0}}},
 {'name': 'SAP',
  'ticker': 'SAP.DE',
  'cluster': 'Technologie',
  'isin': 'DE0007164600',
  'share_class': 'SAP SE ordinary share, Xetra SAP.DE; EUR per ordinary share, no ADS currency conversion; SAP ADS 1:1 not used for price',
  'reference_set': 'classic-2026',
  'hist_pe': 34.155739,
  'hist_div': 1.469745,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 25x und Kurs ≤ Tages-SMA 200',
  'buy_trigger_rationale': 'Eigener Vorschlag, nicht rückgetestet: SAP meldet starkes Cloud-Wachstum und einen hohen wiederkehrenden Umsatzanteil; '
                           '25x erlaubt eine Qualitätsprämie, kombiniert mit einer technischen Rücksetzerbedingung. Forward-KGV beruht auf '
                           'Konsensschätzungen und ist nicht basisgleich mit historischen IFRS-Sondereffekten. Eigener Vorschlag, kein '
                           'nachgewiesenes Optimum und nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://www.sec.gov/Archives/edgar/data/1000184/000110465923027626/R63.htm',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.sap.com/integrated-reports/2025/en/datahub/financial-data.html',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.sap.com/docs/download/investors/2026/sap-2026-q3-statement.pdf',
                           'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '5J-Median · JE',
                                 'source_label': 'IFRS · IR / Kurse',
                                 'accounting_basis': 'IFRS',
                                 'currency': 'EUR',
                                 'share_basis': 'SAP SE ordinary share, Xetra SAP.DE; EUR per ordinary share, no ADS currency conversion; SAP ADS '
                                                '1:1 not used for price',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'diluted_eps': 4.46,
                                                   'source_url': 'https://www.sec.gov/Archives/edgar/data/1000184/000110465923027626/R63.htm',
                                                   'close': 124.900002,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/SAP.DE/history/',
                                                   'split_adjusted_close': 124.900002,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2021-12-30',
                                                   'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/1000184/000110465923027626/R63.htm',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 28.004484753},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'diluted_eps': 1.94,
                                                   'source_url': 'https://www.sec.gov/Archives/edgar/data/1000184/000110465923027626/R63.htm',
                                                   'close': 96.389999,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/SAP.DE/history/',
                                                   'split_adjusted_close': 96.389999,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2022-12-30',
                                                   'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/1000184/000110465923027626/R63.htm',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 49.685566495},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'diluted_eps': 5.2,
                                                   'source_url': 'https://www.sap.com/integrated-reports/2025/en/datahub/financial-data.html',
                                                   'close': 139.479996,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/SAP.DE/history/',
                                                   'split_adjusted_close': 139.479996,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2023-12-29',
                                                   'eps_source_url': 'https://www.sap.com/integrated-reports/2025/en/datahub/financial-data.html',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 26.823076154},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'diluted_eps': 2.65,
                                                   'source_url': 'https://www.sap.com/integrated-reports/2025/en/datahub/financial-data.html',
                                                   'close': 236.300003,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/SAP.DE/history/',
                                                   'split_adjusted_close': 236.300003,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2024-12-30',
                                                   'eps_source_url': 'https://www.sap.com/integrated-reports/2025/en/datahub/financial-data.html',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 89.169812453},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'diluted_eps': 6.1,
                                                   'source_url': 'https://www.sap.com/integrated-reports/2025/en/datahub/financial-data.html',
                                                   'close': 208.350006,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/SAP.DE/history/',
                                                   'split_adjusted_close': 208.350006,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2025-12-30',
                                                   'eps_source_url': 'https://www.sap.com/integrated-reports/2025/en/datahub/financial-data.html',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 34.155738689}],
                                 'method': 'Median der positiven Geschäftsjahresend-KGVs FY2021–FY2025: letzter Schlusskurs am oder vor dem '
                                           'jeweiligen Geschäftsjahresende ÷ veröffentlichtes Jahres-EPS derselben Aktie in derselben Währung. 5 '
                                           'positive Beobachtungen aus 5 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. IFRS reported diluted EPS, continuing and discontinued operations. SAP SE ordinary '
                                           'share, Xetra SAP.DE; EUR per ordinary share, no ADS currency conversion; SAP ADS 1:1 not used for price. '
                                           'Kurse ohne Dividendenbereinigung. Wenn Yahoo nachträglich splitbereinigte Kurse liefert, werden diese '
                                           'anhand der nachfolgenden Splits auf die veröffentlichte EPS-Aktienbasis zurückgeführt. Nachträglich '
                                           'rekonstruierte Jahresend-Stichprobe, kein Durchschnitt täglicher TTM-KGVs und keine rückgetestete '
                                           'Strategie.',
                                 'source_url': 'https://www.sap.com/integrated-reports/2025/en/datahub/financial-data.html',
                                 'price_source_url': 'https://finance.yahoo.com/quote/SAP.DE/history/',
                                 'comparable_to_current': True,
                                 'comparison_note': '2023 enthält den Qualtrics-Verkauf beziehungsweise aufgegebene Aktivitäten; 2024 erhebliche '
                                                    'Restrukturierungsbelastungen. Verwendet wird berichtetes verwässertes IFRS-EPS, kein '
                                                    'bereinigter oder prognostizierter Gewinn.',
                                 'excluded_years': []},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'EUR',
                                  'share_basis': 'SAP SE ordinary share, Xetra SAP.DE; EUR per ordinary share, no ADS currency conversion; SAP ADS '
                                                 '1:1 not used for price',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:02+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-30',
                                                    'close': 124.900002,
                                                    'dividends_per_share': 1.85,
                                                    'dividend_yield': 1.48118493,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/SAP.DE/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 96.389999,
                                                    'dividends_per_share': 2.45,
                                                    'dividend_yield': 2.54175746,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/SAP.DE/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 139.479996,
                                                    'dividends_per_share': 2.05,
                                                    'dividend_yield': 1.469744811,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/SAP.DE/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-30',
                                                    'close': 236.300003,
                                                    'dividends_per_share': 2.2,
                                                    'dividend_yield': 0.931019878,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/SAP.DE/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-30',
                                                    'close': 208.350006,
                                                    'dividends_per_share': 2.35,
                                                    'dividend_yield': 1.127909734,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/SAP.DE/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/SAP.DE/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://www.sap.com/docs/download/investors/2026/sap-2026-q3-statement.pdf',
                       'as_of': '2026-Q3',
                       'facts': {'cloud_backlog_growth_constant_currency_pct': 26,
                                 'cloud_revenue_guidance_2026_billion_eur': [25.8, 26.2],
                                 'free_cash_flow_guidance_2026_billion_eur': 10},
                       'source_pages': 'PDF p1 and p4; 2026 cloud/cloud+software outlook toward upper end; non-IFRS operating-profit '
                                       'guidance11.8–12.2bn at constant currencies'}},
 {'name': 'Apple',
  'ticker': 'AAPL',
  'cluster': 'Technologie',
  'isin': 'US0378331005',
  'share_class': 'NASDAQ-Stammaktie; EPS und Kurse auf der Aktienbasis nach dem 4:1-Split 2020; keine ADS-Umrechnung.',
  'reference_set': 'classic-2026',
  'hist_pe': 34.243969,
  'hist_div': 0.430221,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 26x und Tages-RSI (14) ≤ 45',
  'buy_trigger_rationale': 'Die installierte Gerätebasis und margenstarke Services stützen einen Qualitätsaufschlag; Hardwarezyklus, China und '
                           'Margendruck sprechen für Preisdisziplin. Höchstens 26x Forward-KGV und RSI bis 45 sind ein eigener geduldiger '
                           'Einstiegsvorschlag, keine optimale oder rückgetestete Strategie. Eigener Vorschlag, kein nachgewiesenes Optimum und '
                           'nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://s2.q4cdn.com/470004039/files/doc_earnings/2023/q4/filing/_10-K-Q4-2023-As-Filed.pdf',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://d18rn0p25nwr6d.cloudfront.net/CIK-0000320193/c24e7a28-5254-4dfa-9447-62aaa3c24bb1.pdf',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://s2.q4cdn.com/470004039/files/doc_earnings/2026/q4/filing/10K-Q4-2026-as-filed.pdf',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.apple.com/newsroom/2026/11/apple-reports-fourth-quarter-results/',
                           'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '5J-Median · JE',
                                 'source_label': 'US-GAAP · IR / Kurse',
                                 'accounting_basis': 'US-GAAP',
                                 'currency': 'USD',
                                 'share_basis': 'NASDAQ-Stammaktie; EPS und Kurse auf der Aktienbasis nach dem 4:1-Split 2020; keine ADS-Umrechnung.',
                                 'period': 'FY2022–FY2026',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2022,
                                                   'fiscal_end': '2022-09-24',
                                                   'diluted_eps': 6.11,
                                                   'dividend_per_share': 0.9,
                                                   'source_url': 'https://s2.q4cdn.com/470004039/files/doc_earnings/2023/q4/filing/_10-K-Q4-2023-As-Filed.pdf',
                                                   'source_location': '2023Form10-Kconsolidatedstatements;earningsandshareholdersequityprintedp31',
                                                   'close': 150.429993,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/AAPL/history/',
                                                   'split_adjusted_close': 150.429993,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2022-09-23',
                                                   'eps_source_url': 'https://s2.q4cdn.com/470004039/files/doc_earnings/2023/q4/filing/_10-K-Q4-2023-As-Filed.pdf',
                                                   'currency': 'USD',
                                                   'pe_is_published': False,
                                                   'pe': 24.620293453},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-09-30',
                                                   'diluted_eps': 6.13,
                                                   'dividend_per_share': 0.94,
                                                   'source_url': 'https://d18rn0p25nwr6d.cloudfront.net/CIK-0000320193/c24e7a28-5254-4dfa-9447-62aaa3c24bb1.pdf',
                                                   'source_location': '2025Form10-Kconsolidatedstatementsofoperations;equityprintedp32',
                                                   'close': 171.210007,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/AAPL/history/',
                                                   'split_adjusted_close': 171.210007,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2023-09-29',
                                                   'eps_source_url': 'https://d18rn0p25nwr6d.cloudfront.net/CIK-0000320193/c24e7a28-5254-4dfa-9447-62aaa3c24bb1.pdf',
                                                   'currency': 'USD',
                                                   'pe_is_published': False,
                                                   'pe': 27.929854323},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-09-28',
                                                   'diluted_eps': 6.08,
                                                   'dividend_per_share': 0.98,
                                                   'source_url': 'https://s2.q4cdn.com/470004039/files/doc_earnings/2026/q4/filing/10K-Q4-2026-as-filed.pdf',
                                                   'source_location': '2026Form10-Kconsolidatedoperationsandshareholdersequityprintedp35',
                                                   'close': 227.789993,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/AAPL/history/',
                                                   'split_adjusted_close': 227.789993,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2024-09-27',
                                                   'eps_source_url': 'https://s2.q4cdn.com/470004039/files/doc_earnings/2026/q4/filing/10K-Q4-2026-as-filed.pdf',
                                                   'currency': 'USD',
                                                   'pe_is_published': False,
                                                   'pe': 37.465459375},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-09-27',
                                                   'diluted_eps': 7.46,
                                                   'dividend_per_share': 1.02,
                                                   'source_url': 'https://s2.q4cdn.com/470004039/files/doc_earnings/2026/q4/filing/10K-Q4-2026-as-filed.pdf',
                                                   'source_location': '2026Form10-Kconsolidatedoperationsandshareholdersequityprintedp35',
                                                   'close': 255.460007,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/AAPL/history/',
                                                   'split_adjusted_close': 255.460007,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2025-09-26',
                                                   'eps_source_url': 'https://s2.q4cdn.com/470004039/files/doc_earnings/2026/q4/filing/10K-Q4-2026-as-filed.pdf',
                                                   'currency': 'USD',
                                                   'pe_is_published': False,
                                                   'pe': 34.243968767},
                                                  {'fiscal_year': 2026,
                                                   'fiscal_end': '2026-09-26',
                                                   'diluted_eps': 8.98,
                                                   'dividend_per_share': 1.06,
                                                   'source_url': 'https://s2.q4cdn.com/470004039/files/doc_earnings/2026/q4/filing/10K-Q4-2026-as-filed.pdf',
                                                   'source_location': '2026Form10-Kconsolidatedoperationsandshareholdersequityprintedp35',
                                                   'close': 341.070007,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/AAPL/history/',
                                                   'split_adjusted_close': 341.070007,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2026-09-25',
                                                   'eps_source_url': 'https://s2.q4cdn.com/470004039/files/doc_earnings/2026/q4/filing/10K-Q4-2026-as-filed.pdf',
                                                   'currency': 'USD',
                                                   'pe_is_published': False,
                                                   'pe': 37.981069822}],
                                 'method': 'Median der positiven Geschäftsjahresend-KGVs FY2022–FY2026: letzter Schlusskurs am oder vor dem '
                                           'jeweiligen Geschäftsjahresende ÷ veröffentlichtes Jahres-EPS derselben Aktie in derselben Währung. 5 '
                                           'positive Beobachtungen aus 5 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. USGAAPdilutedEPS. NASDAQ-Stammaktie; EPS und Kurse auf der Aktienbasis nach dem '
                                           '4:1-Split 2020; keine ADS-Umrechnung.. Kurse ohne Dividendenbereinigung. Wenn Yahoo nachträglich '
                                           'splitbereinigte Kurse liefert, werden diese anhand der nachfolgenden Splits auf die veröffentlichte '
                                           'EPS-Aktienbasis zurückgeführt. Nachträglich rekonstruierte Jahresend-Stichprobe, kein Durchschnitt '
                                           'täglicher TTM-KGVs und keine rückgetestete Strategie.',
                                 'source_url': 'https://s2.q4cdn.com/470004039/files/doc_earnings/2026/q4/filing/10K-Q4-2026-as-filed.pdf',
                                 'price_source_url': 'https://finance.yahoo.com/quote/AAPL/history/',
                                 'comparable_to_current': True,
                                 'comparison_note': 'Tatsächliche Geschäftsjahresenden im September statt Dezemberkurse. Berichtetes US-GAAP-EPS '
                                                    'einschließlich der einmaligen EU-Steuerbelastung 2024 und Sondereffekten 2026. Keine '
                                                    'zusätzliche Splitumrechnung für FY2022–FY2026 erforderlich.',
                                 'excluded_years': []},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'USD',
                                  'share_basis': 'NASDAQ-Stammaktie; EPS und Kurse auf der Aktienbasis nach dem 4:1-Split 2020; keine '
                                                 'ADS-Umrechnung.',
                                  'period': 'FY2022–FY2026',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:04+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2022,
                                                    'price_date': '2022-09-23',
                                                    'close': 150.429993,
                                                    'dividends_per_share': 0.9,
                                                    'dividend_yield': 0.598284946,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/AAPL/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-09-29',
                                                    'close': 171.210007,
                                                    'dividends_per_share': 0.94,
                                                    'dividend_yield': 0.549033329,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/AAPL/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-09-27',
                                                    'close': 227.789993,
                                                    'dividends_per_share': 0.98,
                                                    'dividend_yield': 0.43022083,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/AAPL/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-09-26',
                                                    'close': 255.460007,
                                                    'dividends_per_share': 1.02,
                                                    'dividend_yield': 0.39927972,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/AAPL/history/'},
                                                   {'fiscal_year': 2026,
                                                    'price_date': '2026-09-25',
                                                    'close': 341.070007,
                                                    'dividends_per_share': 1.06,
                                                    'dividend_yield': 0.310786635,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/AAPL/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/AAPL/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://www.apple.com/newsroom/2026/11/apple-reports-fourth-quarter-results/',
                       'as_of': '2026-11-02',
                       'facts': 'Q4 FY2026: berichtetes EPS 2,10 USD und Bruttomarge 47,9%; jeweils positive Effekte aus Zollrückerstattungen (0,05 '
                                'USD EPS-Beitrag). FY2026-Gesamt-EPS 8,98 USD. Quartalsumsatz 114,5 Mrd. USD.'}},
 {'name': 'BMW (Stamm)',
  'ticker': 'BMW.DE',
  'cluster': 'Automobil & Nutzfahrzeuge',
  'isin': 'DE0005190003',
  'share_class': 'Ordinary share; NOT BMW3.DE preference',
  'reference_set': 'classic-2026',
  'hist_pe': 5.703452,
  'hist_div': 6.956105,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 7x und Kurs ≤ SMA 200',
  'buy_trigger_rationale': 'Vorschlag: bei zyklischen Autogewinnen einen niedrigen Forward-Multiplikator mit einem Rücksetzer zur langfristigen '
                           'Kurslinie verbinden. 2022 nicht als Normalgewinn unterstellen; Schwellen nicht rückgetestet. Eigener Vorschlag, kein '
                           'nachgewiesenes Optimum und nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://newsroom.bmwgroup.com/global/bmw-group-accelerates-realignment/',
                           'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '5J-Median · JE',
                                 'source_label': 'IFRS · IR / Kurse',
                                 'accounting_basis': 'IFRS',
                                 'currency': 'EUR',
                                 'share_basis': 'Ordinary share; NOT BMW3.DE preference',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'diluted_eps': 18.77,
                                                   'close': 88.49,
                                                   'pe': 4.714437933,
                                                   'pe_is_published': False,
                                                   'dividend_per_share': 5.8,
                                                   'source_url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                                                   'price_source_url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                                                   'price_date': '2021-12-30',
                                                   'eps_source_url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'diluted_eps': 27.31,
                                                   'close': 83.38,
                                                   'pe': 3.053094105,
                                                   'pe_is_published': False,
                                                   'dividend_per_share': 8.5,
                                                   'source_url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                                                   'price_source_url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                                                   'price_date': '2022-12-30',
                                                   'eps_source_url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'diluted_eps': 17.67,
                                                   'close': 100.78,
                                                   'pe': 5.703452179,
                                                   'pe_is_published': False,
                                                   'dividend_per_share': 6,
                                                   'source_url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                                                   'price_source_url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                                                   'price_date': '2023-12-29',
                                                   'eps_source_url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'diluted_eps': 11.62,
                                                   'close': 78.98,
                                                   'pe': 6.796901893,
                                                   'pe_is_published': False,
                                                   'dividend_per_share': 4.3,
                                                   'source_url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                                                   'price_source_url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                                                   'price_date': '2024-12-30',
                                                   'eps_source_url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'diluted_eps': 11.89,
                                                   'close': 93.14,
                                                   'pe': 7.833473507,
                                                   'pe_is_published': False,
                                                   'dividend_per_share': 4.4,
                                                   'source_url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                                                   'price_source_url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                                                   'price_date': '2025-12-30',
                                                   'eps_source_url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                                                   'currency': 'EUR'}],
                                 'method': 'Median der positiven Geschäftsjahresend-KGVs FY2021–FY2025: letzter Schlusskurs am oder vor dem '
                                           'jeweiligen Geschäftsjahresende ÷ veröffentlichtes Jahres-EPS derselben Aktie in derselben Währung. 5 '
                                           'positive Beobachtungen aus 5 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. IFRS reported diluted EPS, exact listed share class; no adjusted earnings. Ordinary '
                                           'share; NOT BMW3.DE preference. Kurse ohne Dividendenbereinigung. Wenn Yahoo nachträglich splitbereinigte '
                                           'Kurse liefert, werden diese anhand der nachfolgenden Splits auf die veröffentlichte EPS-Aktienbasis '
                                           'zurückgeführt. Nachträglich rekonstruierte Jahresend-Stichprobe, kein Durchschnitt täglicher TTM-KGVs '
                                           'und keine rückgetestete Strategie.',
                                 'source_url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                                 'price_source_url': 'https://www.bmwgroup.com/en/report/2025/downloads/BMW-Group-Report-2025-en.pdf',
                                 'comparable_to_current': True,
                                 'comparison_note': 'Stammaktie, nicht Vorzugsaktie. 2022 erhöhte die Neubewertung im Zuge der Vollkonsolidierung '
                                                    'von BMW Brilliance den Gewinn; dadurch ist das damalige KGV außergewöhnlich niedrig. Gemeldete '
                                                    'Gewinne sind stark zyklisch.',
                                 'excluded_years': []},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'EUR',
                                  'share_basis': 'Ordinary share; NOT BMW3.DE preference',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:04+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-30',
                                                    'close': 88.489998,
                                                    'dividends_per_share': 1.9,
                                                    'dividend_yield': 2.147135321,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/BMW.DE/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 83.379997,
                                                    'dividends_per_share': 5.8,
                                                    'dividend_yield': 6.956104811,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/BMW.DE/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 100.779999,
                                                    'dividends_per_share': 8.5,
                                                    'dividend_yield': 8.43421324,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/BMW.DE/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-30',
                                                    'close': 78.980003,
                                                    'dividends_per_share': 6.0,
                                                    'dividend_yield': 7.596859642,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/BMW.DE/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-30',
                                                    'close': 93.139999,
                                                    'dividends_per_share': 4.3,
                                                    'dividend_yield': 4.616706064,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/BMW.DE/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/BMW.DE/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://newsroom.bmwgroup.com/global/bmw-group-accelerates-realignment/',
                       'as_of': '2026-11-04',
                       'facts': {'fy2026_automotive_reported_ebit_margin_guidance_percent': [-0.5, 0.5],
                                 'fy2026_automotive_fcf_guidance_eur_billion_min': 2.5}}},
 {'name': 'Coca-Cola',
  'ticker': 'KO',
  'cluster': 'Konsum',
  'isin': 'US1912161007',
  'share_class': 'NYSE common stock USD0.25par; 2012split predates all observations',
  'reference_set': 'classic-2026',
  'hist_pe': 25.308943,
  'hist_div': 2.918037,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 22x und Dividendenrendite ≥ 3,0%',
  'buy_trigger_rationale': 'Defensive Marken und wiederkehrende Cashflows rechtfertigen einen Qualitätsaufschlag. Der eigene Vorschlag von höchstens '
                           '22x Forward-KGV liegt unter dem rekonstruierten Jahresend-Median von rund 25,3x; mindestens 3,0% Dividendenrendite '
                           'ergänzt Ausschüttungsdisziplin. Keine rückgetestete oder optimale Schwelle. Eigener Vorschlag, kein nachgewiesenes '
                           'Optimum und nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://investors.coca-colacompany.com/filings-reports/all-sec-filings/content/0000021344-24-000009/ko-20231231.htm',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://investors.coca-colacompany.com/filings-reports/annual-filings-10-k/content/0000021344-25-000011/ko-20241231.htm',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.coca-colacompany.com/media-center/coca-cola-reports-fourth-quarter-and-full-year-2025-results',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://investors.coca-colacompany.com/news-events/press-releases/detail/1176/coca-cola-reports-third-quarter-2026-results-and-raises-full-year-guidance',
                           'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '5J-Median · JE',
                                 'source_label': 'US-GAAP · IR / Kurse',
                                 'accounting_basis': 'US-GAAP',
                                 'currency': 'USD',
                                 'share_basis': 'NYSE common stock USD0.25par; 2012split predates all observations',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'diluted_eps': 2.25,
                                                   'close': 59.21,
                                                   'dividend_per_share': 1.68,
                                                   'source_url': 'https://investors.coca-colacompany.com/filings-reports/all-sec-filings/content/0000021344-24-000009/ko-20231231.htm',
                                                   'source_location': '2023 Form10-K consolidatedstatementsofincome',
                                                   'price_source_url': 'https://investors.coca-colacompany.com/stock-info/year-end',
                                                   'pe': 26.315555556,
                                                   'dividend_reference_yield_pct': 2.83735855,
                                                   'price_date': '2021-12-31',
                                                   'eps_source_url': 'https://investors.coca-colacompany.com/filings-reports/all-sec-filings/content/0000021344-24-000009/ko-20231231.htm',
                                                   'currency': 'USD',
                                                   'pe_is_published': False},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'diluted_eps': 2.19,
                                                   'close': 63.61,
                                                   'dividend_per_share': 1.76,
                                                   'source_url': 'https://investors.coca-colacompany.com/filings-reports/annual-filings-10-k/content/0000021344-25-000011/ko-20241231.htm',
                                                   'source_location': '2024 Form10-K printedp62 consolidatedstatementsofincome',
                                                   'price_source_url': 'https://investors.coca-colacompany.com/stock-info/year-end',
                                                   'pe': 29.0456621,
                                                   'dividend_reference_yield_pct': 2.76686056,
                                                   'price_date': '2022-12-30',
                                                   'eps_source_url': 'https://investors.coca-colacompany.com/filings-reports/annual-filings-10-k/content/0000021344-25-000011/ko-20241231.htm',
                                                   'currency': 'USD',
                                                   'pe_is_published': False},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'diluted_eps': 2.47,
                                                   'close': 58.93,
                                                   'dividend_per_share': 1.84,
                                                   'source_url': 'https://investors.coca-colacompany.com/filings-reports/annual-filings-10-k/content/0000021344-25-000011/ko-20241231.htm',
                                                   'source_location': '2024 Form10-K printedp62 consolidatedstatementsofincome',
                                                   'price_source_url': 'https://investors.coca-colacompany.com/stock-info/year-end',
                                                   'pe': 23.858299595,
                                                   'dividend_reference_yield_pct': 3.12234855,
                                                   'price_date': '2023-12-29',
                                                   'eps_source_url': 'https://investors.coca-colacompany.com/filings-reports/annual-filings-10-k/content/0000021344-25-000011/ko-20241231.htm',
                                                   'currency': 'USD',
                                                   'pe_is_published': False},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'diluted_eps': 2.46,
                                                   'close': 62.26,
                                                   'dividend_per_share': 1.94,
                                                   'source_url': 'https://investors.coca-colacompany.com/filings-reports/annual-filings-10-k/content/0000021344-25-000011/ko-20241231.htm',
                                                   'source_location': '2024 Form10-K printedp62 consolidatedstatementsofincome',
                                                   'price_source_url': 'https://investors.coca-colacompany.com/stock-info/year-end',
                                                   'pe': 25.308943089,
                                                   'dividend_reference_yield_pct': 3.11596531,
                                                   'price_date': '2024-12-31',
                                                   'eps_source_url': 'https://investors.coca-colacompany.com/filings-reports/annual-filings-10-k/content/0000021344-25-000011/ko-20241231.htm',
                                                   'currency': 'USD',
                                                   'pe_is_published': False},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'diluted_eps': 3.04,
                                                   'close': 69.91,
                                                   'dividend_per_share': 2.04,
                                                   'source_url': 'https://www.coca-colacompany.com/media-center/coca-cola-reports-fourth-quarter-and-full-year-2025-results',
                                                   'source_location': 'Full-yearEPS headline; financialrelease dilutedEPS3.04 (basic3.05)',
                                                   'price_source_url': 'https://investors.coca-colacompany.com/stock-info/year-end',
                                                   'pe': 22.996710526,
                                                   'dividend_reference_yield_pct': 2.91803748,
                                                   'price_date': '2025-12-31',
                                                   'eps_source_url': 'https://www.coca-colacompany.com/media-center/coca-cola-reports-fourth-quarter-and-full-year-2025-results',
                                                   'currency': 'USD',
                                                   'pe_is_published': False}],
                                 'method': 'Median der positiven Geschäftsjahresend-KGVs FY2021–FY2025: letzter Schlusskurs am oder vor dem '
                                           'jeweiligen Geschäftsjahresende ÷ veröffentlichtes Jahres-EPS derselben Aktie in derselben Währung. 5 '
                                           'positive Beobachtungen aus 5 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. US GAAP diluted net income per common share. NYSE common stock USD0.25par; 2012split '
                                           'predates all observations. Kurse ohne Dividendenbereinigung. Wenn Yahoo nachträglich splitbereinigte '
                                           'Kurse liefert, werden diese anhand der nachfolgenden Splits auf die veröffentlichte EPS-Aktienbasis '
                                           'zurückgeführt. Nachträglich rekonstruierte Jahresend-Stichprobe, kein Durchschnitt täglicher TTM-KGVs '
                                           'und keine rückgetestete Strategie.',
                                 'source_url': 'https://www.coca-colacompany.com/media-center/coca-cola-reports-fourth-quarter-and-full-year-2025-results',
                                 'price_source_url': 'https://investors.coca-colacompany.com/stock-info/year-end',
                                 'comparable_to_current': True,
                                 'comparison_note': 'Durchgehend berichtetes verwässertes US-GAAP-EPS. 2024/2025 enthalten BODYARMOR-Abschreibungen; '
                                                    '2025 außerdem einen Gewinn aus dem Verkauf des Anteils an Coca-Cola Consolidated. Bereinigtes '
                                                    'EPS wird nicht beigemischt.',
                                 'excluded_years': []},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'USD',
                                  'share_basis': 'NYSE common stock USD0.25par; 2012split predates all observations',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:06+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-31',
                                                    'close': 59.209999,
                                                    'dividends_per_share': 1.68,
                                                    'dividend_yield': 2.837358598,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/KO/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 63.610001,
                                                    'dividends_per_share': 1.76,
                                                    'dividend_yield': 2.76686053,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/KO/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 58.93,
                                                    'dividends_per_share': 1.84,
                                                    'dividend_yield': 3.122348533,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/KO/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-31',
                                                    'close': 62.259998,
                                                    'dividends_per_share': 1.94,
                                                    'dividend_yield': 3.115965391,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/KO/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-31',
                                                    'close': 69.910004,
                                                    'dividends_per_share': 2.04,
                                                    'dividend_yield': 2.918037324,
                                                    'currency': 'USD',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/KO/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/KO/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://investors.coca-colacompany.com/news-events/press-releases/detail/1176/coca-cola-reports-third-quarter-2026-results-and-raises-full-year-guidance',
                       'as_of': '2026-10-27',
                       'facts': 'Q3 2026: Absatzvolumen +5%, organischer Umsatz +7%, berichtetes EPS 0,93 USD, bereinigtes EPS 0,91 USD. '
                                'Jahresprognose angehoben. Diese Primärdaten stützen die Geschäftseinordnung, nicht einen konkreten Kaufkurs.'}},
 {'name': 'Jungheinrich (Vorz.)',
  'ticker': 'JUN3.DE',
  'cluster': 'Industrie & Logistik',
  'isin': 'DE0006219934',
  'share_class': 'Listed non-voting preference share, ISIN DE0006219934; NOT unlisted ordinary shares',
  'reference_set': 'classic-2026',
  'hist_pe': 11.3,
  'hist_div': 2.258611,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 12x und Kurs ≤ SMA 200',
  'buy_trigger_rationale': 'Vorschlag: bei zyklischer Intralogistik eine Bewertung um das frühere Normalniveau und einen Rücksetzer zur '
                           'langfristigen Kurslinie verlangen; 2025 enthält Sondereffekte. Schwellen selbst gewählt, nicht rückgetestet. Eigener '
                           'Vorschlag, kein nachgewiesenes Optimum und nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/935524/a05be293fa008c1af7edae272f3c5bd1/annual-report-2021-data.pdf',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/1573992/63397e59e19f28d7ac76eb4556bc4895/annual-report-2023-data.pdf',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/2383280/ba4de8a88cd546feccb68a746c06817b/download-gb2025-data.pdf',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.jungheinrich.com/en/newsroom/press-releases/jungheinrich-after-nine-months-in-2026-2631784',
                           'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '5J-Median · JE',
                                 'source_label': 'IFRS · IR / Kurse',
                                 'accounting_basis': 'IFRS',
                                 'currency': 'EUR',
                                 'share_basis': 'Listed non-voting preference share, ISIN DE0006219934; NOT unlisted ordinary shares',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'diluted_eps': 2.62,
                                                   'close': 44.88,
                                                   'pe': 17.1,
                                                   'pe_is_published': True,
                                                   'dividend_per_share': 0.68,
                                                   'source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/935524/a05be293fa008c1af7edae272f3c5bd1/annual-report-2021-data.pdf',
                                                   'price_source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/935524/a05be293fa008c1af7edae272f3c5bd1/annual-report-2021-data.pdf',
                                                   'price_date': '2021-12-30',
                                                   'eps_source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/935524/a05be293fa008c1af7edae272f3c5bd1/annual-report-2021-data.pdf',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'diluted_eps': 2.65,
                                                   'close': 26.58,
                                                   'pe': 10,
                                                   'pe_is_published': True,
                                                   'dividend_per_share': 0.68,
                                                   'source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/1573992/63397e59e19f28d7ac76eb4556bc4895/annual-report-2023-data.pdf',
                                                   'price_source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/1573992/63397e59e19f28d7ac76eb4556bc4895/annual-report-2023-data.pdf',
                                                   'price_date': '2022-12-30',
                                                   'eps_source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/1573992/63397e59e19f28d7ac76eb4556bc4895/annual-report-2023-data.pdf',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'diluted_eps': 2.94,
                                                   'close': 33.22,
                                                   'pe': 11.3,
                                                   'pe_is_published': True,
                                                   'dividend_per_share': 0.75,
                                                   'source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/1573992/63397e59e19f28d7ac76eb4556bc4895/annual-report-2023-data.pdf',
                                                   'price_source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/1573992/63397e59e19f28d7ac76eb4556bc4895/annual-report-2023-data.pdf',
                                                   'price_date': '2023-12-29',
                                                   'eps_source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/1573992/63397e59e19f28d7ac76eb4556bc4895/annual-report-2023-data.pdf',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'diluted_eps': 2.84,
                                                   'close': 25.66,
                                                   'pe': 9,
                                                   'pe_is_published': True,
                                                   'dividend_per_share': 0.8,
                                                   'source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/2383280/ba4de8a88cd546feccb68a746c06817b/download-gb2025-data.pdf',
                                                   'price_source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/2383280/ba4de8a88cd546feccb68a746c06817b/download-gb2025-data.pdf',
                                                   'price_date': '2024-12-30',
                                                   'eps_source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/2383280/ba4de8a88cd546feccb68a746c06817b/download-gb2025-data.pdf',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'diluted_eps': 1.03,
                                                   'close': 35.42,
                                                   'pe': 34.4,
                                                   'pe_is_published': True,
                                                   'dividend_per_share': 0.29,
                                                   'source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/2383280/ba4de8a88cd546feccb68a746c06817b/download-gb2025-data.pdf',
                                                   'price_source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/2383280/ba4de8a88cd546feccb68a746c06817b/download-gb2025-data.pdf',
                                                   'price_date': '2025-12-30',
                                                   'eps_source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/2383280/ba4de8a88cd546feccb68a746c06817b/download-gb2025-data.pdf',
                                                   'currency': 'EUR'}],
                                 'method': 'Median der direkt vom Emittenten veröffentlichten positiven Jahresend-KGVs FY2021–FY2025. Gerundete EPS '
                                           'dienen nur zur Dokumentation, nicht zur Neuberechnung dieser veröffentlichten Verhältnisse. 5 positive '
                                           'Beobachtungen aus 5 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. IFRS reported diluted EPS, exact listed share class; no adjusted earnings. Listed '
                                           'non-voting preference share, ISIN DE0006219934; NOT unlisted ordinary shares. Kurse ohne '
                                           'Dividendenbereinigung. Wenn Yahoo nachträglich splitbereinigte Kurse liefert, werden diese anhand der '
                                           'nachfolgenden Splits auf die veröffentlichte EPS-Aktienbasis zurückgeführt. Nachträglich rekonstruierte '
                                           'Jahresend-Stichprobe, kein Durchschnitt täglicher TTM-KGVs und keine rückgetestete Strategie.',
                                 'source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/2383280/ba4de8a88cd546feccb68a746c06817b/download-gb2025-data.pdf',
                                 'price_source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/2383280/ba4de8a88cd546feccb68a746c06817b/download-gb2025-data.pdf',
                                 'comparable_to_current': True,
                                 'comparison_note': 'Vorzugsaktie und Vorzugs-EPS. 2025 belasten Transformation und Russland-Effekte den berichteten '
                                                    'Gewinn; das erhöhte KGV von 34,4 bleibt in der Stichprobe. Der Median ersetzt keine Prüfung der '
                                                    'laufenden Ertragsnormalisierung.',
                                 'excluded_years': []},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'EUR',
                                  'share_basis': 'Listed non-voting preference share, ISIN DE0006219934; NOT unlisted ordinary shares',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:06+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-30',
                                                    'close': 44.880001,
                                                    'dividends_per_share': 0.43,
                                                    'dividend_yield': 0.958110494,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/JUN3.DE/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 26.58,
                                                    'dividends_per_share': 0.68,
                                                    'dividend_yield': 2.55831453,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/JUN3.DE/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 33.220001,
                                                    'dividends_per_share': 0.68,
                                                    'dividend_yield': 2.046959588,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/JUN3.DE/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-30',
                                                    'close': 25.66,
                                                    'dividends_per_share': 0.75,
                                                    'dividend_yield': 2.922837118,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/JUN3.DE/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-30',
                                                    'close': 35.419998,
                                                    'dividends_per_share': 0.8,
                                                    'dividend_yield': 2.258611071,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/JUN3.DE/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/JUN3.DE/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://www.jungheinrich.com/en/newsroom/press-releases/jungheinrich-after-nine-months-in-2026-2631784',
                       'as_of': '2026-11-10',
                       'facts': {'fy2026_ebit_guidance_eur_million': [340, 400], 'fy2026_ebit_margin_guidance_percent': [6.2, 7.0]}},
  'annual_roe_reference': {'value': 4.235746,
                           'as_of': '2025-12-31',
                           'verified_at': '2026-11-18',
                           'reference_version': 1,
                           'provider': 'Jungheinrich IR',
                           'label': 'ROE FY2025',
                           'source_label': 'IR · FY2025',
                           'accounting_basis': 'IFRS',
                           'currency': 'EUR',
                           'period': 'FY2025',
                           'source_url': 'https://media-live2.prod.scw.jungheinrichcloud.com/resource/blob/2383280/ba4de8a88cd546feccb68a746c06817b/download-gb2025-data.pdf',
                           'method': 'Jahresergebnis der Eigentümer 103,6 Mio. EUR ÷ durchschnittliches den Eigentümern zurechenbares Eigenkapital '
                                     '((2436,3 + 2455,4) / 2 Mio. EUR) ×100. Jahreswert FY2025, kein aktueller TTM-Wert.',
                           'calculation_inputs': {'owner_net_income_eur_million': 103.6,
                                                  'owner_equity_open_eur_million': 2436.3,
                                                  'owner_equity_close_eur_million': 2455.4}}},
 {'name': 'Deutsche Telekom',
  'ticker': 'DTE.DE',
  'cluster': 'Telekommunikation',
  'isin': 'DE0005557508',
  'share_class': 'Deutsche Telekom AG ordinary registered share, Xetra DTE.DE',
  'reference_set': 'classic-2026',
  'hist_pe': 12.726872,
  'hist_div': 3.253796,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 13x und Dividendenrendite ≥ 4%',
  'buy_trigger_rationale': 'Eigener Vorschlag, nicht rückgetestet: Wiederkehrende Telekommunikationserlöse und steigender Free Cashflow; '
                           'Bewertungsdisziplin über13x und mindestens4% laufende Rendite. Die erwartete2026-Dividende ist noch eine '
                           'Absichtserklärung, kein gezahlter Betrag. Eigener Vorschlag, kein nachgewiesenes Optimum und nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://report.telekom.com/annual-report-2023/to-our-shareholders/the-t-share.html',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://report.telekom.com/annual-report-2025/to-our-shareholders/the-t-share.html',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.telekom.com/en/newsroom/latest-updates/media-information/2026/11/more-growth-raised-guidance-record-dividend',
                           'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '5J-Median · JE',
                                 'source_label': 'IFRS · IR / Kurse',
                                 'accounting_basis': 'IFRS',
                                 'currency': 'EUR',
                                 'share_basis': 'Deutsche Telekom AG ordinary registered share, Xetra DTE.DE',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'diluted_eps': 0.87,
                                                   'source_url': 'https://report.telekom.com/annual-report-2023/to-our-shareholders/the-t-share.html',
                                                   'close': 16.3,
                                                   'price_source_url': 'https://report.telekom.com/annual-report-2023/to-our-shareholders/the-t-share.html',
                                                   'dividend_per_share': 0.64,
                                                   'pe_reconstructed': 18.73563218390805,
                                                   'price_date': '2021-12-30',
                                                   'eps_source_url': 'https://report.telekom.com/annual-report-2023/to-our-shareholders/the-t-share.html',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 18.735632184},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'diluted_eps': 1.61,
                                                   'source_url': 'https://report.telekom.com/annual-report-2023/to-our-shareholders/the-t-share.html',
                                                   'close': 18.64,
                                                   'price_source_url': 'https://report.telekom.com/annual-report-2023/to-our-shareholders/the-t-share.html',
                                                   'dividend_per_share': 0.7,
                                                   'pe_reconstructed': 11.577639751552795,
                                                   'price_date': '2022-12-30',
                                                   'eps_source_url': 'https://report.telekom.com/annual-report-2023/to-our-shareholders/the-t-share.html',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 11.577639752},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'diluted_eps': 3.57,
                                                   'source_url': 'https://report.telekom.com/annual-report-2025/to-our-shareholders/the-t-share.html',
                                                   'close': 21.75,
                                                   'price_source_url': 'https://report.telekom.com/annual-report-2025/to-our-shareholders/the-t-share.html',
                                                   'dividend_per_share': 0.77,
                                                   'pe_reconstructed': 6.0924369747899165,
                                                   'price_date': '2023-12-29',
                                                   'eps_source_url': 'https://report.telekom.com/annual-report-2025/to-our-shareholders/the-t-share.html',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 6.092436975},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'diluted_eps': 2.27,
                                                   'source_url': 'https://report.telekom.com/annual-report-2025/to-our-shareholders/the-t-share.html',
                                                   'close': 28.89,
                                                   'price_source_url': 'https://report.telekom.com/annual-report-2025/to-our-shareholders/the-t-share.html',
                                                   'dividend_per_share': 0.9,
                                                   'pe_reconstructed': 12.726872246696036,
                                                   'price_date': '2024-12-30',
                                                   'eps_source_url': 'https://report.telekom.com/annual-report-2025/to-our-shareholders/the-t-share.html',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 12.726872247},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'diluted_eps': 1.97,
                                                   'source_url': 'https://report.telekom.com/annual-report-2025/to-our-shareholders/the-t-share.html',
                                                   'close': 27.66,
                                                   'price_source_url': 'https://report.telekom.com/annual-report-2025/to-our-shareholders/the-t-share.html',
                                                   'dividend_per_share': 1.0,
                                                   'pe_reconstructed': 14.040609137055839,
                                                   'price_date': '2025-12-30',
                                                   'eps_source_url': 'https://report.telekom.com/annual-report-2025/to-our-shareholders/the-t-share.html',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 14.040609137}],
                                 'method': 'Median der positiven Geschäftsjahresend-KGVs FY2021–FY2025: letzter Schlusskurs am oder vor dem '
                                           'jeweiligen Geschäftsjahresende ÷ veröffentlichtes Jahres-EPS derselben Aktie in derselben Währung. 5 '
                                           'positive Beobachtungen aus 5 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. IFRS reported basic and diluted EPS (equal). Deutsche Telekom AG ordinary registered '
                                           'share, Xetra DTE.DE. Kurse ohne Dividendenbereinigung. Wenn Yahoo nachträglich splitbereinigte Kurse '
                                           'liefert, werden diese anhand der nachfolgenden Splits auf die veröffentlichte EPS-Aktienbasis '
                                           'zurückgeführt. Nachträglich rekonstruierte Jahresend-Stichprobe, kein Durchschnitt täglicher TTM-KGVs '
                                           'und keine rückgetestete Strategie.',
                                 'source_url': 'https://report.telekom.com/annual-report-2025/to-our-shareholders/the-t-share.html',
                                 'price_source_url': 'https://report.telekom.com/annual-report-2025/to-our-shareholders/the-t-share.html',
                                 'comparable_to_current': True,
                                 'comparison_note': '2023 enthält den Gewinn aus dem GD-Towers-Verkauf. Die Reihe verwendet berichtete EPS und '
                                                    'mischt keine bereinigten EPS hinzu. Forward-Schätzungen und historische gemeldete Jahresgewinne '
                                                    'können sich methodisch unterscheiden.',
                                 'excluded_years': []},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'EUR',
                                  'share_basis': 'Deutsche Telekom AG ordinary registered share, Xetra DTE.DE',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:06+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-30',
                                                    'close': 16.299999,
                                                    'dividends_per_share': 0.6,
                                                    'dividend_yield': 3.680981767,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DTE.DE/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 18.638,
                                                    'dividends_per_share': 0.64,
                                                    'dividend_yield': 3.433844743,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DTE.DE/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 21.75,
                                                    'dividends_per_share': 0.7,
                                                    'dividend_yield': 3.218390805,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DTE.DE/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-30',
                                                    'close': 28.889999,
                                                    'dividends_per_share': 0.77,
                                                    'dividend_yield': 2.665282161,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DTE.DE/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-30',
                                                    'close': 27.66,
                                                    'dividends_per_share': 0.9,
                                                    'dividend_yield': 3.253796113,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DTE.DE/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/DTE.DE/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://www.telekom.com/en/newsroom/latest-updates/media-information/2026/11/more-growth-raised-guidance-record-dividend',
                       'as_of': '2026-Q3',
                       'facts': {'recurring_adjusted_eps_guidance_2026_eur': 2.2,
                                 'proposed_dividend_2026_eur': 1.1,
                                 'share_buyback_plan_2027_billion_eur': 2,
                                 'free_cash_flow_al_guidance_2026_billion_eur': 20.1},
                       'note': 'Planned dividend for financialyear2026; actual2025dividend1.00 EUR'}},
 {'name': 'Deutsche Bank',
  'ticker': 'DBK.DE',
  'cluster': 'Banken',
  'isin': 'DE0005140008',
  'share_class': 'Deutsche Bank AG ordinary registered share, Xetra DBK.DE',
  'reference_set': 'classic-2026',
  'hist_pe': 10.715211,
  'hist_div': 2.05376,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 8x und Kurs ≤ Tages-SMA 200',
  'buy_trigger_rationale': 'Eigener Vorschlag, nicht rückgetestet: Bankgewinne reagieren auf Kreditkosten, Zinsen und Rechtsrisiken;8xForward-Gewinn '
                           'plus technischerRücksetzer ist ein vorsichtiger Einstiegsvorschlag. CET1undKapitalrückgaben bleiben '
                           'qualitativeKontrollgrößen. Eigener Vorschlag, kein nachgewiesenes Optimum und nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://investor-relations.db.com/files/documents/other-presentations-and-events/2025/Annual-Report-2025.pdf?language_id=1',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.db.com/news/detail/20261028-deutsche-bank-reports-on-the-third-quarter-results-of-2026?language_id=1',
                           'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '5J-Median · JE',
                                 'source_label': 'IFRS · IR / Kurse',
                                 'accounting_basis': 'IFRS',
                                 'currency': 'EUR',
                                 'share_basis': 'Deutsche Bank AG ordinary registered share, Xetra DBK.DE',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'diluted_eps': 0.93,
                                                   'source_url': 'https://investor-relations.db.com/files/documents/other-presentations-and-events/2025/Annual-Report-2025.pdf?language_id=1',
                                                   'close': 11.018,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/DBK.DE/history/',
                                                   'split_adjusted_close': 11.018,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2021-12-30',
                                                   'eps_source_url': 'https://investor-relations.db.com/files/documents/other-presentations-and-events/2025/Annual-Report-2025.pdf?language_id=1',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 11.847311828},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'diluted_eps': 2.37,
                                                   'source_url': 'https://investor-relations.db.com/files/documents/other-presentations-and-events/2025/Annual-Report-2025.pdf?language_id=1',
                                                   'close': 10.588,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/DBK.DE/history/',
                                                   'split_adjusted_close': 10.588,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2022-12-30',
                                                   'eps_source_url': 'https://investor-relations.db.com/files/documents/other-presentations-and-events/2025/Annual-Report-2025.pdf?language_id=1',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 4.467510549},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'diluted_eps': 2.03,
                                                   'source_url': 'https://investor-relations.db.com/files/documents/other-presentations-and-events/2025/Annual-Report-2025.pdf?language_id=1',
                                                   'close': 12.364,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/DBK.DE/history/',
                                                   'split_adjusted_close': 12.364,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2023-12-29',
                                                   'eps_source_url': 'https://investor-relations.db.com/files/documents/other-presentations-and-events/2025/Annual-Report-2025.pdf?language_id=1',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 6.090640394},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'diluted_eps': 1.37,
                                                   'source_url': 'https://investor-relations.db.com/files/documents/other-presentations-and-events/2025/Annual-Report-2025.pdf?language_id=1',
                                                   'close': 16.639999,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/DBK.DE/history/',
                                                   'split_adjusted_close': 16.639999,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2024-12-30',
                                                   'eps_source_url': 'https://investor-relations.db.com/files/documents/other-presentations-and-events/2025/Annual-Report-2025.pdf?language_id=1',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 12.145984672},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'diluted_eps': 3.09,
                                                   'source_url': 'https://investor-relations.db.com/files/documents/other-presentations-and-events/2025/Annual-Report-2025.pdf?language_id=1',
                                                   'close': 33.110001,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/DBK.DE/history/',
                                                   'split_adjusted_close': 33.110001,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2025-12-30',
                                                   'eps_source_url': 'https://investor-relations.db.com/files/documents/other-presentations-and-events/2025/Annual-Report-2025.pdf?language_id=1',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 10.71521068}],
                                 'method': 'Median der positiven Geschäftsjahresend-KGVs FY2021–FY2025: letzter Schlusskurs am oder vor dem '
                                           'jeweiligen Geschäftsjahresende ÷ veröffentlichtes Jahres-EPS derselben Aktie in derselben Währung. 5 '
                                           'positive Beobachtungen aus 5 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. IFRS reported diluted EPS attributable to common shareholders, after AT1 coupons. '
                                           'Deutsche Bank AG ordinary registered share, Xetra DBK.DE. Kurse ohne Dividendenbereinigung. Wenn Yahoo '
                                           'nachträglich splitbereinigte Kurse liefert, werden diese anhand der nachfolgenden Splits auf die '
                                           'veröffentlichte EPS-Aktienbasis zurückgeführt. Nachträglich rekonstruierte Jahresend-Stichprobe, kein '
                                           'Durchschnitt täglicher TTM-KGVs und keine rückgetestete Strategie.',
                                 'source_url': 'https://investor-relations.db.com/files/documents/other-presentations-and-events/2025/Annual-Report-2025.pdf?language_id=1',
                                 'price_source_url': 'https://finance.yahoo.com/quote/DBK.DE/history/',
                                 'comparable_to_current': True,
                                 'comparison_note': 'Verwässertes EPS für Stammaktionäre nach AT1-Kupons. Nicht durch gesamten Bankgewinn je Aktie '
                                                    'ersetzt. Rechtskosten belasten insbesondere 2024; die Jahresend-Stichprobe ist keine durch den '
                                                    'Kreditzyklus normalisierte Bewertung.',
                                 'excluded_years': []},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'EUR',
                                  'share_basis': 'Deutsche Bank AG ordinary registered share, Xetra DBK.DE',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:07+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-30',
                                                    'close': 11.018,
                                                    'dividends_per_share': 0.0,
                                                    'dividend_yield': 0.0,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DBK.DE/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 10.588,
                                                    'dividends_per_share': 0.2,
                                                    'dividend_yield': 1.888930812,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DBK.DE/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 12.364,
                                                    'dividends_per_share': 0.3,
                                                    'dividend_yield': 2.426399161,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DBK.DE/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-30',
                                                    'close': 16.639999,
                                                    'dividends_per_share': 0.45,
                                                    'dividend_yield': 2.704327022,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DBK.DE/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-30',
                                                    'close': 33.110001,
                                                    'dividends_per_share': 0.68,
                                                    'dividend_yield': 2.053760155,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DBK.DE/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/DBK.DE/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://www.db.com/news/detail/20261028-deutsche-bank-reports-on-the-third-quarter-results-of-2026?language_id=1',
                       'as_of': '2026-10-28',
                       'facts': {'cet1_ratio_pct': 14.0,
                                 'payout_ratio_pct': 60,
                                 'share_buybacks_completed_2026_billion_eur': 1.5,
                                 'q3_post_tax_profit_billion_eur': 1.9}}},
 {'name': 'BASF',
  'ticker': 'BAS.DE',
  'cluster': 'Chemie',
  'isin': 'DE000BASF111',
  'share_class': 'BASF SE ordinary registered share, Xetra BAS.DE',
  'reference_set': 'classic-2026',
  'hist_pe': 26.85,
  'hist_div': 6.97007,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 12x und Dividendenrendite ≥ 5%',
  'buy_trigger_rationale': 'Eigener Vorschlag, nicht rückgetestet: Chemiezyklus und stark schwankende berichtete Gewinne erfordern einen '
                           'Bewertungsabschlag; die Dividendenhürde ist ein zusätzlicher Einkommenspuffer, keine Zusage zur Dividendensicherheit. '
                           'Eigener Vorschlag, kein nachgewiesenes Optimum und nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.basf.com/global/en/media/news-releases/2026/10/p-26-180', 'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '5J-Median · n=4',
                                 'source_label': 'IFRS · IR / Kurse',
                                 'accounting_basis': 'IFRS',
                                 'currency': 'EUR',
                                 'share_basis': 'BASF SE ordinary registered share, Xetra BAS.DE',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 4,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'basic_eps': 6.01,
                                                   'source_url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                                                   'close': 61.78,
                                                   'price_source_url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                                                   'pe': 10.3,
                                                   'dividend_per_share': 3.4,
                                                   'price_date': '2021-12-30',
                                                   'eps_source_url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                                                   'currency': 'EUR',
                                                   'pe_is_published': True},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'basic_eps': -0.7,
                                                   'source_url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                                                   'close': 46.39,
                                                   'price_source_url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                                                   'pe': None,
                                                   'dividend_per_share': 3.4,
                                                   'price_date': '2022-12-30',
                                                   'eps_source_url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                                                   'currency': 'EUR',
                                                   'pe_is_published': True,
                                                   'exclusion_reason': 'Berichtetes EPS ist nicht positiv; kein sinnvoll positives KGV.'},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'basic_eps': 0.25,
                                                   'source_url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                                                   'close': 48.78,
                                                   'price_source_url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                                                   'pe': 195.1,
                                                   'dividend_per_share': 3.4,
                                                   'price_date': '2023-12-29',
                                                   'eps_source_url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                                                   'currency': 'EUR',
                                                   'pe_is_published': True},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'basic_eps': 1.45,
                                                   'source_url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                                                   'close': 42.46,
                                                   'price_source_url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                                                   'pe': 29.3,
                                                   'dividend_per_share': 2.25,
                                                   'price_date': '2024-12-30',
                                                   'eps_source_url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                                                   'currency': 'EUR',
                                                   'pe_is_published': True},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'basic_eps': 1.82,
                                                   'source_url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                                                   'close': 44.43,
                                                   'price_source_url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                                                   'pe': 24.4,
                                                   'dividend_per_share': 2.25,
                                                   'price_date': '2025-12-30',
                                                   'eps_source_url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                                                   'currency': 'EUR',
                                                   'pe_is_published': True}],
                                 'method': 'Median der direkt vom Emittenten veröffentlichten positiven Jahresend-KGVs FY2021–FY2025. Gerundete EPS '
                                           'dienen nur zur Dokumentation, nicht zur Neuberechnung dieser veröffentlichten Verhältnisse. 4 positive '
                                           'Beobachtungen aus 5 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. IFRS reported EPS, issuer-published year-end P/E; not adjusted EPS. BASF SE ordinary '
                                           'registered share, Xetra BAS.DE. Kurse ohne Dividendenbereinigung. Wenn Yahoo nachträglich '
                                           'splitbereinigte Kurse liefert, werden diese anhand der nachfolgenden Splits auf die veröffentlichte '
                                           'EPS-Aktienbasis zurückgeführt. Nachträglich rekonstruierte Jahresend-Stichprobe, kein Durchschnitt '
                                           'täglicher TTM-KGVs und keine rückgetestete Strategie.',
                                 'source_url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                                 'price_source_url': 'https://www.basf.com/global/en/investors/share-and-adrs/share-overview/key-basf-share-data',
                                 'comparable_to_current': True,
                                 'comparison_note': '2022 war ein Verlustjahr; die Referenz enthält vier positive KGVs innerhalb von fünf Jahren. '
                                                    'Der geringe Gewinn 2023 erzeugt ein KGV von 195,1; dieser Wert bleibt enthalten. Zyklische '
                                                    'Gewinne und Ausschüttungen sowie Sondereffekte beachten.',
                                 'excluded_years': [{'fiscal_year': 2022,
                                                     'reason': 'Berichtetes EPS ist nicht positiv; kein sinnvoll positives KGV.'}]},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'EUR',
                                  'share_basis': 'BASF SE ordinary registered share, Xetra BAS.DE',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:08+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-30',
                                                    'close': 61.779999,
                                                    'dividends_per_share': 3.3,
                                                    'dividend_yield': 5.341534583,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/BAS.DE/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 46.389999,
                                                    'dividends_per_share': 3.4,
                                                    'dividend_yield': 7.329165865,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/BAS.DE/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 48.779999,
                                                    'dividends_per_share': 3.4,
                                                    'dividend_yield': 6.970069875,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/BAS.DE/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-30',
                                                    'close': 42.459999,
                                                    'dividends_per_share': 3.4,
                                                    'dividend_yield': 8.007536678,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/BAS.DE/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-30',
                                                    'close': 44.43,
                                                    'dividends_per_share': 2.25,
                                                    'dividend_yield': 5.064145813,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/BAS.DE/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/BAS.DE/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://www.basf.com/global/en/media/news-releases/2026/10/p-26-180',
                       'as_of': '2026-10-28',
                       'facts': {'q3_sales_billion_eur': 16.4,
                                 'q3_ebitda_before_special_items_billion_eur': 1.6,
                                 'ebitda_before_special_items_guidance_2026_billion_eur': [7.4, 7.8]},
                       'note': 'Raised2026 EBITDA guidancefrom6.9–7.7bn; Q3operatingcashflow1.1bn'}},
 {'name': 'E.ON',
  'ticker': 'EOAN.DE',
  'cluster': 'Versorger',
  'isin': 'DE000ENAG999',
  'share_class': 'E.ON SE ordinary registered share, Xetra EOAN.DE',
  'reference_set': 'classic-2026',
  'hist_pe': 13.334286,
  'hist_div': 4.197531,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 13x und Dividendenrendite ≥ 4%',
  'buy_trigger_rationale': 'Eigener Vorschlag, nicht rückgetestet: E.ONs regulierte Netze stützen langfristig planbarere Erträge; moderates '
                           'Forward-KGV und eine4%-Rendite schaffen Puffer für hohe Investitionen und Zinsrisiken. Historisches IFRS-KGV ist durch '
                           'Bewertungseffekte verzerrbar. Eigener Vorschlag, kein nachgewiesenes Optimum und nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://annualreport.eon.com/content/dam/eon-annualreport/documents/en/GB25-gesamt-EN_final.pdf',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.webdisclosure.fr/press-release/eon-se-etr-eoan-eon-reaffirms-2026-guidance-and-continues-to-invest-in-europes-energy-infrastructure-zgoOr5CAUiN',
                           'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '5J-Median · JE',
                                 'source_label': 'IFRS · IR / Kurse',
                                 'accounting_basis': 'IFRS',
                                 'currency': 'EUR',
                                 'share_basis': 'E.ON SE ordinary registered share, Xetra EOAN.DE',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'diluted_eps': 1.8,
                                                   'source_url': 'https://annualreport.eon.com/content/dam/eon-annualreport/documents/en/GB25-gesamt-EN_final.pdf',
                                                   'close': 12.192,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/EOAN.DE/history/',
                                                   'split_adjusted_close': 12.192,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2021-12-30',
                                                   'eps_source_url': 'https://annualreport.eon.com/content/dam/eon-annualreport/documents/en/GB25-gesamt-EN_final.pdf',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 6.773333333},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'diluted_eps': 0.7,
                                                   'source_url': 'https://annualreport.eon.com/content/dam/eon-annualreport/documents/en/GB25-gesamt-EN_final.pdf',
                                                   'close': 9.334,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/EOAN.DE/history/',
                                                   'split_adjusted_close': 9.334,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2022-12-30',
                                                   'eps_source_url': 'https://annualreport.eon.com/content/dam/eon-annualreport/documents/en/GB25-gesamt-EN_final.pdf',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 13.334285714},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'diluted_eps': 0.2,
                                                   'source_url': 'https://annualreport.eon.com/content/dam/eon-annualreport/documents/en/GB25-gesamt-EN_final.pdf',
                                                   'close': 12.15,
                                                   'price_source_url': 'https://finance.yahoo.com/quote/EOAN.DE/history/',
                                                   'split_adjusted_close': 12.15,
                                                   'subsequent_split_factor': 1,
                                                   'price_date': '2023-12-29',
                                                   'eps_source_url': 'https://annualreport.eon.com/content/dam/eon-annualreport/documents/en/GB25-gesamt-EN_final.pdf',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 60.75},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'diluted_eps': 1.73,
                                                   'source_url': 'https://annualreport.eon.com/content/dam/eon-annualreport/documents/en/GB25-gesamt-EN_final.pdf',
                                                   'close': 11.25,
                                                   'price_source_url': 'https://annualreport.eon.com/content/dam/eon-annualreport/documents/en/GB25-gesamt-EN_final.pdf',
                                                   'pe_reconstructed': 6.502890173410405,
                                                   'price_date': '2024-12-30',
                                                   'eps_source_url': 'https://annualreport.eon.com/content/dam/eon-annualreport/documents/en/GB25-gesamt-EN_final.pdf',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 6.502890173},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'diluted_eps': 0.66,
                                                   'source_url': 'https://annualreport.eon.com/content/dam/eon-annualreport/documents/en/GB25-gesamt-EN_final.pdf',
                                                   'close': 16.13,
                                                   'price_source_url': 'https://annualreport.eon.com/content/dam/eon-annualreport/documents/en/GB25-gesamt-EN_final.pdf',
                                                   'pe_reconstructed': 24.439393939393938,
                                                   'price_date': '2025-12-30',
                                                   'eps_source_url': 'https://annualreport.eon.com/content/dam/eon-annualreport/documents/en/GB25-gesamt-EN_final.pdf',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 24.439393939}],
                                 'method': 'Median der positiven Geschäftsjahresend-KGVs FY2021–FY2025: letzter Schlusskurs am oder vor dem '
                                           'jeweiligen Geschäftsjahresende ÷ veröffentlichtes Jahres-EPS derselben Aktie in derselben Währung. 5 '
                                           'positive Beobachtungen aus 5 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. IFRS reported basic and diluted EPS (no potentially dilutive shares), not adjusted EPS. '
                                           'E.ON SE ordinary registered share, Xetra EOAN.DE. Kurse ohne Dividendenbereinigung. Wenn Yahoo '
                                           'nachträglich splitbereinigte Kurse liefert, werden diese anhand der nachfolgenden Splits auf die '
                                           'veröffentlichte EPS-Aktienbasis zurückgeführt. Nachträglich rekonstruierte Jahresend-Stichprobe, kein '
                                           'Durchschnitt täglicher TTM-KGVs und keine rückgetestete Strategie.',
                                 'source_url': 'https://annualreport.eon.com/content/dam/eon-annualreport/documents/en/GB25-gesamt-EN_final.pdf',
                                 'price_source_url': 'https://annualreport.eon.com/content/dam/eon-annualreport/documents/en/GB25-gesamt-EN_final.pdf',
                                 'comparable_to_current': True,
                                 'comparison_note': 'Berichtetes EPS schwankt stark durch Bewertungs- und Sondereffekte; 2025 unterscheiden sich '
                                                    'berichtetes EPS 0,66 EUR und bereinigtes EPS 1,16 EUR. Forward-KGVs dürfen nicht als identisch '
                                                    'berechnete Fortsetzung des gemeldeten KGVs verstanden werden.',
                                 'excluded_years': []},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'EUR',
                                  'share_basis': 'E.ON SE ordinary registered share, Xetra EOAN.DE',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:08+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-30',
                                                    'close': 12.192,
                                                    'dividends_per_share': 0.47,
                                                    'dividend_yield': 3.854986754,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/EOAN.DE/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 9.334,
                                                    'dividends_per_share': 0.49,
                                                    'dividend_yield': 5.249625233,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/EOAN.DE/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 12.15,
                                                    'dividends_per_share': 0.51,
                                                    'dividend_yield': 4.197530996,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/EOAN.DE/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-30',
                                                    'close': 11.245,
                                                    'dividends_per_share': 0.53,
                                                    'dividend_yield': 4.713205917,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/EOAN.DE/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-30',
                                                    'close': 16.125,
                                                    'dividends_per_share': 0.55,
                                                    'dividend_yield': 3.410852713,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/EOAN.DE/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/EOAN.DE/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://www.webdisclosure.fr/press-release/eon-se-etr-eoan-eon-reaffirms-2026-guidance-and-continues-to-invest-in-europes-energy-infrastructure-zgoOr5CAUiN',
                       'additional_source_url': 'https://live.deutsche-boerse.com/news/EQS-News-EON-reaffirms-2026-guidance-and-continues-to-invest-in-Europes-energy-infrastructure-e8a859fa-a500-418d-b3ff-784e39cb74b9',
                       'issuer_press_index': 'https://www.eon.com/en/about-us/media/press-release/2026.html',
                       'title': 'E.ON reaffirms 2026 guidance and continues to invest in Europe’s energy infrastructure',
                       'as_of': '2026-11-11',
                       'publisher': 'E.ON SE; original issuer announcement distributed by EQS News, reproduced by WebDisclosure and Deutsche Börse',
                       'issuer_responsible_for_content': True,
                       'eqs_news_id': '2432254',
                       'facts': {'nine_month_adjusted_group_ebitda_billion_eur': 7.1,
                                 'nine_month_adjusted_group_net_income_billion_eur': 2.2,
                                 'nine_month_investments_billion_eur': 5.2,
                                 'adjusted_group_ebitda_guidance_2026_billion_eur': [9.4, 9.6],
                                 'adjusted_group_net_income_guidance_2026_billion_eur': [2.7, 2.9],
                                 'network_investment_guidance_2026_billion_eur': 7.0},
                       'note': 'These are adjusted operating guidance/result metrics, not reported IFRS EPS. 2026 adjustments include temporary '
                               'regulatory effects; do not substitute into historical reported-EPS P/E series. Issuer-hosted deep link was not '
                               'retrievable; verified company-authored EQS release used, not a journalist or analyst summary.'}},
 {'name': 'Commerzbank',
  'ticker': 'CBK.DE',
  'cluster': 'Banken',
  'isin': 'DE000CBK1001',
  'share_class': 'Commerzbank AG ordinary bearer share, Xetra CBK.DE, ISINDE000CBK1001',
  'reference_set': 'classic-2026',
  'hist_pe': 8.929293,
  'hist_div': 1.800554,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 9x und Dividendenrendite ≥ 4%',
  'buy_trigger_rationale': 'Eigener Vorschlag, nicht rückgetestet: Zins- und Kreditzyklus sowie Umsetzungsrisiken der Restrukturierung sprechen für '
                           'einen Bewertungs- und Ausschüttungspuffer. HistorischeKGVs2021sinddurchgeringenGewinnverzerrt. Eigener Vorschlag, kein '
                           'nachgewiesenes Optimum und nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://investor-relations.commerzbank.com/media/document/c65deade-2198-45f3-b6c4-07ff5c10df34/assets/DE000CBK1001-JA-2022-EQ-E-00.pdf',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://investor-relations.commerzbank.com/media/document/465882c6-f153-48db-bdb0-5e45dba25056/assets/Commerzbank%20Group_Annual%20Report%202023.pdf?disposition=inline',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://investor-relations.commerzbank.com/media/document/630f424f-282e-4582-9276-a38357e911db/assets/Commerzbank_Group_Annual_Report_2025.pdf?disposition=inline',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://investor-relations.commerzbank.com/media/document/153e5d75-822d-4756-976d-72c8b9d82ece/assets/2026_Q3_Analyst_Presentation.pdf?disposition=inline',
                           'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '5J-Median · JE',
                                 'source_label': 'IFRS · IR / Kurse',
                                 'accounting_basis': 'IFRS',
                                 'currency': 'EUR',
                                 'share_basis': 'Commerzbank AG ordinary bearer share, Xetra CBK.DE, ISINDE000CBK1001',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'diluted_eps': 0.23,
                                                   'source_url': 'https://investor-relations.commerzbank.com/media/document/c65deade-2198-45f3-b6c4-07ff5c10df34/assets/DE000CBK1001-JA-2022-EQ-E-00.pdf',
                                                   'close': 6.69,
                                                   'price_source_url': 'https://investor-relations.commerzbank.com/media/document/c65deade-2198-45f3-b6c4-07ff5c10df34/assets/DE000CBK1001-JA-2022-EQ-E-00.pdf',
                                                   'dividend_per_share': 0,
                                                   'pe_reconstructed': 29.086956521739133,
                                                   'price_date': '2021-12-30',
                                                   'eps_source_url': 'https://investor-relations.commerzbank.com/media/document/c65deade-2198-45f3-b6c4-07ff5c10df34/assets/DE000CBK1001-JA-2022-EQ-E-00.pdf',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 29.086956522},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'diluted_eps': 0.99,
                                                   'source_url': 'https://investor-relations.commerzbank.com/media/document/465882c6-f153-48db-bdb0-5e45dba25056/assets/Commerzbank%20Group_Annual%20Report%202023.pdf?disposition=inline',
                                                   'close': 8.84,
                                                   'price_source_url': 'https://investor-relations.commerzbank.com/media/document/465882c6-f153-48db-bdb0-5e45dba25056/assets/Commerzbank%20Group_Annual%20Report%202023.pdf?disposition=inline',
                                                   'dividend_per_share': 0.2,
                                                   'pe_reconstructed': 8.929292929292929,
                                                   'price_date': '2022-12-30',
                                                   'eps_source_url': 'https://investor-relations.commerzbank.com/media/document/465882c6-f153-48db-bdb0-5e45dba25056/assets/Commerzbank%20Group_Annual%20Report%202023.pdf?disposition=inline',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 8.929292929},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'diluted_eps': 1.63,
                                                   'source_url': 'https://investor-relations.commerzbank.com/media/document/465882c6-f153-48db-bdb0-5e45dba25056/assets/Commerzbank%20Group_Annual%20Report%202023.pdf?disposition=inline',
                                                   'close': 10.76,
                                                   'price_source_url': 'https://investor-relations.commerzbank.com/media/document/465882c6-f153-48db-bdb0-5e45dba25056/assets/Commerzbank%20Group_Annual%20Report%202023.pdf?disposition=inline',
                                                   'dividend_per_share': 0.35,
                                                   'pe_reconstructed': 6.601226993865031,
                                                   'price_date': '2023-12-29',
                                                   'eps_source_url': 'https://investor-relations.commerzbank.com/media/document/465882c6-f153-48db-bdb0-5e45dba25056/assets/Commerzbank%20Group_Annual%20Report%202023.pdf?disposition=inline',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 6.601226994},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'diluted_eps': 2.06,
                                                   'source_url': 'https://investor-relations.commerzbank.com/media/document/630f424f-282e-4582-9276-a38357e911db/assets/Commerzbank_Group_Annual_Report_2025.pdf?disposition=inline',
                                                   'close': 15.73,
                                                   'price_source_url': 'https://investor-relations.commerzbank.com/media/document/630f424f-282e-4582-9276-a38357e911db/assets/Commerzbank_Group_Annual_Report_2025.pdf?disposition=inline',
                                                   'dividend_per_share': 0.65,
                                                   'pe_reconstructed': 7.635922330097087,
                                                   'price_date': '2024-12-30',
                                                   'eps_source_url': 'https://investor-relations.commerzbank.com/media/document/630f424f-282e-4582-9276-a38357e911db/assets/Commerzbank_Group_Annual_Report_2025.pdf?disposition=inline',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 7.63592233},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'diluted_eps': 2.06,
                                                   'source_url': 'https://investor-relations.commerzbank.com/media/document/630f424f-282e-4582-9276-a38357e911db/assets/Commerzbank_Group_Annual_Report_2025.pdf?disposition=inline',
                                                   'close': 36.1,
                                                   'price_source_url': 'https://investor-relations.commerzbank.com/media/document/630f424f-282e-4582-9276-a38357e911db/assets/Commerzbank_Group_Annual_Report_2025.pdf?disposition=inline',
                                                   'dividend_per_share': 1.1,
                                                   'pe_reconstructed': 17.524271844660195,
                                                   'price_date': '2025-12-30',
                                                   'eps_source_url': 'https://investor-relations.commerzbank.com/media/document/630f424f-282e-4582-9276-a38357e911db/assets/Commerzbank_Group_Annual_Report_2025.pdf?disposition=inline',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 17.524271845}],
                                 'method': 'Median der positiven Geschäftsjahresend-KGVs FY2021–FY2025: letzter Schlusskurs am oder vor dem '
                                           'jeweiligen Geschäftsjahresende ÷ veröffentlichtes Jahres-EPS derselben Aktie in derselben Währung. 5 '
                                           'positive Beobachtungen aus 5 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. IFRS IAS33 reported basic=diluted EPS, common shareholders after AT1 coupons; '
                                           'FY2024latestrestated2.06. Commerzbank AG ordinary bearer share, Xetra CBK.DE, ISINDE000CBK1001. Kurse '
                                           'ohne Dividendenbereinigung. Wenn Yahoo nachträglich splitbereinigte Kurse liefert, werden diese anhand '
                                           'der nachfolgenden Splits auf die veröffentlichte EPS-Aktienbasis zurückgeführt. Nachträglich '
                                           'rekonstruierte Jahresend-Stichprobe, kein Durchschnitt täglicher TTM-KGVs und keine rückgetestete '
                                           'Strategie.',
                                 'source_url': 'https://investor-relations.commerzbank.com/media/document/630f424f-282e-4582-9276-a38357e911db/assets/Commerzbank_Group_Annual_Report_2025.pdf?disposition=inline',
                                 'price_source_url': 'https://investor-relations.commerzbank.com/media/document/630f424f-282e-4582-9276-a38357e911db/assets/Commerzbank_Group_Annual_Report_2025.pdf?disposition=inline',
                                 'comparable_to_current': True,
                                 'comparison_note': 'Berichtetes IAS-33-EPS nach AT1-Kupons. Für 2024 gilt der aktuell rückwirkend angepasste Wert '
                                                    '2,06 EUR statt 2,08 EUR. Die fünf Jahresendwerte sind keine gleitenden TTM-Durchschnittswerte.',
                                 'excluded_years': []},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'EUR',
                                  'share_basis': 'Commerzbank AG ordinary bearer share, Xetra CBK.DE, ISINDE000CBK1001',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:10+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-30',
                                                    'close': 6.689,
                                                    'dividends_per_share': 0.0,
                                                    'dividend_yield': 0.0,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/CBK.DE/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 8.836,
                                                    'dividends_per_share': 0.0,
                                                    'dividend_yield': 0.0,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/CBK.DE/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 10.76,
                                                    'dividends_per_share': 0.2,
                                                    'dividend_yield': 1.85873602,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/CBK.DE/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-30',
                                                    'close': 15.725,
                                                    'dividends_per_share': 0.35,
                                                    'dividend_yield': 2.225755113,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/CBK.DE/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-30',
                                                    'close': 36.099998,
                                                    'dividends_per_share': 0.65,
                                                    'dividend_yield': 1.800554093,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/CBK.DE/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/CBK.DE/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://investor-relations.commerzbank.com/media/document/153e5d75-822d-4756-976d-72c8b9d82ece/assets/2026_Q3_Analyst_Presentation.pdf?disposition=inline',
                       'release_url': 'https://investor-relations.commerzbank.com/news/commerzbank-erzielt-bestes-neunmonatsergebnis-der-konzerngeschichte/2a0cfd89-4a45-442a-9554-265b67997d7e',
                       'alternative_release_url': 'https://www.commerzbank.de/group/newsroom/press-releases/best-nine-month-result-in-history.html',
                       'title': 'Q3 2026 Analyst Presentation — key targets for2026to2030confirmed',
                       'as_of': '2026-11-05',
                       'source_pages': 'Printedp1/PDFp2summary; printedp12/PDFp13Q3metrics',
                       'facts': {'nine_month_net_result_million_eur': 2669,
                                 'nine_month_net_rote_pct': 12.4,
                                 'q3_net_result_million_eur': 859,
                                 'q3_net_rote_pct': 11.9,
                                 'cet1_ratio_pct': 14.2,
                                 'net_result_guidance_2026_minimum_billion_eur': 3.4,
                                 'net_rote_guidance_2026_approx_pct': 12,
                                 'share_buyback_ongoing_billion_eur': 1.2,
                                 'payout_target_pct': 100},
                       'note': '100% payout target applies to net result afterAT1coupons and before extraordinarynonrecurringitems until '
                               'CET1target13.5% is reached; includes sharebuybacks and is not equivalent to cashdividendyield. NetRoTE is not total '
                               'shareholder ROE. PDF downloaded and directly parsed locally.'}},
 {'name': 'Daimler Truck',
  'ticker': 'DTG.DE',
  'cluster': 'Automobil & Nutzfahrzeuge',
  'isin': 'DE000DTR0CK8',
  'share_class': 'Registered ordinary share after Daimler spin-off; standalone listed from December2021',
  'reference_set': 'classic-2026',
  'hist_pe': 10.123626,
  'hist_div': 4.456193,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 10x und Div.-Rendite ≥ 4,5%',
  'buy_trigger_rationale': 'Vorschlag: für den Lkw-Zyklus einen moderaten künftigen Gewinnmultiplikator mit laufender Ausschüttung kombinieren. '
                           'Ergebnisverbesserung und Zahlungsfähigkeit bleiben entscheidend; Schwellen kein rückgetestetes Optimum. Eigener '
                           'Vorschlag, kein nachgewiesenes Optimum und nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/reports/annual-reports/2022/daimler-truck-ir-factbook-fy-2022.pdf',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/factbooks/daimler-truck-ir-factbook-fy-2023.pdf',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/reports/annual-reports/2025/daimler-truck-ir-factbook-fy-2025.pdf',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://www.daimlertruck.com/en/newsroom/pressrelease/daimler-truck-profitability-rebounds-as-markets-recover-53654175',
                           'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '5J-Median · JE',
                                 'source_label': 'IFRS · IR / Kurse',
                                 'accounting_basis': 'IFRS',
                                 'currency': 'EUR',
                                 'share_basis': 'Registered ordinary share after Daimler spin-off; standalone listed from December2021',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'diluted_eps': 2.85,
                                                   'close': 32.29,
                                                   'pe': 11.329824561,
                                                   'pe_is_published': False,
                                                   'dividend_per_share': None,
                                                   'source_url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/reports/annual-reports/2022/daimler-truck-ir-factbook-fy-2022.pdf',
                                                   'price_source_url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/reports/annual-reports/2022/daimler-truck-ir-factbook-fy-2022.pdf',
                                                   'price_date': '2021-12-30',
                                                   'eps_source_url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/reports/annual-reports/2022/daimler-truck-ir-factbook-fy-2022.pdf',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'diluted_eps': 3.24,
                                                   'close': 28.95,
                                                   'pe': 8.935185185,
                                                   'pe_is_published': False,
                                                   'dividend_per_share': 1.3,
                                                   'source_url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/factbooks/daimler-truck-ir-factbook-fy-2023.pdf',
                                                   'price_source_url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/factbooks/daimler-truck-ir-factbook-fy-2023.pdf',
                                                   'price_date': '2022-12-30',
                                                   'eps_source_url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/factbooks/daimler-truck-ir-factbook-fy-2023.pdf',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'diluted_eps': 4.62,
                                                   'close': 34.02,
                                                   'pe': 7.363636364,
                                                   'pe_is_published': False,
                                                   'dividend_per_share': 1.9,
                                                   'source_url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/factbooks/daimler-truck-ir-factbook-fy-2023.pdf',
                                                   'price_source_url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/factbooks/daimler-truck-ir-factbook-fy-2023.pdf',
                                                   'price_date': '2023-12-29',
                                                   'eps_source_url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/factbooks/daimler-truck-ir-factbook-fy-2023.pdf',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'diluted_eps': 3.64,
                                                   'close': 36.85,
                                                   'pe': 10.123626374,
                                                   'pe_is_published': False,
                                                   'dividend_per_share': 1.9,
                                                   'source_url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/reports/annual-reports/2025/daimler-truck-ir-factbook-fy-2025.pdf',
                                                   'price_source_url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/reports/annual-reports/2025/daimler-truck-ir-factbook-fy-2025.pdf',
                                                   'price_date': '2024-12-30',
                                                   'eps_source_url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/reports/annual-reports/2025/daimler-truck-ir-factbook-fy-2025.pdf',
                                                   'currency': 'EUR'},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'diluted_eps': 2.56,
                                                   'close': 37.32,
                                                   'pe': 14.578125,
                                                   'pe_is_published': False,
                                                   'dividend_per_share': 1.9,
                                                   'source_url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/reports/annual-reports/2025/daimler-truck-ir-factbook-fy-2025.pdf',
                                                   'price_source_url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/reports/annual-reports/2025/daimler-truck-ir-factbook-fy-2025.pdf',
                                                   'price_date': '2025-12-30',
                                                   'eps_source_url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/reports/annual-reports/2025/daimler-truck-ir-factbook-fy-2025.pdf',
                                                   'currency': 'EUR'}],
                                 'method': 'Median der positiven Geschäftsjahresend-KGVs FY2021–FY2025: letzter Schlusskurs am oder vor dem '
                                           'jeweiligen Geschäftsjahresende ÷ veröffentlichtes Jahres-EPS derselben Aktie in derselben Währung. 5 '
                                           'positive Beobachtungen aus 5 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. IFRS reported diluted EPS, exact listed share class; no adjusted earnings. Registered '
                                           'ordinary share after Daimler spin-off; standalone listed from December2021. Kurse ohne '
                                           'Dividendenbereinigung. Wenn Yahoo nachträglich splitbereinigte Kurse liefert, werden diese anhand der '
                                           'nachfolgenden Splits auf die veröffentlichte EPS-Aktienbasis zurückgeführt. Nachträglich rekonstruierte '
                                           'Jahresend-Stichprobe, kein Durchschnitt täglicher TTM-KGVs und keine rückgetestete Strategie.',
                                 'source_url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/reports/annual-reports/2025/daimler-truck-ir-factbook-fy-2025.pdf',
                                 'price_source_url': 'https://www.daimlertruck.com/fileadmin/user_upload/documents/investors/reports/annual-reports/2025/daimler-truck-ir-factbook-fy-2025.pdf',
                                 'comparable_to_current': False,
                                 'comparison_note': 'Börsenhandel erst seit Dezember 2021; der erste Jahresendkurs ist mit dem berichteten '
                                                    'Jahresergebnis kombiniert und keine volle Börsenjahres-Historie. 2024/2025 enthalten '
                                                    'aufgegebene Aktivitäten; die Fuso-Entkonsolidierung 2026 verändert den Konzernumfang. '
                                                    'Historischer Abstand ist deshalb kein direkt vergleichbares Bewertungssignal.',
                                 'excluded_years': []},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 4J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'EUR',
                                  'share_basis': 'Registered ordinary share after Daimler spin-off; standalone listed from December2021',
                                  'period': 'FY2022–FY2025',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:09+01:00',
                                  'sample_count': 4,
                                  'observations': [{'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 28.945,
                                                    'dividends_per_share': 0.0,
                                                    'dividend_yield': 0.0,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DTG.DE/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 34.02,
                                                    'dividends_per_share': 1.3,
                                                    'dividend_yield': 3.821281548,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DTG.DE/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-30',
                                                    'close': 36.849998,
                                                    'dividends_per_share': 1.9,
                                                    'dividend_yield': 5.156038205,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DTG.DE/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-30',
                                                    'close': 37.32,
                                                    'dividends_per_share': 1.9,
                                                    'dividend_yield': 5.091104007,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DTG.DE/history/'}],
                                  'excluded_years': [{'fiscal_year': 2021,
                                                      'reason': 'Noch keine vollständige zwölfmonatige Börsenhistorie vor dem Jahresendkurs.'}],
                                  'source_url': 'https://finance.yahoo.com/quote/DTG.DE/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://www.daimlertruck.com/en/newsroom/pressrelease/daimler-truck-profitability-rebounds-as-markets-recover-53654175',
                       'as_of': '2026-11-06',
                       'facts': {'q3_industrial_business_revenue_eur_billion': 11.6, 'q3_adjusted_industrial_return_on_sales_percent': 9.1}}},
 {'name': 'DHL Group',
  'ticker': 'DHL.DE',
  'cluster': 'Industrie & Logistik',
  'isin': 'DE0005552004',
  'share_class': 'DHL AG ordinary registered share, Xetra DHL.DE; formerly Deutsche Post AG',
  'reference_set': 'classic-2026',
  'hist_pe': 14.099751,
  'hist_div': 4.124401,
  'hist_ps': None,
  'buy_trigger': 'Forward P/E ≤ 13x und Dividendenrendite ≥ 4,5%',
  'buy_trigger_rationale': 'Eigener Vorschlag, nicht rückgetestet: Globale Logistik bleibt konjunkturabhängig; ein niedriges '
                           'zweistelligesForward-KGV und4,5%Dividendenrendite sollen einen Puffer für Volumen- und Margenschwankungen schaffen. '
                           'Eigener Vorschlag, kein nachgewiesenes Optimum und nicht rückgetestet.',
  'buy_trigger_sources': [{'url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                           'label': 'Geschäftsbericht / Aktienkennzahlen'},
                          {'url': 'https://reporting-hub.group.dhl.com/2026-q3/en/', 'label': 'Aktuelle Unternehmensangaben'}],
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'fact_type': 'issuer_year_end_pe_median',
                                 'label': '5J-Median · JE',
                                 'source_label': 'IFRS · IR / Kurse',
                                 'accounting_basis': 'IFRS',
                                 'currency': 'EUR',
                                 'share_basis': 'DHL AG ordinary registered share, Xetra DHL.DE; formerly Deutsche Post AG',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-18',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'diluted_eps': 4.01,
                                                   'source_url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                                                   'close': 56.54,
                                                   'price_source_url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                                                   'dividend_per_share': 1.8,
                                                   'pe_reconstructed': 14.099750623441397,
                                                   'price_date': '2021-12-30',
                                                   'eps_source_url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 14.099750623},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'diluted_eps': 4.33,
                                                   'source_url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                                                   'close': 35.18,
                                                   'price_source_url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                                                   'dividend_per_share': 1.85,
                                                   'pe_reconstructed': 8.124711316397228,
                                                   'price_date': '2022-12-30',
                                                   'eps_source_url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 8.124711316},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'diluted_eps': 3.04,
                                                   'source_url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                                                   'close': 44.86,
                                                   'price_source_url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                                                   'dividend_per_share': 1.85,
                                                   'pe_reconstructed': 14.756578947368421,
                                                   'price_date': '2023-12-29',
                                                   'eps_source_url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 14.756578947},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'diluted_eps': 2.81,
                                                   'source_url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                                                   'close': 33.98,
                                                   'price_source_url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                                                   'dividend_per_share': 1.85,
                                                   'pe_reconstructed': 12.092526690391457,
                                                   'price_date': '2024-12-30',
                                                   'eps_source_url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 12.09252669},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'diluted_eps': 3.04,
                                                   'source_url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                                                   'close': 46.73,
                                                   'price_source_url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                                                   'dividend_per_share': 1.9,
                                                   'pe_reconstructed': 15.371710526315788,
                                                   'price_date': '2025-12-30',
                                                   'eps_source_url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                                                   'currency': 'EUR',
                                                   'pe_is_published': False,
                                                   'pe': 15.371710526}],
                                 'method': 'Median der positiven Geschäftsjahresend-KGVs FY2021–FY2025: letzter Schlusskurs am oder vor dem '
                                           'jeweiligen Geschäftsjahresende ÷ veröffentlichtes Jahres-EPS derselben Aktie in derselben Währung. 5 '
                                           'positive Beobachtungen aus 5 Jahren; Verlustjahre bleiben dokumentiert und sind vom positiven Median '
                                           'ausgeschlossen. IFRS reported diluted EPS, restated five-year comparative series from2025AnnualReport. '
                                           'DHL AG ordinary registered share, Xetra DHL.DE; formerly Deutsche Post AG. Kurse ohne '
                                           'Dividendenbereinigung. Wenn Yahoo nachträglich splitbereinigte Kurse liefert, werden diese anhand der '
                                           'nachfolgenden Splits auf die veröffentlichte EPS-Aktienbasis zurückgeführt. Nachträglich rekonstruierte '
                                           'Jahresend-Stichprobe, kein Durchschnitt täglicher TTM-KGVs und keine rückgetestete Strategie.',
                                 'source_url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                                 'price_source_url': 'https://reporting-hub.group.dhl.com/2025-fy/en/the-company/key-figures/',
                                 'comparable_to_current': True,
                                 'comparison_note': 'Verwässertes EPS aus der rückwirkend angepassten Fünfjahresübersicht, nicht das höhere '
                                                    'unverwässerte EPS. Logistiknachfrage, Frachtzyklus und Kostenentwicklung beeinflussen die '
                                                    'Vergleichbarkeit mit aktuellen Analystenschätzungen.',
                                 'excluded_years': []},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance · historische Barausschüttungen',
                                  'fact_type': 'historical_cash_yield_median',
                                  'label': 'Hist. Div. 5J',
                                  'source_label': 'Cash-Historie · Yahoo',
                                  'accounting_basis': '12M-Barausschüttungen / Schlusskurs',
                                  'currency': 'EUR',
                                  'share_basis': 'DHL AG ordinary registered share, Xetra DHL.DE; formerly Deutsche Post AG',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-18',
                                  'retrieved_at': '2026-11-18T14:41:10+01:00',
                                  'sample_count': 5,
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-30',
                                                    'close': 56.540001,
                                                    'dividends_per_share': 1.35,
                                                    'dividend_yield': 2.387690092,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DHL.DE/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 35.18,
                                                    'dividends_per_share': 1.8,
                                                    'dividend_yield': 5.116543446,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DHL.DE/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 44.855,
                                                    'dividends_per_share': 1.85,
                                                    'dividend_yield': 4.124400889,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DHL.DE/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-30',
                                                    'close': 33.919998,
                                                    'dividends_per_share': 1.85,
                                                    'dividend_yield': 5.454009728,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DHL.DE/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-30',
                                                    'close': 46.73,
                                                    'dividends_per_share': 1.85,
                                                    'dividend_yield': 3.958912943,
                                                    'currency': 'EUR',
                                                    'share_basis': 'Kurs und Barausschüttungen einheitlich splitbereinigt auf dieselbe Aktienbasis',
                                                    'source_url': 'https://finance.yahoo.com/quote/DHL.DE/history/'}],
                                  'excluded_years': [],
                                  'source_url': 'https://finance.yahoo.com/quote/DHL.DE/history/',
                                  'method': 'Median der tatsächlichen Barausschüttungen einschließlich gemeldeter Sonderdividenden in den jeweils '
                                            'zwölf Monaten vor dem Geschäftsjahresendkurs ÷ diesen Schlusskurs ×100. Offene linke Datumsgrenze, '
                                            'einheitliche splitbereinigte Aktienbasis, keine Wiederanlage in dieser Renditekennzahl. Echte '
                                            'Nullzahlungen bleiben Null. Unvollständige erste Börsenjahre werden ausgeschlossen. Aktuelle '
                                            'Dividendenrenditen des Kursanbieters können eine annualisierte zuletzt bekannte Rate verwenden; ihre '
                                            'Basis kann von dieser historischen Cash-Rendite abweichen.'}},
  'primary_research': {'source_url': 'https://reporting-hub.group.dhl.com/2026-q3/en/',
                       'as_of': '2026-Q3',
                       'facts': {'q3_revenue_million_eur': 22658,
                                 'q3_ebit_million_eur': 1716,
                                 'q3_free_cash_flow_million_eur': 901,
                                 'q3_basic_eps_eur': 0.82},
                       'report_pdf': 'https://reporting-hub.group.dhl.com/ecomaXL/files/DHL-Group-Quarterly-Statement-Q3-2026.pdf'}}]

CLASSIC_CLUSTER_ORDER = ['Konsum', 'Versicherungen', 'Technologie', 'Automobil & Nutzfahrzeuge',
                         'Gesundheit', 'Industrie & Logistik', 'Telekommunikation', 'Banken', 'Chemie', 'Versorger']
PORTFOLIOS.append(dict(id='classic', name='A24', valuation_style='income', tranche_policy='individual_only',
                      cluster_order=CLASSIC_CLUSTER_ORDER,
                      stocks=[dict(deepcopy(ref), initial={field: None for field in NUMERIC_FIELDS})
                              for ref in CLASSIC_REFERENCES]))

# A research inventory is deliberately separate from the three user portfolios.
# Issuer-reviewed seed records are populated here before publishing this data.
RESERVE_REFERENCES: list[dict[str, Any]] = [{'name': 'Rio Tinto plc',
  'ticker': 'RIO',
  'isin': 'US7672041008',
  'exchange': 'NYSE',
  'currency': 'USD',
  'quote_currency': 'USD',
  'financial_currency': 'USD',
  'asset_type': 'stock',
  'instrument_subtype': 'mining_ads',
  'cluster': 'Rohstoffe / Bergbau',
  'share_basis': '1 NYSE ADS represents 1 Rio Tinto plc ordinary share since 2010-04-30; earlier ADR ratio was 1:4. Do not '
                 'use Rio Tinto Limited/Australian share prices.',
  'identity_sources': ['https://www.riotinto.com/en/invest/shareholder-information/shareholder-faqs',
                       'https://company-announcements.afr.com/asx/rio/67046b18-0146-11f1-895b-3ec68c5b4fc7.pdf'],
  'hist_pe': 9.363492497138635,
  'hist_ps': None,
  'hist_ps_not_used': {'status': 'not_used_not_required_for_primary_valuation',
                       'checked_source_url': 'https://www.sec.gov/Archives/edgar/data/887028/000162828026009531/R11.htm',
                       'checked_source_scope': 'FY2025 annual report EPS note R11, including combined plc+Limited weighted '
                                               'shares and dilution.',
                       'available_years': [2023, 2024, 2025],
                       'diluted_weighted_combined_shares_million': [1631.5, 1633.4, 1638.0],
                       'basis_note': 'Combined weighted plc and Limited shares plus dilutive awards, consistent with '
                                     'consolidated revenues; plc-only sharesOutstanding would misstate the denominator.',
                       'remaining_requirement': '2021/2022 combined diluted weighted shares were not in the checked 2025 '
                                                'note. No five-year reference was created from mixed denominators or a '
                                                'three-observation sample.',
                       'valuation_choice': 'Reserve primary valuation uses P/E with operating/free cashflow and cyclical '
                                           'dividend coverage; historical P/S is supplemental, not required by the '
                                           'individual buy proposal.'},
  'primary_valuation': 'pe_and_cashflow',
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'label': '5J-Median · JE',
                                 'source_label': 'IFRS · IR / Kurse',
                                 'accounting_basis': 'IFRS diluted EPS',
                                 'currency': 'USD',
                                 'share_basis': '1 ADS = 1 ordinary share',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-21',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'price_date': '2021-12-31',
                                                   'close': 66.94000244140625,
                                                   'currency': 'USD',
                                                   'source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026009531/rio-20251231.htm',
                                                   'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026009531/rio-20251231.htm',
                                                   'price_source_url': 'https://finance.yahoo.com/quote/RIO/history/',
                                                   'share_basis': '1 ADS = 1 ordinary share',
                                                   'diluted_eps': 12.963,
                                                   'pe': 5.1639282913990785},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'price_date': '2022-12-30',
                                                   'close': 71.19999694824219,
                                                   'currency': 'USD',
                                                   'source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026009531/rio-20251231.htm',
                                                   'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026009531/rio-20251231.htm',
                                                   'price_source_url': 'https://finance.yahoo.com/quote/RIO/history/',
                                                   'share_basis': '1 ADS = 1 ordinary share',
                                                   'diluted_eps': 7.604,
                                                   'pe': 9.363492497138635},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'price_date': '2023-12-29',
                                                   'close': 74.45999908447266,
                                                   'currency': 'USD',
                                                   'source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026009531/rio-20251231.htm',
                                                   'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026009531/rio-20251231.htm',
                                                   'price_source_url': 'https://finance.yahoo.com/quote/RIO/history/',
                                                   'share_basis': '1 ADS = 1 ordinary share',
                                                   'diluted_eps': 6.165,
                                                   'pe': 12.077858732274558},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'price_date': '2024-12-31',
                                                   'close': 58.810001373291016,
                                                   'currency': 'USD',
                                                   'source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026009531/rio-20251231.htm',
                                                   'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026009531/rio-20251231.htm',
                                                   'price_source_url': 'https://finance.yahoo.com/quote/RIO/history/',
                                                   'share_basis': '1 ADS = 1 ordinary share',
                                                   'diluted_eps': 7.072,
                                                   'pe': 8.315893859345449},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'price_date': '2025-12-31',
                                                   'close': 80.02999877929688,
                                                   'currency': 'USD',
                                                   'source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026009531/rio-20251231.htm',
                                                   'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026009531/rio-20251231.htm',
                                                   'price_source_url': 'https://finance.yahoo.com/quote/RIO/history/',
                                                   'share_basis': '1 ADS = 1 ordinary share',
                                                   'diluted_eps': 6.084,
                                                   'pe': 13.154174684302577}],
                                 'source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026009531/rio-20251231.htm',
                                 'method': 'Letzter Handelsschluss am oder vor dem 31. Dezember geteilt durch das '
                                           'berichtete verwässerte EPS desselben Geschäftsjahres; Median der fünf positiven '
                                           'Einzelwerte. Keine kursbereinigten Total-Return-Kurse. Jahresergebnis wurde '
                                           'erst nach dem Jahresende publiziert; rückblickende Bewertungsreferenz, keine '
                                           'handelbare zeitpunktgerechte Backtestreihe. ',
                                 'comparable_to_current': True,
                                 'comparison_note': 'Diversifizierter Qualitäts-Rohstoffkonzern, aber variable/kürzbare '
                                                    'Dividende; kein stetiger Dividendenwachstumswert.'},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance / datierte Emittentenprüfung',
                                  'label': 'Hist. Div. 5J',
                                  'accounting_basis': 'Ex-Tag-Barausschüttungen',
                                  'currency': 'USD',
                                  'share_basis': '1 NYSE ADS represents 1 Rio Tinto plc ordinary share since 2010-04-30; '
                                                 'earlier ADR ratio was 1:4. Do not use Rio Tinto Limited/Australian share '
                                                 'prices.',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-21',
                                  'sample_count': 5,
                                  'window_count': 5,
                                  'source_url': 'https://finance.yahoo.com/quote/RIO/history/',
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-31',
                                                    'close': 66.94000244140625,
                                                    'dividends_per_share': 9.63,
                                                    'dividend_yield': 14.386016804270819,
                                                    'currency': 'USD',
                                                    'share_basis': '1 NYSE ADS represents 1 Rio Tinto plc ordinary share '
                                                                   'since 2010-04-30; earlier ADR ratio was 1:4. Do not use '
                                                                   'Rio Tinto Limited/Australian share prices.',
                                                    'source_url': 'https://finance.yahoo.com/quote/RIO/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 71.19999694824219,
                                                    'dividends_per_share': 7.46,
                                                    'dividend_yield': 10.477528538973027,
                                                    'currency': 'USD',
                                                    'share_basis': '1 NYSE ADS represents 1 Rio Tinto plc ordinary share '
                                                                   'since 2010-04-30; earlier ADR ratio was 1:4. Do not use '
                                                                   'Rio Tinto Limited/Australian share prices.',
                                                    'source_url': 'https://finance.yahoo.com/quote/RIO/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 74.45999908447266,
                                                    'dividends_per_share': 4.02,
                                                    'dividend_yield': 5.3988719439002795,
                                                    'currency': 'USD',
                                                    'share_basis': '1 NYSE ADS represents 1 Rio Tinto plc ordinary share '
                                                                   'since 2010-04-30; earlier ADR ratio was 1:4. Do not use '
                                                                   'Rio Tinto Limited/Australian share prices.',
                                                    'source_url': 'https://finance.yahoo.com/quote/RIO/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-31',
                                                    'close': 58.810001373291016,
                                                    'dividends_per_share': 4.35,
                                                    'dividend_yield': 7.396701068562776,
                                                    'currency': 'USD',
                                                    'share_basis': '1 NYSE ADS represents 1 Rio Tinto plc ordinary share '
                                                                   'since 2010-04-30; earlier ADR ratio was 1:4. Do not use '
                                                                   'Rio Tinto Limited/Australian share prices.',
                                                    'source_url': 'https://finance.yahoo.com/quote/RIO/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-31',
                                                    'close': 80.02999877929688,
                                                    'dividends_per_share': 3.73,
                                                    'dividend_yield': 4.660752289009058,
                                                    'currency': 'USD',
                                                    'share_basis': '1 NYSE ADS represents 1 Rio Tinto plc ordinary share '
                                                                   'since 2010-04-30; earlier ADR ratio was 1:4. Do not use '
                                                                   'Rio Tinto Limited/Australian share prices.',
                                                    'source_url': 'https://finance.yahoo.com/quote/RIO/history/'}],
                                  'method': 'Median der tatsächlichen 12M-Cash-Ausschüttungen nach Ex-Tag bis zum letzten '
                                            'Jahreshandelsschluss geteilt durch diesen Kurs. Gemeldete Cash-Sonderzahlungen '
                                            'enthalten; keine künftige Zusage. Bei O ist Orion-Sachdividende ausgeschlossen '
                                            'und die synthetische 1,032-Rückrechnung aufgehoben.'},
                     'ps_ratio': {'provider': 'Yahoo Finance',
                                  'source_url': 'https://finance.yahoo.com/quote/RIO/key-statistics/',
                                  'role': 'informational_only',
                                  'note': 'Kein primärer Kaufmaßstab für diesen Reservewert; historische Vergleichsbasis '
                                          'nicht angesetzt. Sektorpassende Kennzahlen und Methoden stehen in '
                                          'primary_valuation/valuation_references.'}},
  'buy_trigger': 'Forward P/E ≤ 10x und reguläre Dividendenrendite ≥ 6%',
  'buy_trigger_type': 'proposal_not_backtested',
  'buy_trigger_rationale': 'Vorschlag: Bewertungsabschlag und laufende Ertragskompensation für Rohstoff- und '
                           'Investitionszyklus kombinieren. KGV kann am Gewinnhoch künstlich günstig sein; 10x/6% sind '
                           'eigene Einstiegsschwellen, keine ermittelte optimale Strategie.',
  'buy_trigger_evaluation_policy': 'Kein automatisches positives Signal bei unklarer Forward-EPS-/ADS-Basis, veralteten '
                                   'Daten oder fehlenden regulären Dividenden. Sonderdividenden aus Yield-Bedingung '
                                   'ausschließen.',
  'buy_trigger_sources': [{'url': 'https://www.sec.gov/Archives/edgar/data/887028/000162828026009531/R11.htm',
                           'label': 'Emittentenbericht / aktuelle Kennzahlen'},
                          {'url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026050377/rio-20260630_d2.htm',
                           'label': 'Emittentenbericht / aktuelle Kennzahlen'}],
  'classification_note': 'Diversifizierter Qualitäts-Rohstoffkonzern, aber variable/kürzbare Dividende; kein stetiger '
                         'Dividendenwachstumswert.',
  'current_primary_ttm': {'diluted_eps': 7.381999999999998,
                          'currency': 'USD',
                          'per': '1 ADS / 1 ordinary share',
                          'period_end': '2026-06-30',
                          'formula': 'FY2025 diluted EPS6.084 + H12026 diluted EPS4.063 − H12025 diluted EPS2.765',
                          'source_urls': ['https://www.sec.gov/Archives/edgar/data/887028/000162828026009531/R11.htm',
                                          'https://www.sec.gov/Archives/edgar/data/863064/000162828026050377/rio-20260630_d2.htm'],
                          'source_pages': 'H1 2026 Form6K p28, FY2025 EPS note',
                          'price_date': '2026-11-20',
                          'price': 72.73,
                          'pe': 9.852343538336498,
                          'caveat': 'Synthetic TTM sum/difference of rounded published diluted EPS; not basic underlying '
                                    'EPS421.4c.'},
  'dividend_policy': {'regular_fy2025': {'value': 4.02,
                                         'unit': 'USD/ADS',
                                         'period': 'FY2025 declared',
                                         'source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026009531/rio-20251231.htm'},
                      'latest_regular': {'value': 2.11,
                                         'unit': 'USD/ADS',
                                         'period': '2026 interim',
                                         'source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026050377/rio-20260630_d2.htm',
                                         'note': 'Paid2026-09-24; do not double as guaranteed full-year payout.'},
                      'trailing_official_gross_cash': {'value': 4.65,
                                                       'currency': 'USD',
                                                       'period': '12 months to2026-11-20',
                                                       'components': [{'value': 2.54,
                                                                       'ex_date': '2026-03-06',
                                                                       'payment_date': '2026-04-16',
                                                                       'type': 'FY2025 final ordinary'},
                                                                      {'value': 2.11,
                                                                       'ex_date': '2026-08-14',
                                                                       'payment_date': '2026-09-24',
                                                                       'type': '2026 interim ordinary'}],
                                                       'source_urls': ['https://www.sec.gov/Archives/edgar/data/863064/000162828026009531/rio-20251231.htm',
                                                                       'https://www.sec.gov/Archives/edgar/data/863064/000162828026050377/rio-20260630_d2.htm'],
                                                       'note': 'Gross issuer cash before depositary fees and investor '
                                                               'taxes; vendor cash may reflect fees.'},
                      'special_history_example': {'fiscal_year': 2021,
                                                  'ordinary': 7.93,
                                                  'special': 2.47,
                                                  'total': 10.4,
                                                  'currency': 'USD/ADS',
                                                  'source_url': 'https://www.sec.gov/Archives/edgar/data/887028/000162828022003937/rio-20211231.htm'},
                      'projection_note': 'Zyklische ordentliche Dividenden und Sonderzahlungen getrennt halten. '
                                         'Historisches Gesamt-Cash-CAGR ist rückblickend; Sonderdividenden nicht als '
                                         'regelmäßige Wachstumsbasis fortschreiben.'},
  'issuer_snapshot': {'as_of': '2026-06-30',
                      'verified_at': '2026-11-21',
                      'currency': 'USD',
                      'metrics': {'operating_cashflow': {'value': 9173,
                                                         'unit': 'USD million',
                                                         'period': 'H1 2026',
                                                         'source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026050377/rio-20260630_d2.htm'},
                                  'free_cashflow': {'value': 3834,
                                                    'unit': 'USD million',
                                                    'period': 'H1 2026',
                                                    'source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026050377/rio-20260630_d2.htm',
                                                    'note': 'New definition: operating cashflow less Rio share of capital '
                                                            'investment and lease principal; capital investment5037m, '
                                                            'lease302m.'},
                                  'net_debt': {'value': 14061,
                                               'unit': 'USD million',
                                               'period': '2026-06-30',
                                               'source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026050377/rio-20260630_d2.htm'},
                                  'underlying_earnings': {'value': 6851,
                                                          'unit': 'USD million',
                                                          'period': 'H1 2026',
                                                          'source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026050377/rio-20260630_d2.htm'},
                                  'interim_dividend_payout': {'value': 50,
                                                              'unit': '% of underlying EPS',
                                                              'period': 'H1 2026',
                                                              'source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026050377/rio-20260630_d2.htm'},
                                  'dividend_policy': {'range_pct': [40, 60],
                                                      'basis': 'Underlying earnings, averaged across cycle; special '
                                                               'dividends also enter payout numerator. Variable policy, no '
                                                               'fixed growth promise.',
                                                      'source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026050377/rio-20260630_d2.htm'},
                                  'fy2025_fcf': {'value': 4025,
                                                 'unit': 'USD million',
                                                 'period': 'FY2025',
                                                 'source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026009503/ye25-pressreleasex19feb.htm'},
                                  'fy2025_declared_ordinary_dividends': {'value': 6500,
                                                                         'unit': 'USD million approx',
                                                                         'period': 'FY2025',
                                                                         'source_url': 'https://www.sec.gov/Archives/edgar/data/863064/000162828026009531/rio-20251231.htm',
                                                                         'note': 'Declared ordinary cash cost; larger than '
                                                                                 'FY2025 FCF amid investment programme. Not '
                                                                                 'a dividend/FCF paid-cash ratio.'}},
                      'source_urls': ['https://www.sec.gov/Archives/edgar/data/887028/000162828026009531/R11.htm',
                                      'https://www.sec.gov/Archives/edgar/data/863064/000162828026050377/rio-20260630_d2.htm'],
                      'refresh_policy': 'Datiertes Emittenten-Research; bei neuer Berichtsperiode erneut prüfen. Aktuelle '
                                        'Kurse, Dividendenhistorien und Renditen werden täglich aktualisiert.'},
  'regular_dividend_reference': {'value': 4.65,
                                 'currency': 'USD',
                                 'as_of': '2026-11-20',
                                 'kind': 'trailing_regular_cash_12m_not_a_forward_promise',
                                 'source_urls': ['https://www.sec.gov/Archives/edgar/data/863064/000162828026009531/rio-20251231.htm',
                                                 'https://www.sec.gov/Archives/edgar/data/863064/000162828026050377/rio-20260630_d2.htm']},
  'hist_div': 7.396701068562776,
  'reference_set': 'income-universe-2026',
  'tranche_policy': 'individual_only',
  'active_in_portfolios': False,
  'prepared_for': 'future_custom_portfolio_simulation',
  'logo': {'source_url': 'https://www.riotinto.com/-/media/project/riotinto/shared/favicon-32x32-new.png?rev=ca7eb2791f3040399e6332e59d664bbd',
           'homepage': 'https://www.riotinto.com/',
           'mime_type': 'image/png',
           'data_uri': 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAMAAABEpIrGAAABmFBMVEXIIiLKLCzyysryzc3////KLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzIIiLKLCzIIiLKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzIIiLKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzIIiLKLCzKLCzIIiLKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzIIiLKLCzKLCzIIiLKLCzIIiLKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCzKLCwgYgPKAAAAh3RSTlMAAAAAAAECAwUICQoLDA0REhYWFxcYGhseICMkJSYnKCkqKzAyMzpBQkVJSUtMTU9QUlNVVldZWl9gYWNkZ2dpamxucHJzdHV2d3t9g4iKjY6Rk5aZmpqcnZ6foKKjpKWmqKutr7K6wsXGyMvN09TV19jZ2tze4ePk5ujp7/Hy8/T19vf6+/y7gbPLAAABa0lEQVQ4jXXT6VsBURQG8LlMSps2LZQkaVPatZP2fV8JbUobadEiIfFv55wxmol7Pt33Pr85M3fODMNKR10uJ5Z7b3naOtJTRzLFSFiGlR4kRRUPuE0FIlA+8538V556IWDIDmy+mzuN3eOOIAp/rQiYYe+Ou0h5gsIhAn2w5SvkRMkjpGijEPQLAVnDFgN0MIRglg5MCBbpYAyBiQ5OIb1WUEF7DJKNZIF73FJYQhA28rPB1/76pt3zBMu3KdEs0iBToQUlyQWeDc0tXZepRXhblhPgLBQ3sDyU5QLcKfQRWFvpgGzh6KvpQIstVuiAnEH4qKEDI551ng6KfZAeSunDWsIWw0LQKwKqT4hXMgGYxKNV8e/PCTHR9gfkXmzKT4gYEhAvMqDjlhtS3K7iQN45tmjlwVE8EktVOPozmG6hx1/tWp4GlQ1anU7XpNGoi/ibzGHL4zL+IbPLht9VcIIKiHrV6w+87ErYX7zzylL9XLMAAAAAAElFTkSuQmCC'},
  'research_verified_at': '2026-11-21'},
 {'name': 'Main Street Capital',
  'ticker': 'MAIN',
  'isin': 'US56035L1044',
  'exchange': 'NYSE',
  'currency': 'USD',
  'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine ADR-/ADS-Umrechnung.',
  'asset_type': 'stock',
  'instrument_subtype': 'BDC',
  'valuation_style': 'bdc_nav_and_nii',
  'cluster': 'Private Credit / BDC',
  'hist_pe': 10.013675214,
  'hist_ps': None,
  'hist_div': 7.016046212926522,
  'field_metadata': {'hist_pe': {'provider': 'Emittentenberichte; Schlusskurse zusätzlich gegen Yahoo-Historie geprüft',
                                 'validation_version': 1,
                                 'label': '5J-Median · JE',
                                 'source_label': 'US-GAAP · IR',
                                 'accounting_basis': 'US GAAP; basic EPS = diluted EPS laut Note B.13',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-21',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                 'currency': 'USD',
                                 'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                'ADR-/ADS-Umrechnung.',
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'price_date': '2021-12-31',
                                                   'close': 44.86,
                                                   'diluted_eps': 4.8,
                                                   'eps_unit': 'USD je Common Share',
                                                   'nav_per_share': 25.29,
                                                   'nii_per_share': 2.65,
                                                   'dividends_per_share': 2.58,
                                                   'pe': 9.345833333,
                                                   'p_nav': 1.773823646,
                                                   'p_nii': 16.928301887,
                                                   'div_yield_pct': 5.751226037,
                                                   'eps_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'price_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'nav_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'nii_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'currency': 'USD'},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'price_date': '2022-12-30',
                                                   'close': 36.95,
                                                   'diluted_eps': 3.24,
                                                   'eps_unit': 'USD je Common Share',
                                                   'nav_per_share': 26.86,
                                                   'nii_per_share': 3.29,
                                                   'dividends_per_share': 2.95,
                                                   'pe': 11.404320988,
                                                   'p_nav': 1.375651526,
                                                   'p_nii': 11.23100304,
                                                   'div_yield_pct': 7.98376184,
                                                   'eps_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'price_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'nav_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'nii_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'currency': 'USD'},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'price_date': '2023-12-29',
                                                   'close': 43.23,
                                                   'diluted_eps': 5.23,
                                                   'eps_unit': 'USD je Common Share',
                                                   'nav_per_share': 29.2,
                                                   'nii_per_share': 4.04,
                                                   'dividends_per_share': 3.7,
                                                   'pe': 8.265774379,
                                                   'p_nav': 1.480479452,
                                                   'p_nii': 10.70049505,
                                                   'div_yield_pct': 8.558871154,
                                                   'eps_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'price_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'nav_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'nii_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'currency': 'USD'},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'price_date': '2024-12-31',
                                                   'close': 58.58,
                                                   'diluted_eps': 5.85,
                                                   'eps_unit': 'USD je Common Share',
                                                   'nav_per_share': 31.65,
                                                   'nii_per_share': 3.93,
                                                   'dividends_per_share': 4.11,
                                                   'pe': 10.013675214,
                                                   'p_nav': 1.850868878,
                                                   'p_nii': 14.905852417,
                                                   'div_yield_pct': 7.016046432,
                                                   'eps_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'price_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'nav_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'nii_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'currency': 'USD'},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'price_date': '2025-12-31',
                                                   'close': 60.39,
                                                   'diluted_eps': 5.52,
                                                   'eps_unit': 'USD je Common Share',
                                                   'nav_per_share': 33.33,
                                                   'nii_per_share': 3.95,
                                                   'dividends_per_share': 4.23,
                                                   'pe': 10.940217391,
                                                   'p_nav': 1.811881188,
                                                   'p_nii': 15.288607595,
                                                   'div_yield_pct': 7.004470939,
                                                   'eps_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'price_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'nav_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'nii_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                   'currency': 'USD'}],
                                 'comparable_to_current': True,
                                 'fact_type': 'historical_year_end_pe_median',
                                 'method': 'Median von fünf Jahresend-KGVs FY2021–FY2025: vom Emittenten veröffentlichter '
                                           'unbereinigter Schlusskurs am letzten Handelstag des Geschäftsjahres in USD '
                                           'dividiert durch US-GAAP diluted EPS des jeweiligen ganzen Geschäftsjahres in '
                                           'USD je Common Share. Kein Dividendenabzug, keine Währungsumrechnung. '
                                           'Nachträglich rekonstruierte Jahresend-Stichprobe; kein Mittel täglicher '
                                           'TTM-KGVs und kein historisch handelbarer Backtest.',
                                 'comparison_note': 'GAAP-Gewinne eines BDC enthalten realisierte und unrealisierte '
                                                    'Bewertungsänderungen. Für Nachkäufe sind NAV und nachhaltige laufende '
                                                    'Erträge aussagekräftiger; aktuelles TTM-KGV kann einen anderen '
                                                    'Berichtszeitraum verwenden.',
                                 'source_locator': '2025 Form 10-K, Note F Financial Highlights, p.175; Note B.13 Earnings '
                                                   'Per Share, p.153.',
                                 'reference_version': 1},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance / datierte Emittentenprüfung',
                                  'label': 'Hist. Div. 5J',
                                  'accounting_basis': 'Ex-Tag-Barausschüttungen',
                                  'currency': 'USD',
                                  'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                 'ADR-/ADS-Umrechnung.',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-21',
                                  'sample_count': 5,
                                  'window_count': 5,
                                  'source_url': 'https://finance.yahoo.com/quote/MAIN/history/',
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-31',
                                                    'close': 44.86000061035156,
                                                    'dividends_per_share': 2.575,
                                                    'dividend_yield': 5.740080171567836,
                                                    'currency': 'USD',
                                                    'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                                   'ADR-/ADS-Umrechnung.',
                                                    'source_url': 'https://finance.yahoo.com/quote/MAIN/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 36.95000076293945,
                                                    'dividends_per_share': 2.945,
                                                    'dividend_yield': 7.970229876026987,
                                                    'currency': 'USD',
                                                    'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                                   'ADR-/ADS-Umrechnung.',
                                                    'source_url': 'https://finance.yahoo.com/quote/MAIN/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 43.22999954223633,
                                                    'dividends_per_share': 3.695,
                                                    'dividend_yield': 8.547305202698261,
                                                    'currency': 'USD',
                                                    'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                                   'ADR-/ADS-Umrechnung.',
                                                    'source_url': 'https://finance.yahoo.com/quote/MAIN/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-31',
                                                    'close': 58.58000183105469,
                                                    'dividends_per_share': 4.11,
                                                    'dividend_yield': 7.016046212926522,
                                                    'currency': 'USD',
                                                    'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                                   'ADR-/ADS-Umrechnung.',
                                                    'source_url': 'https://finance.yahoo.com/quote/MAIN/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-31',
                                                    'close': 60.38999938964844,
                                                    'dividends_per_share': 4.23,
                                                    'dividend_yield': 7.004471009690179,
                                                    'currency': 'USD',
                                                    'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                                   'ADR-/ADS-Umrechnung.',
                                                    'source_url': 'https://finance.yahoo.com/quote/MAIN/history/'}],
                                  'method': 'Median der tatsächlichen 12M-Cash-Ausschüttungen nach Ex-Tag bis zum letzten '
                                            'Jahreshandelsschluss geteilt durch diesen Kurs. Gemeldete Cash-Sonderzahlungen '
                                            'enthalten; keine künftige Zusage. Bei O ist Orion-Sachdividende ausgeschlossen '
                                            'und die synthetische 1,032-Rückrechnung aufgehoben.'},
                     'ps_ratio': {'provider': 'Yahoo Finance',
                                  'source_url': 'https://finance.yahoo.com/quote/MAIN/key-statistics/',
                                  'role': 'informational_only',
                                  'note': 'Kein primärer Kaufmaßstab für diesen Reservewert; historische Vergleichsbasis '
                                          'nicht angesetzt. Sektorpassende Kennzahlen und Methoden stehen in '
                                          'primary_valuation/valuation_references.'}},
  'valuation_references': {'p_nav': {'provider': 'Emittentenberichte; Schlusskurse zusätzlich gegen Yahoo-Historie geprüft',
                                     'validation_version': 1,
                                     'label': '5J-Median · JE',
                                     'source_label': 'US-GAAP · IR',
                                     'accounting_basis': 'US GAAP',
                                     'period': 'FY2021–FY2025',
                                     'verified_at': '2026-11-21',
                                     'sample_count': 5,
                                     'window_count': 5,
                                     'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                     'currency': 'USD',
                                     'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                    'ADR-/ADS-Umrechnung.',
                                     'observations': [{'fiscal_year': 2021,
                                                       'fiscal_end': '2021-12-31',
                                                       'price_date': '2021-12-31',
                                                       'close': 44.86,
                                                       'diluted_eps': 4.8,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 25.29,
                                                       'nii_per_share': 2.65,
                                                       'dividends_per_share': 2.58,
                                                       'pe': 9.345833333,
                                                       'p_nav': 1.773823646,
                                                       'p_nii': 16.928301887,
                                                       'div_yield_pct': 5.751226037,
                                                       'eps_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'price_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nav_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nii_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'currency': 'USD'},
                                                      {'fiscal_year': 2022,
                                                       'fiscal_end': '2022-12-31',
                                                       'price_date': '2022-12-30',
                                                       'close': 36.95,
                                                       'diluted_eps': 3.24,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 26.86,
                                                       'nii_per_share': 3.29,
                                                       'dividends_per_share': 2.95,
                                                       'pe': 11.404320988,
                                                       'p_nav': 1.375651526,
                                                       'p_nii': 11.23100304,
                                                       'div_yield_pct': 7.98376184,
                                                       'eps_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'price_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nav_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nii_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'currency': 'USD'},
                                                      {'fiscal_year': 2023,
                                                       'fiscal_end': '2023-12-31',
                                                       'price_date': '2023-12-29',
                                                       'close': 43.23,
                                                       'diluted_eps': 5.23,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 29.2,
                                                       'nii_per_share': 4.04,
                                                       'dividends_per_share': 3.7,
                                                       'pe': 8.265774379,
                                                       'p_nav': 1.480479452,
                                                       'p_nii': 10.70049505,
                                                       'div_yield_pct': 8.558871154,
                                                       'eps_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'price_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nav_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nii_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'currency': 'USD'},
                                                      {'fiscal_year': 2024,
                                                       'fiscal_end': '2024-12-31',
                                                       'price_date': '2024-12-31',
                                                       'close': 58.58,
                                                       'diluted_eps': 5.85,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 31.65,
                                                       'nii_per_share': 3.93,
                                                       'dividends_per_share': 4.11,
                                                       'pe': 10.013675214,
                                                       'p_nav': 1.850868878,
                                                       'p_nii': 14.905852417,
                                                       'div_yield_pct': 7.016046432,
                                                       'eps_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'price_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nav_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nii_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'currency': 'USD'},
                                                      {'fiscal_year': 2025,
                                                       'fiscal_end': '2025-12-31',
                                                       'price_date': '2025-12-31',
                                                       'close': 60.39,
                                                       'diluted_eps': 5.52,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 33.33,
                                                       'nii_per_share': 3.95,
                                                       'dividends_per_share': 4.23,
                                                       'pe': 10.940217391,
                                                       'p_nav': 1.811881188,
                                                       'p_nii': 15.288607595,
                                                       'div_yield_pct': 7.004470939,
                                                       'eps_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'price_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nav_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nii_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'currency': 'USD'}],
                                     'comparable_to_current': True,
                                     'fact_type': 'historical_year_end_price_nav_median',
                                     'method': 'Median der fünf Jahresendschlusskurse dividiert durch den jeweils gleich '
                                               'datierten NAV je Common Share, USD/USD; Emittentenangaben, FY2021–FY2025.',
                                     'median': 1.773823646},
                           'p_nii': {'provider': 'Emittentenberichte; Schlusskurse zusätzlich gegen Yahoo-Historie geprüft',
                                     'validation_version': 1,
                                     'label': '5J-Median · JE',
                                     'source_label': 'US-GAAP · IR',
                                     'accounting_basis': 'US GAAP',
                                     'period': 'FY2021–FY2025',
                                     'verified_at': '2026-11-21',
                                     'sample_count': 5,
                                     'window_count': 5,
                                     'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                     'currency': 'USD',
                                     'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                    'ADR-/ADS-Umrechnung.',
                                     'observations': [{'fiscal_year': 2021,
                                                       'fiscal_end': '2021-12-31',
                                                       'price_date': '2021-12-31',
                                                       'close': 44.86,
                                                       'diluted_eps': 4.8,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 25.29,
                                                       'nii_per_share': 2.65,
                                                       'dividends_per_share': 2.58,
                                                       'pe': 9.345833333,
                                                       'p_nav': 1.773823646,
                                                       'p_nii': 16.928301887,
                                                       'div_yield_pct': 5.751226037,
                                                       'eps_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'price_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nav_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nii_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'currency': 'USD'},
                                                      {'fiscal_year': 2022,
                                                       'fiscal_end': '2022-12-31',
                                                       'price_date': '2022-12-30',
                                                       'close': 36.95,
                                                       'diluted_eps': 3.24,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 26.86,
                                                       'nii_per_share': 3.29,
                                                       'dividends_per_share': 2.95,
                                                       'pe': 11.404320988,
                                                       'p_nav': 1.375651526,
                                                       'p_nii': 11.23100304,
                                                       'div_yield_pct': 7.98376184,
                                                       'eps_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'price_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nav_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nii_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'currency': 'USD'},
                                                      {'fiscal_year': 2023,
                                                       'fiscal_end': '2023-12-31',
                                                       'price_date': '2023-12-29',
                                                       'close': 43.23,
                                                       'diluted_eps': 5.23,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 29.2,
                                                       'nii_per_share': 4.04,
                                                       'dividends_per_share': 3.7,
                                                       'pe': 8.265774379,
                                                       'p_nav': 1.480479452,
                                                       'p_nii': 10.70049505,
                                                       'div_yield_pct': 8.558871154,
                                                       'eps_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'price_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nav_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nii_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'currency': 'USD'},
                                                      {'fiscal_year': 2024,
                                                       'fiscal_end': '2024-12-31',
                                                       'price_date': '2024-12-31',
                                                       'close': 58.58,
                                                       'diluted_eps': 5.85,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 31.65,
                                                       'nii_per_share': 3.93,
                                                       'dividends_per_share': 4.11,
                                                       'pe': 10.013675214,
                                                       'p_nav': 1.850868878,
                                                       'p_nii': 14.905852417,
                                                       'div_yield_pct': 7.016046432,
                                                       'eps_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'price_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nav_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nii_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'currency': 'USD'},
                                                      {'fiscal_year': 2025,
                                                       'fiscal_end': '2025-12-31',
                                                       'price_date': '2025-12-31',
                                                       'close': 60.39,
                                                       'diluted_eps': 5.52,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 33.33,
                                                       'nii_per_share': 3.95,
                                                       'dividends_per_share': 4.23,
                                                       'pe': 10.940217391,
                                                       'p_nav': 1.811881188,
                                                       'p_nii': 15.288607595,
                                                       'div_yield_pct': 7.004470939,
                                                       'eps_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'price_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nav_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'nii_source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'source_url': 'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                                                       'currency': 'USD'}],
                                     'comparable_to_current': True,
                                     'fact_type': 'historical_year_end_price_nii_median',
                                     'method': 'Median der fünf Jahresendschlusskurse dividiert durch den veröffentlichten '
                                               'gewichteten basic NII je Common Share des ganzen Geschäftsjahres, USD/USD. '
                                               'Einheitlich die NII-Werte des Financial-Highlights-Tables im 2025-10-K; '
                                               'kein DNII und kein Core EPS.',
                                     'comparison_note': 'NII ist laufender Nettoinvestmentertrag und schließt '
                                                        'Bewertungsänderungen aus. Der Nenner ist basic NII; die '
                                                        'KGV-Referenz verwendet diluted GAAP EPS. Diese Reihen sind separat '
                                                        'benannt und nicht austauschbar.',
                                     'median': 14.905852417}},
  'identity_sources': ['https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm',
                       'https://www.boerse-stuttgart.de/en/products/equities/stuttgart/a0x8y3-main-street-cap-cp-dl--01'],
  'buy_trigger': 'P/NAV ≤ 1,5× und DNII deckt die Basisdividende ≥ 1,1×',
  'buy_trigger_rationale': 'Vorschlag: Die interne Verwaltung, niedrige Kosten und Equity-Beteiligungen können einen '
                           'NAV-Aufschlag begründen. Ein Limit von 1,5× NAV verlangt dennoch einen Abschlag zur '
                           'rekonstruierten 5J-Jahresend-Medianbewertung; mindestens 1,1× DNII-Deckung gibt der regulären '
                           'Dividende einen Puffer. Die Schwellen sind eigene Vorschläge, kein belegtes Optimum oder '
                           'Backtest. Sonderausschüttungen separat behandeln.',
  'buy_trigger_sources': ['https://www.mainstcapital.com/investors/news-events/press-releases/detail/2775/main-street-announces-third-quarter-2026-results',
                          'https://www.mainstcapital.com/investors/news-events/press-releases/detail/2774/main-street-announces-first-quarter-2027-regular-monthly',
                          'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000161/q32026maininvestorlender.htm',
                          'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000016/main-20251231.htm'],
  'buy_trigger_type': 'proposal',
  'dividend_policy': {'as_of': '2026-11-03',
                      'regular_frequency': 'monthly',
                      'regular_latest_declared_per_payment': 0.27,
                      'regular_latest_declared_payments': ['2027-01-15', '2027-02-12', '2027-03-15'],
                      'regular_latest_declared_quarterly_total': 0.81,
                      'regular_forward_annualized': 3.24,
                      'regular_current_q4_2026_per_payment': 0.265,
                      'regular_current_q4_2026_quarterly_total': 0.795,
                      'supplemental_latest_declared': 0.3,
                      'supplemental_latest_payment_date': '2026-12-28',
                      'supplemental_guaranteed': False,
                      'dnii_latest_declared_regular_quarter_coverage': 1.185185185,
                      'nii_latest_declared_regular_quarter_coverage': 1.098765432,
                      'source_url': 'https://www.mainstcapital.com/investors/news-events/press-releases/detail/2774/main-street-announces-first-quarter-2027-regular-monthly',
                      'simulation_note': 'Für eine vorsichtige wiederkehrende Dividendensimulation reguläre Dividende und '
                                         'Supplements getrennt führen. 3,24 USD ist eine Annualisierung des zuletzt '
                                         'deklarierten Monatsbetrags, keine Jahresgarantie; weitere Supplements nicht als '
                                         'zugesichert ansetzen. Ein historisches Ausschüttungs-CAGR inklusive Supplements '
                                         'ausdrücklich als solches markieren.'},
  'financial_currency': 'USD',
  'quote_currency': 'USD',
  'primary_valuation': 'p_nav',
  'issuer_snapshot': {'as_of': '2026-09-30',
                      'published_at': '2026-11-05',
                      'verified_at': '2026-11-21',
                      'currency': 'USD',
                      'metrics': {'as_of': '2026-09-30',
                                  'released_at': '2026-11-05',
                                  'verified_at': '2026-11-21',
                                  'currency': 'USD',
                                  'period': 'Q3 2026',
                                  'net_investment_income_usd': 83823000,
                                  'nii_per_share': 0.89,
                                  'distributable_nii_usd': 89937000,
                                  'distributable_nii_per_share': 0.96,
                                  'distributable_nii_before_tax_per_share': 0.98,
                                  'gaap_eps': 1.25,
                                  'nav_per_share': 34.22,
                                  'regular_dividend_paid_in_period': 0.795,
                                  'supplemental_dividend_paid_in_period': 0.3,
                                  'total_dividend_paid_in_period': 1.095,
                                  'dnii_regular_dividend_coverage': 1.20754717,
                                  'dnii_total_dividend_coverage': 0.876712329,
                                  'nii_regular_dividend_coverage': 1.119496855,
                                  'debt_nav_ratio': 0.81,
                                  'regulatory_debt_nav_ratio': 0.7,
                                  'net_debt_nav_ratio': 0.78,
                                  'interest_coverage_ratio': 3.66,
                                  'nonaccrual_fair_value_pct': 0.9,
                                  'nonaccrual_cost_pct': 3.5,
                                  'ttm_operating_expenses_to_assets_pct': 1.3,
                                  'ttm_roe_pct': 14.4,
                                  'sources': ['https://www.mainstcapital.com/investors/news-events/press-releases/detail/2775/main-street-announces-third-quarter-2026-results',
                                              'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000161/q32026maininvestorlender.htm'],
                                  'notes': ['DNII ist eine vom Unternehmen definierte Non-GAAP-Größe; sie addiert nicht '
                                            'zahlungswirksame Vergütungsaufwendungen zum NII hinzu.',
                                            'Die 0,96 USD DNII deckten die Q3-Basisdividende 1,208×, die vollständige '
                                            'Ausschüttung inklusive Supplement aber nur 0,877×. Supplements können unter '
                                            'anderem durch realisierte Gewinne und zuvor einbehaltenes steuerpflichtiges '
                                            'Einkommen finanziert werden.',
                                            'Nicht börsennotierte Mittelstandskredite und Equity-Investments beinhalten '
                                            'Kredit-, Bewertungs- und Liquiditätsrisiken; der hohe NAV-Aufschlag kann '
                                            'schrumpfen.']},
                      'source_urls': ['https://www.mainstcapital.com/investors/news-events/press-releases/detail/2775/main-street-announces-third-quarter-2026-results',
                                      'https://www.mainstcapital.com/investors/sec-filings/all-sec-filings/content/0001396440-26-000161/q32026maininvestorlender.htm'],
                      'refresh_policy': 'Datiertes Emittenten-Research; bei neuer Berichtsperiode erneut prüfen. Aktuelle '
                                        'Kurse, Dividendenhistorien und Renditen werden täglich aktualisiert.'},
  'hist_ps_not_used': 'KUV ist für einen kreditinvestierenden BDC keine sinnvolle Nachkauf-Referenz; NAV und NII werden als '
                      'passende ergänzende Bewertungsgrößen vollständig rekonstruiert.',
  'regular_dividend_reference': {'value': 3.24,
                                 'currency': 'USD',
                                 'as_of': '2026-11-03',
                                 'kind': 'latest_declared_payment_annualized_not_guaranteed',
                                 'source_url': 'https://www.mainstcapital.com/investors/news-events/press-releases/detail/2774/main-street-announces-first-quarter-2027-regular-monthly'},
  'reference_set': 'income-universe-2026',
  'tranche_policy': 'individual_only',
  'active_in_portfolios': False,
  'prepared_for': 'future_custom_portfolio_simulation',
  'logo': {'source_url': 'https://d1io3yog0oux5.cloudfront.net/_a4e908e41eed2a06e52395a353a7ea8f/mainstcapital/files/theme/images/favicons/apple-touch-icon.png',
           'homepage': 'https://www.mainstcapital.com/',
           'mime_type': 'image/png',
           'data_uri': 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAAAIsUlEQVR4AezBgQAAAADDoPtTX2AI1QoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABwdu4xSpacj+N4qtJ3rOu1bdu2Z3vWtnGNtW3bto1r27bNmZ0833M2j3KSrq6uxs70ffE5uahO9VR+nc6/MLImtjY2S2BTlENEUIXNAlRBpKgEmxr9rSvjMR8iDF7nYV3HcSg19+3XxCTt+pbtW0CE4cd9L8F4VEAkR0raVtgGO2ErtOTfYxAwxGIoSEEMIh3S0gkkXsIMTHGYjjYQqfBrfI/2voB9TMNlECm6BFON/rphfYiQivGR8X6nYgKOsAS6Ke0PmG4cs6+wTshjFXOMx1ScBRGgAIfhFQzFLMzHDAzGG6gmxE0hgJiHNvgR34XwI679Jwb6U6gAvdAcIgUbYwxUgJshUlCJX6AMtTgnxdn+dyjDapxoCXRz2kFQFo8z6xdCBNOBdo9H0Ae+AvdhEVQCq/AzjkYTCLwOlYJnG2qgl+N4iBRcjvoMBvpILIWy+ChEoBAY6BNCBnopzocIFhjoSxO8zqftijqoJI3CFhB4La8Crb0MCZE8WUb7A1QmAu3/vVZ8EcphJnaECCFyoA3jsVsmA41tMdnYfg4+wMN4A6P+J/Ar0YbxkXrZcTsGof//mGf0N9f4/0Ho2JADPRFbQIRwMBZlKtDYCnogndrnMtDat2iZwUBfYHwLzsep0BOQ9HU90R5T8Rz/VgIBxErR9H80x/vG/l9HlbFdSUMOdD2ugUiGV+N5tE9AZTDQbaAC/ImqHAe6HvfJuGwC4RIh0J2Nbbs7zor42AmtIdx8n/YNo88XIEwNNtDajygLUwxmMNDN0AMqwFIcnstAa4sQT/cM7fhgT8buEKnQgX6zsQX6L8egHBiiGLT1UZ+mQJ+IFVBJeNI/3fcggmQq0NpIbJ+BQB+Hlcb2Q3EVOIctm0Akr/EFug4DsQrK8DBEAFcxuAAjowbaZ4DMAx5gFDbI1Qxt+BRN0xzoFo73XIdJeAs1aJ2vM3Q97scoKMNwrJdiMfgF3okaaOyI6VCGoZjlGNjz/iGBrsOtfjwmIaBFCDRwMMZDOazGAFyLZvm45Lgcj7svWIQuBmtxHl5IQ6C7OpZIV+I9KIuPg89JpznQ7uXVfJwYZYZ22AtfYWXAB+pLbJFvgb4YB2JJmHDwf5s4isEhWAcvRgx0a/SHMkzARrjQsXafhZ2yOEMv0/3UQlkMxpbpDLRWgVPxAWaiPuBUYt4E+lIU42cow2zsErIYvAsCL0cMdByroQyvyrjn0W6OSVAWHbIY6MU4Fd9AObzLJFABgciBNhRgB7THQMuY/IVr8inQl0HgasenvGuIK4OzsVv0QMtC2g8d9yecCoEY3oay6EZwmmZpybEU+2JXTICyqEVbilwP0QPtJNehfdQyEXxl/aZt5IHezFFs9EGLJIvBDyRhRKRAYw/McSxn1obQzkGdI2RHQjikO9D7Q+BiLIeymI0jkLFAa03xh+W4tcirGdqPS5/2WSjDCpyQRDG4CtUQiBrou6GSOJW4EcY6tn3KO72JB2GToUAX4Rkohz7YmGPopxjoApyV+GKKlJaCeRha5VWgtSMcd7S9wleURKIrg33RKg2BXhdDoQxLcKjlpvtXEpyT3jCbM7S2PrpBOfB+ZSXtJ2ECzf6b6GO3FMNxnP1G/tiWlmsAPzB2xXm15NAq8RuUYRK2DCgGO0GkIdDnO84YdHPcq1HtKB7rcH62A60djBlQFitxQ5gZWt9t2MaYbObheRyDbbEdqh3j1z7/ikIN1zm2o1KWxY5icBq2jx5oWUL7FZRFFx0sqUnx91MYGyS4KvmJ+7Rj5gLtVQtPh3YVlMUMTAwR6EJd7NU6loSzMNuxv17YIJ8DvYWjWv8eJ2AhlOF14+svpUDrYCxwXLj4E+/ifcOHCWbDWdg52zO0VmaGxc0ZaLO/dpgZoq++2BMibwPNwPn6q0wZFqJ3iKdcQgVa1AhPz0IqzTrlItDa5hgQNdBGwbc3Xsd0x9KvFuPxIDaBCAj0q6jHKt0+06gC7Xzcye1PNI0aaGyM0VBp1h3NchFo7TjMixhog2xCuzXOwZ14WrsV1dgEEiIJO+N4HKfbHRpjoCstg+1yI0QaAn0F6qHSbBmOylmg4zLsc4A60LnT6AKt3RDyca0IgZbltD8muF+7LknK4Wm/OuZBaFkLtNYUn+Yg0GsCHfI5vueowH0IQ6hA43AscYTrTpyEUwKcheFQFqOxUbZnaMP2GJm7QK8JtHSFUluCwyAizdBx36d9zrHtiJC/ROZBKIs6XJDLQGtnYHEKga7ApWiLNlnWDkc0+EBrx2AZlMVPkqUChEWYGXrLBOdknxJcaodIhr6SuMT5BEk8VpTx89DBxdwDqA8Z6A0wHipHnm0sgW6KPx3r2sshos7QuDFBMXd0Cg/U9oIKuBU2J4HWWuPHHAR6TaC1mxxr0o2jBjrgie7eaA4R0h1QDl0j/CqwFqED7bYXJrnGY02gg0nci57oZjgJIoHN8RW6oxt6ooN/hu9BuLBNR8v+euAM4xzoj+hm6BnhFwTujp8dfT6JYggU4WnLe/zNFlACXUn7GnoY2/+IHSFCOg/dLE50zOofQI9BVvVEm9wF2q0Y5abAR9/jMY/tSo3XFEAEKLLsy3xtE/1/Nk0gQiJ40qffUkefpQTTh4DQs3S5oQwxafYblx79WraXZZAQIUmUJfdzs2/9M+WGLIRIh+idaP9qlw4oAACiMIw9vpJ//yQX42BYg30FQiM0CA1Cg9BwLRAahAahQWiEBqFBaBAahEZoEBqEBqFBaIQGoUFoEBqERmgQGoQGoUFohAahQWgQGoRGaBAahAah4VogNAgNQoPQCA1Cg9AgNAiN0CA0CA1Cg9AIDUKD0CA0CI3QIDQIDUKD0AgNQoPQIDQIjdAgNAgNQsMDOwjdQhL2vUAAAAAASUVORK5CYII='},
  'research_verified_at': '2026-11-21'},
 {'name': 'Ares Capital',
  'ticker': 'ARCC',
  'isin': 'US04010L1035',
  'exchange': 'NASDAQ Global Select',
  'currency': 'USD',
  'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine ADR-/ADS-Umrechnung.',
  'asset_type': 'stock',
  'instrument_subtype': 'BDC',
  'valuation_style': 'bdc_nav_and_nii',
  'cluster': 'Private Credit / BDC',
  'hist_pe': 8.971311475,
  'hist_ps': None,
  'hist_div': 9.490855380354365,
  'field_metadata': {'hist_pe': {'provider': 'Emittentenberichte; Schlusskurse zusätzlich gegen Yahoo-Historie geprüft',
                                 'validation_version': 1,
                                 'label': '5J-Median · JE',
                                 'source_label': 'US-GAAP · IR',
                                 'accounting_basis': 'US GAAP diluted EPS; Convertible-Note-Verwässerung in 2022/2023 '
                                                     'berücksichtigt',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-21',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                 'currency': 'USD',
                                 'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                'ADR-/ADS-Umrechnung.',
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'price_date': '2021-12-31',
                                                   'close': 21.19,
                                                   'diluted_eps': 3.51,
                                                   'eps_unit': 'USD je Common Share',
                                                   'nav_per_share': 18.96,
                                                   'nii_per_share': 1.66,
                                                   'dividends_per_share': 1.62,
                                                   'pe': 6.037037037,
                                                   'p_nav': 1.117616034,
                                                   'p_nii': 12.765060241,
                                                   'div_yield_pct': 7.645115621,
                                                   'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775024000011/arcc-20231231.htm',
                                                   'price_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'nav_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'nii_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'currency': 'USD'},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'price_date': '2022-12-30',
                                                   'close': 18.47,
                                                   'diluted_eps': 1.19,
                                                   'eps_unit': 'USD je Common Share',
                                                   'nav_per_share': 18.4,
                                                   'nii_per_share': 2.19,
                                                   'dividends_per_share': 1.87,
                                                   'pe': 15.521008403,
                                                   'p_nav': 1.003804348,
                                                   'p_nii': 8.433789954,
                                                   'div_yield_pct': 10.124526259,
                                                   'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775024000011/arcc-20231231.htm',
                                                   'price_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'nav_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'nii_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'currency': 'USD'},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'price_date': '2023-12-29',
                                                   'close': 20.03,
                                                   'diluted_eps': 2.68,
                                                   'eps_unit': 'USD je Common Share',
                                                   'nav_per_share': 19.24,
                                                   'nii_per_share': 2.27,
                                                   'dividends_per_share': 1.92,
                                                   'pe': 7.473880597,
                                                   'p_nav': 1.041060291,
                                                   'p_nii': 8.823788546,
                                                   'div_yield_pct': 9.585621568,
                                                   'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'price_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'nav_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'nii_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'currency': 'USD'},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'price_date': '2024-12-31',
                                                   'close': 21.89,
                                                   'diluted_eps': 2.44,
                                                   'eps_unit': 'USD je Common Share',
                                                   'nav_per_share': 19.89,
                                                   'nii_per_share': 2.3,
                                                   'dividends_per_share': 1.92,
                                                   'pe': 8.971311475,
                                                   'p_nav': 1.100553042,
                                                   'p_nii': 9.517391304,
                                                   'div_yield_pct': 8.771128369,
                                                   'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'price_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'nav_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'nii_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'currency': 'USD'},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'price_date': '2025-12-31',
                                                   'close': 20.23,
                                                   'diluted_eps': 1.86,
                                                   'eps_unit': 'USD je Common Share',
                                                   'nav_per_share': 19.94,
                                                   'nii_per_share': 2.02,
                                                   'dividends_per_share': 1.92,
                                                   'pe': 10.876344086,
                                                   'p_nav': 1.014543631,
                                                   'p_nii': 10.014851485,
                                                   'div_yield_pct': 9.490855166,
                                                   'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'price_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'nav_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'nii_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                   'currency': 'USD'}],
                                 'comparable_to_current': True,
                                 'fact_type': 'historical_year_end_pe_median',
                                 'method': 'Median von fünf Jahresend-KGVs FY2021–FY2025: vom Emittenten veröffentlichter '
                                           'unbereinigter Schlusskurs am letzten Handelstag des Geschäftsjahres in USD '
                                           'dividiert durch US-GAAP diluted EPS des jeweiligen ganzen Geschäftsjahres in '
                                           'USD je Common Share. Kein Dividendenabzug, keine Währungsumrechnung. '
                                           'Nachträglich rekonstruierte Jahresend-Stichprobe; kein Mittel täglicher '
                                           'TTM-KGVs und kein historisch handelbarer Backtest.',
                                 'comparison_note': 'GAAP-Gewinne eines BDC enthalten realisierte und unrealisierte '
                                                    'Bewertungsänderungen. Für Nachkäufe sind NAV und nachhaltige laufende '
                                                    'Erträge aussagekräftiger; aktuelles TTM-KGV kann einen anderen '
                                                    'Berichtszeitraum verwenden.',
                                 'source_locator': '2025 Form 10-K Note 10 Earnings per Share, F-198 (PDF p.312); 2023 Form '
                                                   '10-K Note 10 für FY2021/2022. Kurs/NAV aus Note 14 Financial '
                                                   'Highlights, F-203 (PDF p.317).',
                                 'source_mirror_url': 'https://materials.proxyvote.com/Approved/04010L/20260302/10K_625711.PDF',
                                 'method_caveat': 'Die Financial-Highlights-Zeile Net increase in stockholders equity '
                                                  'resulting from operations ist eine NAV-Brücke und weicht vom echten GAAP '
                                                  'EPS ab. Hier wird ausdrücklich der diluted EPS aus Note 10 verwendet '
                                                  '(2021 3,51; 2022 1,19; 2023 2,68; 2024 2,44; 2025 1,86 USD).',
                                 'reference_version': 1},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance / datierte Emittentenprüfung',
                                  'label': 'Hist. Div. 5J',
                                  'accounting_basis': 'Ex-Tag-Barausschüttungen',
                                  'currency': 'USD',
                                  'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                 'ADR-/ADS-Umrechnung.',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-21',
                                  'sample_count': 5,
                                  'window_count': 5,
                                  'source_url': 'https://finance.yahoo.com/quote/ARCC/history/',
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-31',
                                                    'close': 21.190000534057617,
                                                    'dividends_per_share': 1.62,
                                                    'dividend_yield': 7.6451154278937175,
                                                    'currency': 'USD',
                                                    'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                                   'ADR-/ADS-Umrechnung.',
                                                    'source_url': 'https://finance.yahoo.com/quote/ARCC/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 18.469999313354492,
                                                    'dividends_per_share': 1.87,
                                                    'dividend_yield': 10.12452663519008,
                                                    'currency': 'USD',
                                                    'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                                   'ADR-/ADS-Umrechnung.',
                                                    'source_url': 'https://finance.yahoo.com/quote/ARCC/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 20.030000686645508,
                                                    'dividends_per_share': 1.92,
                                                    'dividend_yield': 9.585621239045244,
                                                    'currency': 'USD',
                                                    'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                                   'ADR-/ADS-Umrechnung.',
                                                    'source_url': 'https://finance.yahoo.com/quote/ARCC/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-31',
                                                    'close': 21.88999938964844,
                                                    'dividends_per_share': 1.92,
                                                    'dividend_yield': 8.771128613680768,
                                                    'currency': 'USD',
                                                    'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                                   'ADR-/ADS-Umrechnung.',
                                                    'source_url': 'https://finance.yahoo.com/quote/ARCC/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-31',
                                                    'close': 20.229999542236328,
                                                    'dividends_per_share': 1.92,
                                                    'dividend_yield': 9.490855380354365,
                                                    'currency': 'USD',
                                                    'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                                   'ADR-/ADS-Umrechnung.',
                                                    'source_url': 'https://finance.yahoo.com/quote/ARCC/history/'}],
                                  'method': 'Median der tatsächlichen 12M-Cash-Ausschüttungen nach Ex-Tag bis zum letzten '
                                            'Jahreshandelsschluss geteilt durch diesen Kurs. Gemeldete Cash-Sonderzahlungen '
                                            'enthalten; keine künftige Zusage. Bei O ist Orion-Sachdividende ausgeschlossen '
                                            'und die synthetische 1,032-Rückrechnung aufgehoben.'},
                     'ps_ratio': {'provider': 'Yahoo Finance',
                                  'source_url': 'https://finance.yahoo.com/quote/ARCC/key-statistics/',
                                  'role': 'informational_only',
                                  'note': 'Kein primärer Kaufmaßstab für diesen Reservewert; historische Vergleichsbasis '
                                          'nicht angesetzt. Sektorpassende Kennzahlen und Methoden stehen in '
                                          'primary_valuation/valuation_references.'}},
  'valuation_references': {'p_nav': {'provider': 'Emittentenberichte; Schlusskurse zusätzlich gegen Yahoo-Historie geprüft',
                                     'validation_version': 1,
                                     'label': '5J-Median · JE',
                                     'source_label': 'US-GAAP · IR',
                                     'accounting_basis': 'US GAAP',
                                     'period': 'FY2021–FY2025',
                                     'verified_at': '2026-11-21',
                                     'sample_count': 5,
                                     'window_count': 5,
                                     'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                     'currency': 'USD',
                                     'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                    'ADR-/ADS-Umrechnung.',
                                     'observations': [{'fiscal_year': 2021,
                                                       'fiscal_end': '2021-12-31',
                                                       'price_date': '2021-12-31',
                                                       'close': 21.19,
                                                       'diluted_eps': 3.51,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 18.96,
                                                       'nii_per_share': 1.66,
                                                       'dividends_per_share': 1.62,
                                                       'pe': 6.037037037,
                                                       'p_nav': 1.117616034,
                                                       'p_nii': 12.765060241,
                                                       'div_yield_pct': 7.645115621,
                                                       'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775024000011/arcc-20231231.htm',
                                                       'price_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nav_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nii_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'currency': 'USD'},
                                                      {'fiscal_year': 2022,
                                                       'fiscal_end': '2022-12-31',
                                                       'price_date': '2022-12-30',
                                                       'close': 18.47,
                                                       'diluted_eps': 1.19,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 18.4,
                                                       'nii_per_share': 2.19,
                                                       'dividends_per_share': 1.87,
                                                       'pe': 15.521008403,
                                                       'p_nav': 1.003804348,
                                                       'p_nii': 8.433789954,
                                                       'div_yield_pct': 10.124526259,
                                                       'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775024000011/arcc-20231231.htm',
                                                       'price_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nav_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nii_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'currency': 'USD'},
                                                      {'fiscal_year': 2023,
                                                       'fiscal_end': '2023-12-31',
                                                       'price_date': '2023-12-29',
                                                       'close': 20.03,
                                                       'diluted_eps': 2.68,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 19.24,
                                                       'nii_per_share': 2.27,
                                                       'dividends_per_share': 1.92,
                                                       'pe': 7.473880597,
                                                       'p_nav': 1.041060291,
                                                       'p_nii': 8.823788546,
                                                       'div_yield_pct': 9.585621568,
                                                       'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'price_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nav_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nii_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'currency': 'USD'},
                                                      {'fiscal_year': 2024,
                                                       'fiscal_end': '2024-12-31',
                                                       'price_date': '2024-12-31',
                                                       'close': 21.89,
                                                       'diluted_eps': 2.44,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 19.89,
                                                       'nii_per_share': 2.3,
                                                       'dividends_per_share': 1.92,
                                                       'pe': 8.971311475,
                                                       'p_nav': 1.100553042,
                                                       'p_nii': 9.517391304,
                                                       'div_yield_pct': 8.771128369,
                                                       'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'price_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nav_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nii_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'currency': 'USD'},
                                                      {'fiscal_year': 2025,
                                                       'fiscal_end': '2025-12-31',
                                                       'price_date': '2025-12-31',
                                                       'close': 20.23,
                                                       'diluted_eps': 1.86,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 19.94,
                                                       'nii_per_share': 2.02,
                                                       'dividends_per_share': 1.92,
                                                       'pe': 10.876344086,
                                                       'p_nav': 1.014543631,
                                                       'p_nii': 10.014851485,
                                                       'div_yield_pct': 9.490855166,
                                                       'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'price_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nav_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nii_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'currency': 'USD'}],
                                     'comparable_to_current': True,
                                     'fact_type': 'historical_year_end_price_nav_median',
                                     'method': 'Median der fünf Jahresendschlusskurse dividiert durch den jeweils gleich '
                                               'datierten NAV je Common Share, USD/USD; Emittentenangaben, FY2021–FY2025.',
                                     'median': 1.041060291},
                           'p_nii': {'provider': 'Emittentenberichte; Schlusskurse zusätzlich gegen Yahoo-Historie geprüft',
                                     'validation_version': 1,
                                     'label': '5J-Median · JE',
                                     'source_label': 'US-GAAP · IR',
                                     'accounting_basis': 'US GAAP',
                                     'period': 'FY2021–FY2025',
                                     'verified_at': '2026-11-21',
                                     'sample_count': 5,
                                     'window_count': 5,
                                     'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                     'currency': 'USD',
                                     'share_basis': 'Eine an der US-Heimatbörse notierte Common Share; keine '
                                                    'ADR-/ADS-Umrechnung.',
                                     'observations': [{'fiscal_year': 2021,
                                                       'fiscal_end': '2021-12-31',
                                                       'price_date': '2021-12-31',
                                                       'close': 21.19,
                                                       'diluted_eps': 3.51,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 18.96,
                                                       'nii_per_share': 1.66,
                                                       'dividends_per_share': 1.62,
                                                       'pe': 6.037037037,
                                                       'p_nav': 1.117616034,
                                                       'p_nii': 12.765060241,
                                                       'div_yield_pct': 7.645115621,
                                                       'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775024000011/arcc-20231231.htm',
                                                       'price_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nav_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nii_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'currency': 'USD'},
                                                      {'fiscal_year': 2022,
                                                       'fiscal_end': '2022-12-31',
                                                       'price_date': '2022-12-30',
                                                       'close': 18.47,
                                                       'diluted_eps': 1.19,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 18.4,
                                                       'nii_per_share': 2.19,
                                                       'dividends_per_share': 1.87,
                                                       'pe': 15.521008403,
                                                       'p_nav': 1.003804348,
                                                       'p_nii': 8.433789954,
                                                       'div_yield_pct': 10.124526259,
                                                       'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775024000011/arcc-20231231.htm',
                                                       'price_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nav_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nii_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'currency': 'USD'},
                                                      {'fiscal_year': 2023,
                                                       'fiscal_end': '2023-12-31',
                                                       'price_date': '2023-12-29',
                                                       'close': 20.03,
                                                       'diluted_eps': 2.68,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 19.24,
                                                       'nii_per_share': 2.27,
                                                       'dividends_per_share': 1.92,
                                                       'pe': 7.473880597,
                                                       'p_nav': 1.041060291,
                                                       'p_nii': 8.823788546,
                                                       'div_yield_pct': 9.585621568,
                                                       'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'price_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nav_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nii_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'currency': 'USD'},
                                                      {'fiscal_year': 2024,
                                                       'fiscal_end': '2024-12-31',
                                                       'price_date': '2024-12-31',
                                                       'close': 21.89,
                                                       'diluted_eps': 2.44,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 19.89,
                                                       'nii_per_share': 2.3,
                                                       'dividends_per_share': 1.92,
                                                       'pe': 8.971311475,
                                                       'p_nav': 1.100553042,
                                                       'p_nii': 9.517391304,
                                                       'div_yield_pct': 8.771128369,
                                                       'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'price_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nav_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nii_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'currency': 'USD'},
                                                      {'fiscal_year': 2025,
                                                       'fiscal_end': '2025-12-31',
                                                       'price_date': '2025-12-31',
                                                       'close': 20.23,
                                                       'diluted_eps': 1.86,
                                                       'eps_unit': 'USD je Common Share',
                                                       'nav_per_share': 19.94,
                                                       'nii_per_share': 2.02,
                                                       'dividends_per_share': 1.92,
                                                       'pe': 10.876344086,
                                                       'p_nav': 1.014543631,
                                                       'p_nii': 10.014851485,
                                                       'div_yield_pct': 9.490855166,
                                                       'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'price_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nav_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'nii_source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'source_url': 'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                                                       'currency': 'USD'}],
                                     'comparable_to_current': True,
                                     'fact_type': 'historical_year_end_price_nii_median',
                                     'method': 'Median der fünf Jahresendschlusskurse dividiert durch den veröffentlichten '
                                               'gewichteten basic NII je Common Share des ganzen Geschäftsjahres, USD/USD. '
                                               'Einheitlich die NII-Werte des Financial-Highlights-Tables im 2025-10-K; '
                                               'kein DNII und kein Core EPS.',
                                     'comparison_note': 'NII ist laufender Nettoinvestmentertrag und schließt '
                                                        'Bewertungsänderungen aus. Der Nenner ist basic NII; die '
                                                        'KGV-Referenz verwendet diluted GAAP EPS. Diese Reihen sind separat '
                                                        'benannt und nicht austauschbar.',
                                     'median': 9.517391304}},
  'identity_sources': ['https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm',
                       'https://www.quotrix.de/dokument-download/quotrix/kursblatt/Kursblatt-Quotrix-2026.04.28.pdf'],
  'buy_trigger': 'P/NAV ≤ 1,0× und NII deckt die Dividende ≥ 1,05×',
  'buy_trigger_rationale': 'Vorschlag: Für den extern verwalteten Direct-Lending-BDC ist NAV eine passendere Kaufreferenz '
                           'als KGV allein. Höchstens Buchwert und mindestens 5 % Puffer zwischen laufendem NII und '
                           'regulärer Quartalsdividende verbinden Bewertung und Ausschüttungsdeckung. Aktuell fehlt dieser '
                           'NII-Puffer. Eigene Schwellen, kein nachgewiesenes Optimum oder Backtest.',
  'buy_trigger_sources': ['https://arcc.ares.com/news/ares-capital-corporation-announces-september-30-2026-financial-results-and-declares-fourth-quarter-2026-dividend/0bf958ac-18eb-4a60-9868-fa8914e548a8',
                          'https://www.sec.gov/Archives/edgar/data/1287750/000128775026000006/arcc-20251231.htm'],
  'buy_trigger_type': 'proposal',
  'dividend_policy': {'as_of': '2026-10-27',
                      'regular_frequency': 'quarterly',
                      'regular_latest_declared_per_payment': 0.48,
                      'regular_latest_declared_payments': ['2026-12-29'],
                      'regular_forward_annualized': 1.92,
                      'supplemental_latest_declared': 0,
                      'source_url': 'https://arcc.ares.com/news/ares-capital-corporation-announces-september-30-2026-financial-results-and-declares-fourth-quarter-2026-dividend/0bf958ac-18eb-4a60-9868-fa8914e548a8',
                      'simulation_note': '1,92 USD annualisiert die zuletzt erklärte Quartalsdividende; keine Garantie für '
                                         'vier künftige Quartale. In der Historie gab es zusätzliche Ausschüttungen; '
                                         'Gesamtdividenden-CAGR darf nicht als garantierte reguläre Steigerungsrate '
                                         'erscheinen.'},
  'financial_currency': 'USD',
  'quote_currency': 'USD',
  'primary_valuation': 'p_nav',
  'issuer_snapshot': {'as_of': '2026-09-30',
                      'published_at': '2026-10-27',
                      'verified_at': '2026-11-21',
                      'currency': 'USD',
                      'metrics': {'as_of': '2026-09-30',
                                  'released_at': '2026-10-27',
                                  'verified_at': '2026-11-21',
                                  'currency': 'USD',
                                  'period': 'Q3 2026',
                                  'net_investment_income_usd': 328000000,
                                  'nii_per_share': 0.46,
                                  'core_eps': 0.46,
                                  'gaap_eps': 0.44,
                                  'nav_per_share': 19.31,
                                  'nav_previous_year_end': 19.94,
                                  'nav_ytd_change_pct': -3.159478435,
                                  'regular_dividend_paid_in_period': 0.48,
                                  'supplemental_dividend_paid_in_period': 0,
                                  'total_dividend_paid_in_period': 0.48,
                                  'nii_regular_dividend_coverage': 0.958333333,
                                  'core_eps_regular_dividend_coverage': 0.958333333,
                                  'debt_equity_ratio': 1.11,
                                  'net_debt_equity_ratio': 1.06,
                                  'nonaccrual_fair_value_pct': 1.4,
                                  'nonaccrual_cost_pct': 2.4,
                                  'nonaccrual_previous_year_end_fair_value_pct': 1.2,
                                  'nonaccrual_previous_year_end_cost_pct': 1.8,
                                  'portfolio_fair_value_usd': 29213000000,
                                  'portfolio_company_count': 636,
                                  'first_lien_fair_value_pct': 58,
                                  'floating_rate_fair_value_pct': 70,
                                  'sources': ['https://arcc.ares.com/news/ares-capital-corporation-announces-september-30-2026-financial-results-and-declares-fourth-quarter-2026-dividend/0bf958ac-18eb-4a60-9868-fa8914e548a8'],
                                  'notes': ['Die Q3-Dividende von 0,48 USD war durch Q3-NII/Core EPS von 0,46 USD nur zu '
                                            '95,8 % gedeckt. Eine Unterdeckung in einem Quartal beweist keine unmittelbar '
                                            'bevorstehende Kürzung, rechtfertigt aber keinen ungeprüften positiven '
                                            'Qualitätsstatus.',
                                            'NAV je Aktie lag rund 3,16 % unter Ende 2025, Nonaccrual-Anteile über Ende '
                                            '2025. Hohe laufende Rendite ersetzt keine Kreditqualitätsprüfung.',
                                            'Core EPS ist eine Non-GAAP-Kennzahl, die realisierte/unrealisierte Gewinne und '
                                            'Verluste sowie zugehörige Gebühren und Steuern ausklammert. Für diese Deckung '
                                            'wird explizit der ebenfalls veröffentlichte NII verwendet.',
                                            'Externe Verwaltung, private Kreditnehmer, variabel verzinste Kredite und '
                                            'Fremdfinanzierung erzeugen Kredit-, Zins-, Gebühren- und Bewertungsrisiken.']},
                      'source_urls': ['https://arcc.ares.com/news/ares-capital-corporation-announces-september-30-2026-financial-results-and-declares-fourth-quarter-2026-dividend/0bf958ac-18eb-4a60-9868-fa8914e548a8'],
                      'refresh_policy': 'Datiertes Emittenten-Research; bei neuer Berichtsperiode erneut prüfen. Aktuelle '
                                        'Kurse, Dividendenhistorien und Renditen werden täglich aktualisiert.'},
  'hist_ps_not_used': 'KUV ist für einen kreditinvestierenden BDC keine sinnvolle Nachkauf-Referenz; NAV und NII werden als '
                      'passende ergänzende Bewertungsgrößen vollständig rekonstruiert.',
  'regular_dividend_reference': {'value': 1.92,
                                 'currency': 'USD',
                                 'as_of': '2026-10-27',
                                 'kind': 'latest_declared_payment_annualized_not_guaranteed',
                                 'source_url': 'https://arcc.ares.com/news/ares-capital-corporation-announces-september-30-2026-financial-results-and-declares-fourth-quarter-2026-dividend/0bf958ac-18eb-4a60-9868-fa8914e548a8'},
  'reference_set': 'income-universe-2026',
  'tranche_policy': 'individual_only',
  'active_in_portfolios': False,
  'prepared_for': 'future_custom_portfolio_simulation',
  'logo': {'source_url': 'https://arcc.ares.com/apple-touch-icon.png',
           'homepage': 'https://www.arescapitalcorp.com/',
           'mime_type': 'image/png',
           'data_uri': 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAAAQAElEQVR4AeydTdIeNxGA32SXI8DGnAEqS/5WnASKO0AVoSrcgQonYcXfMgVnwBs4QpaOH9ttt2VppqWR5u2e6VTa0kgtqX8e9Tvv5y/w+av8JyNwoQh8/sh/MgIXikACfaFkpiuPRwKdFFwqAgn0pdKZziTQt2HgHo4m0PfI8228TKBvk+p7OJpA3yPPt/Eygb5Nqu/haAJ9jzzfxssE+vF43CbbN3A0gb5Bku/kYgJ9p2zfwNcE+gZJvpOLCfSdsn0DXxPoGyT5Ti7uAH2nUKSvV4hAAn2FLKYP7yOQQL8PRXauEIEE+gpZTB/eRyCBfh+K7FwhAgn0FbI4w4eL7JFAXySR6cbbCCTQb+OQf14kAgn0RRKZbryNQAL9Ng7550UikEBfJJGlG3//578fyC9/9dsH8tkXXz60MPbHP/3lgZRrIz8n0PvZC6MhAAMuwCKMIaUTjH319TcPBP2rgJ1Al5kO9gyYgAuUtDyPuADYV4A6gR7JvoM1gAvACP0ZJl0B6gR6Bgkn7gG8QIzQn310dKgT6NlELNoPeIEYob/omDfbAvWbTsA/EugASQNiZDXIOhRnnqXPPdo/BvTR03P9ZgT4ksaXvWfAxdmbxjmdTKAdJgaAqcjP/OjHBsRheDZNSqA3w7M/SdK17K/Y1qAyAjN7bmvuz/7iZz95IH/7658fWr76/W/ejO/vEE8jgd7JGWAJZICG8BogwrMWGZdW5tgDYb/WkegercoC8Kvvvn0PMWNa/vC7X38017IHe1tzXscTaJUZYEMACwFKWiBjXEQt2e3KGvZA2K8EBR3GaXc3rCgAKxVYIOa5ovbJEHqso/1kMujA7YEGIgSgRHhGVuSUj3sqpOwN3Jw7ch4gAiRCX/bsbVlfW6Ntqs17HLst0CQLkER4npUg4ELK/QCnhJmqXertPbM3eyH09/Qt8+xl0fOuczugpSIC8gjEAIRQaYEA4aNeC2OIHqPPOoDgXM4fgVnOlb3Yb4bM3m+GTSN73AZoQOadGIgAyhosEo0IoLQIlZZxxLoXepwNzLQ89whncW7Pmh5d9u/R96h7eaA1yD0JILmAK8Jzz/qaLhADc23OMsZ6LiU+WfR7dWb42HvmbP3LAk3ygYeKbA0aCQVgXg9oebau3dMTe/b0ynleMT4Z+/qbN7+sPxvsn//0x+VR4Z4vCTSJBmYgsmQEcAEYoW9Z80Fnvyf27Gt+0MAOLhavGLSAjXzQeFzul/O1b6P9SwENwIDcU5WBZBXIJAWYe+xhjdhEXwSwEYFbxmnZf+WrCGdEkcsADTjADNSW4FMBARlILPojOtgEbD1rgXnPJuaxHV29N2dxph7r6RMTrV8+6zmv/UsATRJJpjXIgAAQKxPWaxO2YxOw0t8TbEcXX7QucRit1tZioM/z1g8PdA84QNADzWiyemySM7AL++TZ2gJ16zUEO6z71PRG7Kntc+ZYaKBJGBXJEjCSMwqNZX/R6bGJNbPsAuxateY1jHMs8o9//cei5lpnKdArPe8Bh0QD80p72LvHJvRnwcxeCFBTrdmXZ4TXCF5BaHnukYg/xgsJdA84wEyiexI5ottjE/sD3apLxr74zTkiVGpslGdLi40WPU864YAmKdbXDJLqEWbsArqVIOA35+gziBvx02NX64cCmo9NkmJJAskkqRbdIzo9NnHOWXZxFv5zHn0R4teCmjnRi1idsT0U0K1E4IgWkkEy9diKPjDzUW7dG7jOsEvbw3mcq8cAdy+WxFCvidIPAzQJACBLYEmiRe+IDrZ4h1n8Ix7lK04JNf6IPm3nF0KWuJAQQAMzCbBEjGp0RnWJArPEjJhsQV3+yA59WRupDQG0FWYCTzWiXSlcMOv+XLAzbLLYA6TYo3WJbekPelonUt890GWwt4JbJmtLd3QOe4DAsh4wvMAs9mJPGSf8QUQHu6UfrXUPtA70VnBJEsna0jk61wtz+RF/9PxZ64nTFrRR35+Jj2ugAQgjLUKSLHqjOnxpsl4uzvAKM7Yh2NeCujXOOu/iGmgrQFTn1YHu+RIILO/tcdypFYHIMBNqt0D3VGccWSm9MEeBAjvLy8cnkafY9+bVLdA91blWaXoD0dInuSS5Na/H+aQAEj3mvY+9iLaT2Ft91us89F0CDUQegoMdJNdiCzCvvFgWG0Z1anbj++h+z1znEuiegNSS0bO+pUtC7wAz/lOhuZD0RajQxECeo7Quge4BaVWgrTYAw6pLtco32RdopY8P+CLPtMRA6zDmXZ4JdDU2HqpCjw3ll6qqU04H+bKrgQXq0tSeWJRrn/HsDmgd4L2A1BKwt2ZvngRSmfb0mI8MM37ig7T0qdAIfRHygciz9zY00LODS+KsMPPOWSZ/tj1n7IfPGupakdDzZ9h05AxXQBNcqzMAZdW16lkTx9m1xFvP8aDXurhcUkTbSF4QPea17wro8lcYzwwaMFuSRrKjw4yvOralP+UzuuUaxjyKK6AtQK0IIslqVazyvMjvzeKL9pVPGxmXlkuLyDMtuUHo98t5K1wB3eP2zN8I0wnesqGW/C19j3NcXm1XrRozXxsv16LnTVwB3VMBygoyGlhrkoC5luTRc5+1Tl9efGrZUYsv+UFaazyMuwL67IAAs05w63ySewWY8Vf7uPcph99an365B2OexA3Q/335/9PjYoEZo67w3owf2l+qcw1Y9ERqlzgrtERnp3358n87GnOnrZWGxM89+Tm7Wf3V1rWA9wy1mwqtA7m6T3J1tWqdB8y1KtXSHxk/a432t8evGtTE7yy7e8+5JdA6uVsBuwrMswHMCr1Fzclz1uRSxU42bdlx+gLjV89Fbel6hfpWFRqYdXJbBPUmvbWPh3F8XmHHqn2P2hoW6JEKYYGZgLaqEnPRRPs8clFr79CeY+AG6Bcvfrg0TtaKQtKXGnLi5qXPoxe1BvVIQTnDdTdAf+rs9kjPLzKRWF2pWjsD82jSW3s+c1z7jG+zbfEIdVige4Jp1b0SzFxiDfAR32oVWu/tqe8G6B+9+MGSuJBYC9ArKtgSh4ybrq7OmNHzKYn+GeIG6F5nLZCyp04szzUB5iMVrLbnM8e4xPr8Vb5Zc6BtWd13BXTvR9teQMvEtoK5KuGt81aO47O+xFzWled529sV0L3BIXlba3RiW3pXS7j2mQIR4rK2kjMw7gromcHfg51YXS3hpc/4h5+rZO8TctW5W/u6AnrL0NocAUXKORKrK1U5L8+rEy7nnNVqn/nkmVkgWj7U4t/SPWPcFdAjgAFvGSid2HJOns9KuJy3ui3jcAbMq30a2d8V0DjQC3VZIcrEsmdNrpRwfNaXmMta8/kOY+GBLpOkE1vOyfPVEq59xrfZl7UsGhJHj607oPf+O7cyiCRQxqhU0t9qZyd866yDc7vLS59747d7wGuFBPp1EEb/5ZUDsa7XcOpK1VqvL0BLJ8o4MGuf8a0ndlH87LHTXYXuMZ4Eij7JlX6rRV9fgJZelHENMyCv8C1SdSZvLoG2Jkbr6eTiWE1WfBzXzjljrLzAOhYzz/f4+xpb/rkEmmqDbBlOtZX5Mrkyrlv09/bU+p77+Ksv8DN98xZTl0AD016gdEXSyWVtTbR+bT7SmPaXOK30TZ8VIUZugd56PaAiPd5Fl2r1rttstH5TKchE6e9KmMuzyhBxmcqxZz+7BZpgIbUA6SRaKojWr+0XZQzAtL9c1FaMovg02063QONoDUSSyBxCgmm3ROtv6UWYK2GuxWemH/q82r4eL5NroAkYUgsmY3sBR2d10jnjDCkv72q/yvPO8HHGGa6BxsEycfJsCfjWZWDvKIKv+vKe8amjz2vFaet7TmvN6nH3QAMlQiB6E8lfCgADa6MK9mu4iIFc6lU+caZlb8mLRfeITs9a90DjjCRQWsZ0knluCXqA3Zr3Po79YiMA6RjI+OxWn9naG1tac88cDwE0wdP/G83WCiKB7dWXdc9uS7vPgLk8sxUDctKae+Z4CKAJkA6gpYKwRoQKzf9rqjxHaAFL+8mrho7BCh/KM7fO8Pj+jL1hgMZYhKDT9kokqPGxhPmM6qzP3Ivv6su1d35rPhzQLUcs4xGgxkYNFuCcAXPPJxifFpZ4P0MnHNA62SMBA5ie5I2ccWRNaZv+7vDpvnNG+EQgLtbdzrhgVltKvVBAE/jSgZFnkleCM7LP7DWlTWfB3FMkPFdn8hEKaAyeJUD92RdfPmZdkqN2YQc2yT6Aw+uGPK9oObMH5hU2zN4zFNArgs+eJHZ2YHv243zskDXAvPpjncujz5Szt1ou2Gq7ts63zIUBmgRYHBrRIbFANbL26BrO5XzZ5yyYy9cbOX+rBeiteQ9zYYBe/Z8CAdXZryBcUs4VEABmdQXkAo3AfMZFkzgcacMATfJ7HB3VBTCSPrreug5/SrBWfwnEL/yz2qj1vP5FiraRfgJNFAoh6aur9Zkwy+XBr8JV0yPVmU8Pk/KTlUIATWXZi9Or7759zA46AHA2QOyd3zNfwrwSGGznPNoeG7Xu6tcgfdbRfgigrU7ykb0CaoAAbMRqS0uPvTRcwLwCGM7gLKRli2Uc+yx6XnRCAE1ytgKmg74Cas6mWiMAMgo2a7Uv2D0bZvbnHIQ+to/KCvtGbbGuuwTQpbNATTLK8RnPQALYve/YJWDYNxNm7OIMhP5RX2fbd9SejfUfTbkH2pKc2jdwYCEpH3k7+UHABiKqdstW5vUcdmHfDHPYl/0R+jP2nGnfDHt69nAPtOXnz633ZqAhOT0BGdEFJOAGKqncAM5ejDFPH8Ee7KI/IuyFsC9n0fI8sldtDbE8Yl9tzzPH3AN9NBgkh1eQo/v0rAduBOA0bMDCPowh9FvCvAjQIuxHizDXWjs6jn1nx2rU1tY690DvJY4ktJyTcXT4sR7VUcae0eILoAMkAqAtYV6EdchKm4lRdJiJj3ugMXJLSMTWvJ6jWj8bam2Plz4xuQLMxNM90LMrE1B7qNYEf7kYDgBmYmJQDaHiHuhVUSSJJHPV/hH2pSoThwi2Wm10DbSlOtd+ZGd1nmTesVrzmobftNZYRdFzDfRZQQRsqtUdKjZ+ImfF9uxzEuh3EadaATZQI++GL9Pg01Wrsk5SAq2j8boP1AgAIK+HQv/LRaUi41NoR4zG3xXo3fAAAEJViwi2gAzM9HcdvohCeKAtfzV+NFcabO9wAy8QI/SP+h5tfXigLT8JmZUUwEakanuBG3ABGLtoeZ7lc7R9wgP9rIADNgJEgI2cBRLnIMDL+bQ8PysWns51DbQlSVRoZHZQ2ZPfpeB3LfjNOaR1BmAjgCWAATiCD0hr7dY46xD2YW9E9qfP3Nb6O865BpqEWJI2+z0aeIEZqLGBXyhCGOd5T7AZwBHAQwBRhOeWiA6t6LAPeyJ7Z999/lOgA0YE2AS+I+YDLBWZ/fQ+gARcgKXHR/vs15LRPXPd2wi4B9oKETC+dan/T9bWQGYn+bgHQJ5TfEfAudfdRwAAAcZJREFUPdCAhOyFkQoNlLR7usyjx2sFa8qKzDxnzqzK7JmyPgLugSYE1iqNLpAiVF2g1cIYc0BMyxxrSsmqXEYkznMIoKmWiDWsgErVBVotjDHX2oczsiq3ohNjPATQhBLQaFeIgMwZ9Fec4XHPK9oUBmiCD3C0swR42ROhP2vf3Od5EQgFNNDx81naIyFjPRAj9I/slWt9RSAU0BI6QOSLmzzvtUCLsI4LQcvz3rqcjxeBkEATZn7yAZwIcIsAKsIz4GphnLUp141AWKB1SoBbRADmGYARrZv9a0fgEkDPT1HuGDUCCXTUzKXd1Qgk0NWw5GDUCCTQUTOXdlcjkEBXw5KDUSOQQEfNXNpdjUA30NVdcjAj4CQCCbSTRKQZcyKQQM+JY+7iJAIJtJNEpBlzIpBAz4lj7uIkAgm0k0Q4NCOkSQl0yLSl0a0IJNCtyOR4yAgk0CHTlka3IpBAtyKT4yEjkECHTFsa3YpAAt2KzNZ4zrmNQALtNjVp2EgEEuiRqOUatxFIoN2mJg0biUACPRK1XOM2Agm029SkYSMRmA30iA25JiMwLQIJ9LRQ5kYeIpBAe8hC2jAtAgn0tFDmRh4i8D0AAAD//6MWfZoAAAAGSURBVAMAnx24L8Bys1YAAAAASUVORK5CYII='},
  'research_verified_at': '2026-11-21'},
 {'name': 'Realty Income Corporation',
  'ticker': 'O',
  'isin': 'US7561091049',
  'exchange': 'NYSE',
  'currency': 'USD',
  'quote_currency': 'USD',
  'financial_currency': 'USD',
  'asset_type': 'stock',
  'instrument_subtype': 'equity_reit',
  'cluster': 'Immobilien / REIT',
  'share_basis': '1 common share, USD0.01 par value. No ADR conversion.',
  'identity_sources': ['https://www.realtyincome.com/sites/realty-income/files/2026-02/q4-realty-income-12-31-25-10k.pdf',
                       'https://itsx.kz/trading-and-data/traded-instruments/share/?issue=5207'],
  'hist_pe': 48.179486266568176,
  'hist_ps': None,
  'hist_ps_not_used': 'KUV wird bei diesem REIT nicht als Bewertungsreferenz verwendet; Emittenten-AFFO ist für die '
                      'Ausschüttungsdeckung geeigneter als Umsatzerlöse.',
  'primary_valuation': 'p_affo',
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'label': '5J-Median · JE',
                                 'source_label': 'US-GAAP · IR / Kurse',
                                 'accounting_basis': 'US GAAP diluted EPS',
                                 'currency': 'USD',
                                 'share_basis': '1 common share',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-21',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'price_date': '2021-12-31',
                                                   'close': 71.58999633789062,
                                                   'currency': 'USD',
                                                   'source_url': 'https://www.sec.gov/Archives/edgar/data/726728/000072672823000038/realtyincomeex991q42022.htm',
                                                   'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/726728/000072672823000038/realtyincomeex991q42022.htm',
                                                   'price_source_url': 'https://finance.yahoo.com/quote/O/history/',
                                                   'share_basis': '1 common share',
                                                   'diluted_eps': 0.87,
                                                   'pe': 82.28735211251796},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'price_date': '2022-12-30',
                                                   'close': 63.43000030517578,
                                                   'currency': 'USD',
                                                   'source_url': 'https://www.sec.gov/Archives/edgar/data/726728/000072672823000038/realtyincomeex991q42022.htm',
                                                   'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/726728/000072672823000038/realtyincomeex991q42022.htm',
                                                   'price_source_url': 'https://finance.yahoo.com/quote/O/history/',
                                                   'share_basis': '1 common share',
                                                   'diluted_eps': 1.42,
                                                   'pe': 44.669014299419565},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'price_date': '2023-12-29',
                                                   'close': 57.41999816894531,
                                                   'currency': 'USD',
                                                   'source_url': 'https://www.realtyincome.com/sites/realty-income/files/2026-02/q4-realty-income-12-31-25-10k.pdf',
                                                   'eps_source_url': 'https://www.realtyincome.com/sites/realty-income/files/2026-02/q4-realty-income-12-31-25-10k.pdf',
                                                   'price_source_url': 'https://finance.yahoo.com/quote/O/history/',
                                                   'share_basis': '1 common share',
                                                   'diluted_eps': 1.26,
                                                   'pe': 45.571427118210565},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'price_date': '2024-12-31',
                                                   'close': 53.40999984741211,
                                                   'currency': 'USD',
                                                   'source_url': 'https://www.realtyincome.com/sites/realty-income/files/2026-02/q4-realty-income-12-31-25-10k.pdf',
                                                   'eps_source_url': 'https://www.realtyincome.com/sites/realty-income/files/2026-02/q4-realty-income-12-31-25-10k.pdf',
                                                   'price_source_url': 'https://finance.yahoo.com/quote/O/history/',
                                                   'share_basis': '1 common share',
                                                   'diluted_eps': 0.98,
                                                   'pe': 54.499999844298074},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'price_date': '2025-12-31',
                                                   'close': 56.369998931884766,
                                                   'currency': 'USD',
                                                   'source_url': 'https://www.realtyincome.com/sites/realty-income/files/2026-02/q4-realty-income-12-31-25-10k.pdf',
                                                   'eps_source_url': 'https://www.realtyincome.com/sites/realty-income/files/2026-02/q4-realty-income-12-31-25-10k.pdf',
                                                   'price_source_url': 'https://finance.yahoo.com/quote/O/history/',
                                                   'share_basis': '1 common share',
                                                   'diluted_eps': 1.17,
                                                   'pe': 48.179486266568176}],
                                 'source_url': 'https://www.realtyincome.com/sites/realty-income/files/2026-02/q4-realty-income-12-31-25-10k.pdf',
                                 'method': 'Letzter Handelsschluss am oder vor dem 31. Dezember geteilt durch das '
                                           'berichtete verwässerte EPS desselben Geschäftsjahres; Median der fünf positiven '
                                           'Einzelwerte. Keine kursbereinigten Total-Return-Kurse. Jahresergebnis wurde '
                                           'erst nach dem Jahresende publiziert; rückblickende Bewertungsreferenz, keine '
                                           'handelbare zeitpunktgerechte Backtestreihe. ',
                                 'comparable_to_current': True,
                                 'comparison_note': 'GAAP EPS wird stark durch Immobilienabschreibungen, Impairments und '
                                                    'Veräußerungen beeinflusst; Kauf-Indikator mit P/AFFO statt GAAP-KGV '
                                                    'bewerten.'},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance / datierte Emittentenprüfung',
                                  'label': 'Hist. Div. 5J',
                                  'accounting_basis': 'Ex-Tag-Barausschüttungen',
                                  'currency': 'USD',
                                  'share_basis': '1 common share, USD0.01 par value. No ADR conversion.',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-21',
                                  'sample_count': 5,
                                  'window_count': 5,
                                  'source_url': 'https://finance.yahoo.com/quote/O/history/',
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-31',
                                                    'close': 71.58999633789062,
                                                    'dividends_per_share': 2.8479982,
                                                    'dividend_yield': 3.9782069362848014,
                                                    'currency': 'USD',
                                                    'share_basis': '1 common share, USD0.01 par value. No ADR conversion.',
                                                    'source_url': 'https://finance.yahoo.com/quote/O/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 63.43000030517578,
                                                    'dividends_per_share': 3.219,
                                                    'dividend_yield': 5.074885676356106,
                                                    'currency': 'USD',
                                                    'share_basis': '1 common share, USD0.01 par value. No ADR conversion.',
                                                    'source_url': 'https://finance.yahoo.com/quote/O/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 57.41999816894531,
                                                    'dividends_per_share': 3.311,
                                                    'dividend_yield': 5.766283708784061,
                                                    'currency': 'USD',
                                                    'share_basis': '1 common share, USD0.01 par value. No ADR conversion.',
                                                    'source_url': 'https://finance.yahoo.com/quote/O/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-31',
                                                    'close': 53.40999984741211,
                                                    'dividends_per_share': 2.872,
                                                    'dividend_yield': 5.377270189487106,
                                                    'currency': 'USD',
                                                    'share_basis': '1 common share, USD0.01 par value. No ADR conversion.',
                                                    'source_url': 'https://finance.yahoo.com/quote/O/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-31',
                                                    'close': 56.369998931884766,
                                                    'dividends_per_share': 3.49,
                                                    'dividend_yield': 6.1912365906147615,
                                                    'currency': 'USD',
                                                    'share_basis': '1 common share, USD0.01 par value. No ADR conversion.',
                                                    'source_url': 'https://finance.yahoo.com/quote/O/history/'}],
                                  'method': 'Median der tatsächlichen 12M-Cash-Ausschüttungen nach Ex-Tag bis zum letzten '
                                            'Jahreshandelsschluss geteilt durch diesen Kurs. Gemeldete Cash-Sonderzahlungen '
                                            'enthalten; keine künftige Zusage. Bei O ist Orion-Sachdividende ausgeschlossen '
                                            'und die synthetische 1,032-Rückrechnung aufgehoben.'},
                     'ps_ratio': {'provider': 'Yahoo Finance',
                                  'source_url': 'https://finance.yahoo.com/quote/O/key-statistics/',
                                  'role': 'informational_only',
                                  'note': 'Kein primärer Kaufmaßstab für diesen Reservewert; historische Vergleichsbasis '
                                          'nicht angesetzt. Sektorpassende Kennzahlen und Methoden stehen in '
                                          'primary_valuation/valuation_references.'}},
  'buy_trigger': 'Forward P/AFFO ≤ 13x und AFFO-Ausschüttungsquote ≤ 80%',
  'buy_trigger_type': 'proposal_not_backtested',
  'buy_trigger_rationale': 'Vorschlag: AFFO bildet bei Net-Lease-Immobilien den ausschüttungsrelevanten laufenden Ertrag '
                           'geeigneter ab als Abschreibungs-geprägtes GAAP EPS. 13x verlangt Abschlag zum '
                           'rekonstruierten14.35x-Median; 80% lässt Dividendenpuffer. Schwellen sind eigenes Urteil.',
  'buy_trigger_evaluation_policy': 'Forward P/AFFO ausschließlich mit frischer issuer-defined AFFO-Guidance derselben '
                                   'USD/common-share-Basis; AFFO-Payout aus tatsächlicher passender Periodenausschüttung, '
                                   'nicht aus GAAP EPS. Kein Signal aus unsicherer REIT-KGV-Grenze.',
  'buy_trigger_sources': [{'url': 'https://www.realtyincome.com/sites/realty-income/files/2026-02/q4-realty-income-12-31-25-10k.pdf',
                           'label': 'Emittentenbericht / aktuelle Kennzahlen'},
                          {'url': 'https://www.realtyincome.com/sites/realty-income/files/2026-11/realty-income-announces-operating-results-for-the-three-and-nine-months-ended-september-30-2026-v2.pdf',
                           'label': 'Emittentenbericht / aktuelle Kennzahlen'}],
  'classification_note': 'Hohe laufende Ausschüttung und breite Vermietung; Zins-/Refinanzierungs- und Mieterrisiken sowie '
                         'Kapitalerhöhungen berücksichtigen.',
  'current_primary_ttm': {'diluted_eps': 1.46,
                          'diluted_affo_per_share': 4.440000000000001,
                          'currency': 'USD',
                          'period_end': '2026-09-30',
                          'formula_eps': '1.17FY2025 +1.13nineMonths2026 −0.84nineMonths2025',
                          'formula_affo': '4.28FY2025 +3.35nineMonths2026 −3.19nineMonths2025',
                          'source_urls': ['https://www.realtyincome.com/sites/realty-income/files/2026-02/q4-realty-income-12-31-25-10k.pdf',
                                          'https://www.realtyincome.com/sites/realty-income/files/2026-11/realty-income-announces-operating-results-for-the-three-and-nine-months-ended-september-30-2026-v2.pdf'],
                          'price_date': '2026-11-20',
                          'price': 58.03,
                          'pe': 39.74657534246575,
                          'p_affo': 13.069819819819816,
                          'caveat': 'Synthetic TTM from rounded reported EPS/AFFO; not company-reported four-quarter '
                                    'share-weighted denominator.'},
  'dividend_policy': {'actual_paid_fy2025': {'value': 3.217,
                                             'unit': 'USD/common share',
                                             'period': 'FY2025 cash paid',
                                             'source_url': 'https://www.realtyincome.com/sites/realty-income/files/2026-02/q4-realty-income-12-31-25-10k.pdf'},
                      'actual_paid_9m2026': {'value': 2.4345,
                                             'unit': 'USD/common share',
                                             'period': '9M2026 cash paid',
                                             'source_url': 'https://www.realtyincome.com/sites/realty-income/files/2026-11/realty-income-announces-operating-results-for-the-three-and-nine-months-ended-september-30-2026-v2.pdf'},
                      'latest_monthly_declared': {'value': 0.274,
                                                  'unit': 'USD/common share',
                                                  'period': 'Declared2026-11-10',
                                                  'source_url': 'https://www.realtyincome.com/sites/realty-income/files/realty-income/press-releases/11%2010%2026%20137th%20Common%20Stock%20Monthly%20Dividend%20Increase%20Declared%20by%20Realty%20Income.pdf',
                                                  'note': 'Payable2026-12-15; record2026-11-30.'},
                      'latest_annualized_regular': {'value': 3.288,
                                                    'unit': 'USD/common share/year run rate',
                                                    'period': 'Declared2026-11-10',
                                                    'source_url': 'https://www.realtyincome.com/sites/realty-income/files/realty-income/press-releases/11%2010%2026%20137th%20Common%20Stock%20Monthly%20Dividend%20Increase%20Declared%20by%20Realty%20Income.pdf',
                                                    'note': '12timeslatestmonthly; not actual trailing cash or guaranteed '
                                                            'future dividend.'},
                      'special_corporate_action': {'date': '2021-11-12',
                                                   'type': 'Orion Office REIT noncash stock distribution',
                                                   'ratio': '1 ONL per10 O',
                                                   'source_url': 'https://www.sec.gov/Archives/edgar/data/1528059/000110465921000210/exhibit991-mergerclosingda.htm',
                                                   'total_return_note': 'Orion-Verteilung wurde separat als bewertete '
                                                                        'Sachdividende rekonstruiert. Der veröffentlichte '
                                                                        '5-Tage-VWAP dient als ausdrückliche '
                                                                        'Wiederanlage-Näherung; kein Bestandteil des '
                                                                        'regulären Cash-DPS-Wachstums.'},
                      'projection_note': 'Monatlicher regulärer Bruttocash; Wachstum nicht mit '
                                         'GAAP-Gewinnausschüttungsquote verwechseln. AFFO-Deckung separat anzeigen.'},
  'issuer_snapshot': {'as_of': '2026-09-30',
                      'verified_at': '2026-11-21',
                      'currency': 'USD',
                      'metrics': {'affo_per_share_q3': {'value': 1.12,
                                                        'unit': 'USD/common share diluted',
                                                        'period': 'Q3 2026',
                                                        'source_url': 'https://www.realtyincome.com/sites/realty-income/files/2026-11/realty-income-announces-operating-results-for-the-three-and-nine-months-ended-september-30-2026-v2.pdf'},
                                  'dividends_paid_q3': {'value': 0.813,
                                                        'unit': 'USD/common share',
                                                        'period': 'Q3 2026',
                                                        'source_url': 'https://www.realtyincome.com/sites/realty-income/files/2026-11/realty-income-announces-operating-results-for-the-three-and-nine-months-ended-september-30-2026-v2.pdf'},
                                  'affo_dividend_payout_q3': {'value': 72.6,
                                                              'unit': '%',
                                                              'period': 'Q3 2026',
                                                              'source_url': 'https://www.realtyincome.com/sites/realty-income/files/2026-11/realty-income-announces-operating-results-for-the-three-and-nine-months-ended-september-30-2026-v2.pdf'},
                                  'net_debt_ebitdare': {'value': 5.4,
                                                        'unit': 'x',
                                                        'period': '2026-09-30',
                                                        'source_url': 'https://www.realtyincome.com/sites/realty-income/files/2026-11/realty-income-announces-operating-results-for-the-three-and-nine-months-ended-september-30-2026-v2.pdf',
                                                        'note': 'Company pro forma annualized adjusted EBITDAre definition; '
                                                                'with unsettled forward equity5.2x.'},
                                  'occupancy': {'value': 98.8,
                                                'unit': '%',
                                                'period': '2026-09-30',
                                                'source_url': 'https://www.realtyincome.com/sites/realty-income/files/2026-11/realty-income-announces-operating-results-for-the-three-and-nine-months-ended-september-30-2026-v2.pdf'},
                                  'fy2026_affo_guidance': {'value': [4.45, 4.46],
                                                           'unit': 'USD/common share diluted',
                                                           'period': 'FY2026 guidance at2026-11-02',
                                                           'source_url': 'https://www.realtyincome.com/sites/realty-income/files/2026-11/realty-income-announces-operating-results-for-the-three-and-nine-months-ended-september-30-2026-v2.pdf'},
                                  'fy2026_eps_guidance': {'value': [1.55, 1.56],
                                                          'unit': 'USD/common share diluted',
                                                          'period': 'FY2026 guidance at2026-11-02',
                                                          'source_url': 'https://www.realtyincome.com/sites/realty-income/files/2026-11/realty-income-announces-operating-results-for-the-three-and-nine-months-ended-september-30-2026-v2.pdf'}},
                      'source_urls': ['https://www.realtyincome.com/sites/realty-income/files/2026-02/q4-realty-income-12-31-25-10k.pdf',
                                      'https://www.realtyincome.com/sites/realty-income/files/2026-11/realty-income-announces-operating-results-for-the-three-and-nine-months-ended-september-30-2026-v2.pdf'],
                      'refresh_policy': 'Datiertes Emittenten-Research; bei neuer Berichtsperiode erneut prüfen. Aktuelle '
                                        'Kurse, Dividendenhistorien und Renditen werden täglich aktualisiert.'},
  'valuation_references': {'p_affo': {'reference_version': 1,
                                      'provider': 'Emittenten-IR + Jahresendkurse',
                                      'label': 'Hist. P/AFFO 5J',
                                      'source_label': 'US-GAAP · IR / Kurse',
                                      'accounting_basis': 'Issuer-defined diluted AFFO per common share (non-GAAP)',
                                      'currency': 'USD',
                                      'share_basis': '1 common share',
                                      'period': 'FY2021–FY2025',
                                      'verified_at': '2026-11-21',
                                      'sample_count': 5,
                                      'window_count': 5,
                                      'observations': [{'fiscal_year': 2021,
                                                        'fiscal_end': '2021-12-31',
                                                        'price_date': '2021-12-31',
                                                        'close': 71.58999633789062,
                                                        'currency': 'USD',
                                                        'source_url': 'https://www.sec.gov/Archives/edgar/data/726728/000072672826000009/realtyincomeq42025supple.htm',
                                                        'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/726728/000072672826000009/realtyincomeq42025supple.htm',
                                                        'price_source_url': 'https://finance.yahoo.com/quote/O/history/',
                                                        'share_basis': '1 common share',
                                                        'diluted_affo_per_share': 3.59,
                                                        'p_affo': 19.941503158186805},
                                                       {'fiscal_year': 2022,
                                                        'fiscal_end': '2022-12-31',
                                                        'price_date': '2022-12-30',
                                                        'close': 63.43000030517578,
                                                        'currency': 'USD',
                                                        'source_url': 'https://www.sec.gov/Archives/edgar/data/726728/000072672826000009/realtyincomeq42025supple.htm',
                                                        'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/726728/000072672826000009/realtyincomeq42025supple.htm',
                                                        'price_source_url': 'https://finance.yahoo.com/quote/O/history/',
                                                        'share_basis': '1 common share',
                                                        'diluted_affo_per_share': 3.92,
                                                        'p_affo': 16.181122526830556},
                                                       {'fiscal_year': 2023,
                                                        'fiscal_end': '2023-12-31',
                                                        'price_date': '2023-12-29',
                                                        'close': 57.41999816894531,
                                                        'currency': 'USD',
                                                        'source_url': 'https://www.sec.gov/Archives/edgar/data/726728/000072672826000009/realtyincomeq42025supple.htm',
                                                        'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/726728/000072672826000009/realtyincomeq42025supple.htm',
                                                        'price_source_url': 'https://finance.yahoo.com/quote/O/history/',
                                                        'share_basis': '1 common share',
                                                        'diluted_affo_per_share': 4,
                                                        'p_affo': 14.354999542236328},
                                                       {'fiscal_year': 2024,
                                                        'fiscal_end': '2024-12-31',
                                                        'price_date': '2024-12-31',
                                                        'close': 53.40999984741211,
                                                        'currency': 'USD',
                                                        'source_url': 'https://www.sec.gov/Archives/edgar/data/726728/000072672826000009/realtyincomeq42025supple.htm',
                                                        'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/726728/000072672826000009/realtyincomeq42025supple.htm',
                                                        'price_source_url': 'https://finance.yahoo.com/quote/O/history/',
                                                        'share_basis': '1 common share',
                                                        'diluted_affo_per_share': 4.19,
                                                        'p_affo': 12.747016670026754},
                                                       {'fiscal_year': 2025,
                                                        'fiscal_end': '2025-12-31',
                                                        'price_date': '2025-12-31',
                                                        'close': 56.369998931884766,
                                                        'currency': 'USD',
                                                        'source_url': 'https://www.sec.gov/Archives/edgar/data/726728/000072672826000009/realtyincomeq42025supple.htm',
                                                        'eps_source_url': 'https://www.sec.gov/Archives/edgar/data/726728/000072672826000009/realtyincomeq42025supple.htm',
                                                        'price_source_url': 'https://finance.yahoo.com/quote/O/history/',
                                                        'share_basis': '1 common share',
                                                        'diluted_affo_per_share': 4.28,
                                                        'p_affo': 13.170560498103917}],
                                      'source_url': 'https://www.sec.gov/Archives/edgar/data/726728/000072672826000009/realtyincomeq42025supple.htm',
                                      'method': 'Wie P/E-Jahresend-Median, mit issuer-defined verwässertem AFFO/Aktie als '
                                                'Nenner; fünf FY2021–FY2025-Werte. AFFO ist eine unternehmensdefinierte '
                                                'Non-GAAP-Kennzahl, kein IFRS-/GAAP-Gewinn. Jahresergebnis wurde erst nach '
                                                'dem Jahresende publiziert; rückblickende Bewertungsreferenz, keine '
                                                'handelbare zeitpunktgerechte Backtestreihe. ',
                                      'comparable_to_current': True,
                                      'comparison_note': 'GAAP EPS wird stark durch Immobilienabschreibungen, Impairments '
                                                         'und Veräußerungen beeinflusst; Kauf-Indikator mit P/AFFO statt '
                                                         'GAAP-KGV bewerten.'}},
  'hist_p_affo': 14.354999542236328,
  'regular_dividend_reference': {'value': 3.288,
                                 'currency': 'USD',
                                 'as_of': '2026-11-10',
                                 'kind': 'latest_declared_payment_annualized_not_guaranteed',
                                 'source_url': 'https://www.realtyincome.com/sites/realty-income/files/realty-income/press-releases/11%2010%2026%20137th%20Common%20Stock%20Monthly%20Dividend%20Increase%20Declared%20by%20Realty%20Income.pdf'},
  'hist_div': 5.377270189487106,
  'reference_set': 'income-universe-2026',
  'tranche_policy': 'individual_only',
  'active_in_portfolios': False,
  'prepared_for': 'future_custom_portfolio_simulation',
  'logo': {'source_url': 'https://www.realtyincome.com/sites/realty-income/files/realty-income/favicon.png',
           'homepage': 'https://www.realtyincome.com/',
           'mime_type': 'image/png',
           'data_uri': 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGQAAABkCAYAAABw4pVUAAASIElEQVR42uxaXWgcVRSe2mp/bKnVpNk95ybGNiYpu/ecxCCotNQqog8igiktgvgsiCCCRAQFnwSlthqfBB9UpFr6VIMKQhGtYG3FB0vxKYnRuE72/swkaQhJdtybMFBD18Ymk2SS+eCw7Mssc7773e9+566XIUOGDBkyZJhD5HkbRqCtzaL8zAfu7W9u3pJ1ZYUwup12K5BvGKDBADkyyFMqL0+FQG1Zd5YRQ57YqoGOGpTnQ+QoEBwppEgjR2OCZzTQz2GeDmSdShhnPW+TarznUZ2XfRopcqpwn+qqct8tchQilbSQL0eetzHrXBI+UcetPsh3DbIdFzER166YLCt4UgO9P9gkd2VdXCKUtu91PvFagPR7KDgyrukLLFOtEDumNchvFdJ9WTcXpQrY5uc6uy3Ii9b5BPJ1CKipFucrFY08oFAedmrLuvs/4Pb8EtJDCopfa6CZeT5xw6QY9xzgcYV8TO/aszPr9AJ8IqgvtGjgXoNsxmKfWMKy1RoVHVMmT6eHQbZnXa+BcEdrnYLi6wZoYPS6PrF4tYTIFYv0W4j8eNb9f6miZbMCPmKx+ONVeSLxik9hBkmXBfe4dL/ufULBvkcUyC8MUqWGTyRe1pVL90AnbYPcs07zRHurAjqhQQbz8sSKlInTPcqLYRPv99YLhqs+oZFetUCDi/CJ5HxFcMUAlRTKFz/3Dm9cu0R4sE0JesrEeULw6iCixsjFNvKkATpRaqDda84nRnKdBxXILw3QtI19YpWXmT0a80wZ5A9/5TruXRM+4dfvu9sH2WtQ2rF4e0pR6TlSKgpp0CA/k9p0/8eO9jvsrE9w/4r7xNKke1dXlKC3THPHbWlSxWYf+Iip5olAxD5Rc+VF40n6SBLpHjumNFLfCHLrqvcJg4VDZaAzGmXNPGGQZkkyyO7FziiQH2qkiknXFjajgS4rpO7VOXeq+kTZ5QnBQTx3qjGmiCzytAI+V4LC00Pi/q1zN37yzRB5Mk2khO49gQNfyJ5SA926anxCY/EVA1xz7qTnFOHImDHI58tNhWdH6tp2zB+bjGDxpQBpwiKlagsLkKfK+eLpiXznnSt6j+0jdRugC3GeqHWWd8lXOXm7hovC7V4NXPIKtxhRfMGCDGxafCVO926xgbxQxs6Hl90n/DwdUHnZZ5Cm//semysaaMAH2aOQxAKfv0kDHQ2QVIApIGX+NTGyr4GfP+s9uGkZyOi6uYzF5wxSnCdqzYIqBnlIIb9tbkDGjhSVc+qTpVBw6o7GVsym+w+GoasuYUKat5Sh8F6IfG1FOJMTpA3SO6qJC4s+seUKhwKkgXm/l5J07+7u6fu/oeMBLyn0VwkxWDxukeffUTtzMxrkJz5wZ5xkl2bcIg9akJfTpJT4P2FX5rzzp9FcZ33ihFikWBGTFuikD7w/Ic+6aaxBSgPylzBlnmKRK0rIS2XBmCAhdHw8DnVAXylBj7nTkZcgHCmlKikait+FyCkjhH51hCTmIQrlMQ3ynAF6YkiIrcsZPksNdJcC+Y0jRWeEzK1UW22K3tW1c6UmAn/C3kabp1MuhOo0EbKWEeVa6nVefhy4F1/XCom8DRMfdTVNftr15D/sm19oHEUcx4/GEv8VUkRaq5WIKaWa0CS7M5uUiMYHaQwUrBSxD0LxpRWL//oQ2ip5qA99sDYtDUaR2LvbmbvNP5XYB6kGFB/EIIpV1KJUbYtYm0tt7rI7M3fjd2PusneJiUnM0+3CPGRndgK/D7/5fn+/3VvqUP7g9DE3Zu7QztLdR6qquiq1obYrD6U8gfQYqxUje/SQpfXAEsYgxlCT1v3U9Rg9ogfpbZFlXN9HNq/Bm7yj6KPpVDkDUQmiBVv80A7NSU4uCps+FfmfrvNoSsJkdMCCZ8bKEYhIGPuyCbooEBJD99EsnhvNnKbNK2HHr+L4QgBCIAvC4FOZIQFl0H3HuLd0z1HsqUeN1ZGlXcEu8f4QyAIjl6RaMtMTcfL6WE+xVdZOpEKetlq9OHlCO8srLDWA+N9PhUDm1wudTZA/hE32+e6ssEdnZJWIUhN7RCHsw2Nwbf79EMgKAgGMrExY37mMbA9aZre3fhPmT2WT5Ipk9Gu339rkz4VAVgJIQbwtJZk54kXN2nx1rTnZ6CWMVxSjv+nBppzg5q+CNzf48yGQFQKicA+aIUTcePP64D+fXY53160VduMLOLrOacfSOQxkxyU3ZrTP1Zbx6xLd2bkqBLJMIPgbg4xLTg9OrTvRVqm4sVty8nkOc9qh08BIStjmrBrkco9xs2CNeyQzWsIMWYy9hBtC4J8JAsERlROc/JJGKwSCfYPi5g6AODttd/PWF8BMVzLzENYUZcAEq10nGH0D4n/cd15LBZLCBxTllyEAIhl9MQ8ExZ4CjM8y8cYmwQ1LcZJEYAXuA0JAVxwyZX21s6siKPJetHGLcqyPoSnn/R7Zv2Xlf6nW8cLsYNkCgVZolSCesonjssbtANSN42gc2QIQJY6r35KoNZI6OvMhmZ9Jboy2A+YPmM8A5t78XLBOEZyambesu0Ig8wGxyQEAGZe2OSBs6ziC/rufEbk5hB6AlGDmh+mYcUchePtrKn0AKmGm0GyUrk3f1WdqKmcXjUarx8lhfaKmMgQyDxAE+HmPmZcQrJ+nxJrPU4tw8qVrz9Qaqd76KsHpSeiQl0v6jsu8nEaRWNri19GmLRD/s5Jte7ho7kxbpT7WfFMIZLaG5JAR87VMcoD2k2RWSyHIACMT5tB0Ba8VdEUm6NE5/sd6iP8Hnk0Giu6PwDDEzbbJKL0nBDIbyEL9qyuKG+354+e6bbTiuXO6b9oMJEjWY+SLCWatK9o/+sgtnm3Y0KMJGaMPBNstEzG6FZDf/muOdyj+z5hDIMUgAvaWXBNxurdQt9jm04pDZxyrsAbZdQ0ZsLNo797qGz1OO1HJSwAd1s7M0aRP3X+rhIEQnPQWPQNQXrzhvsyxrXfiZ8wdIZASGDpJJ2WcdEytfW/zGs+mr+ZwD2Mmg+C6FCP82050eIPaZNNn/bUKhkEx88mgI1NJ83GATrvQr1kQGT0y1mXcXb5AODlQCkRioC0iBCfdelekQqOmkNzs0wChAmsRVF9bLgib1Of3HPEDbluPYs8xWGCFdSN+duT7YJND9dUeI1/B1f05yZofLMoOx6gTjH509TWysTyBwIJCiA+VAAEMKhFk7mcFgG0T0AdfvGWp80IGuIy8FAxqxmloRnZcwB7+fNrjdHcwA7Bvlx6gWS9GvtFDD1UVZxV5zrPJj+mTDRtS6+teLk8gSXI4CATaoEScfKKdmtszduNOZMRF1B9z1iTSNj/Vwy1rC13g9+nf7Z17iFRVHMeXiKKX9BBadUy3LHrszG577znTg4LoAQVBBRFI1B9B/dHrj8LIkqLAlF6oZZGYqPfeM3ddHyDaFoakRGZpVqKSqeEDn+uM7uzO3Necvr87e6/j7Ow4Wu6s7j0wzMzunTPw+8zv9zu/3+/c32miX78fx5jMg4DXy847rw4WA9n57AHP5Ag4KV5hHQ0lg3as4PpV+Pzf8FWjuhrj7w17IBJCdAT/hUyQlVLfAozuglkhEyzIx/AuynOdEChrtOC8gzQLNCQHn/NG6f/x2e8JLmmWpfFJJwWOevI+xDF5Wr1FQIqp9gIEvKN3HrTC5F9QbOH1gxGaNNsVyuxwHm3CCDj2OcHKy4VvsQVMkpaMBd9DgKUZLpOzOTN530mmTCBd08ELAPLnsAci2wHDZHtgv9+D817t57D6gwi0CEJj26TOrg+Fjawv4EG7AnPG8jBnk8MSr8YYAB0k8J5AkKmru3OidXxo6tppsx7bUVgUAbkYQpwCbchamtJpkaAX8arbf7x23kPplhP1FPY85rDcVFhL8VdegNzkX/Np65WWYO1YcYUZZTx/d3ju3VcEu1RcjU+EVtrwOxEQR2dToBm9ENJxCPNUu05cCGwFZXrJ7ueR+nBSLF2adoHgLQCbKWXDBRSbeH4BjHmBxiFQtKAtJeYuOcI2FI12QrqpCEjoQxzBqsIgDYBgD1A+i8yQ1FXuCP4XNOHENXgA0F5Ks/v5rlTLTRaWwIHvcIoahPR88sXQnKEW7wp2xBU8AtIHZEpN24AWcQvPn5LNzy+OXw8Y68uXwxA8BZNfFcu4oy91U/zL0mvwGUmZZYJa5sPo/xEQqls4BptaOJWpouShYJt6BRsrDWWkJdTFME39816CHba15B2UyYVmPOkKZpVqnkfzGHzT0fbiJju55OZr8PeN0jy3gOB9rM5AeLa4IzF2Cd7PCc1UeSXRYB0SfqNXNI/FvH/gunIf5Oe9gtgDRa2HoVE9AHcSkMOI1DOj4h8MNSBpAMHrbegcNL5uQGCqHGjH4u20vNXUSSQ0V/QPFCHotCX44zSnpbP3K5Z/ERBSMtP/bpg02+AfyyWkaf2BpBubPxqKQI6g/WxmXKKpLkBoCQuh7rEX3K1C4E/DJ/S4qYoa5EI7Omm+rM4ewDXHHNF/TgoIHZ3d68ceC9smQDt2eCaPgNQKRFL6Q1PeJCfsmeyQNHnl2MRkPXQnlZzPySesqWTSPIreDWWXXHzXOD9+EepzffNFQGoBApNDv/ofHUN9EFqyVbYPmEbxYHrWUmaYsrX0HnNWnA/zfIOs7uVUoKJEYgSkRiDkEwqIDfKa8pot2OoARmXtQOSe4i9Jg92G9/sKZuVr4SssQPisGMPcyQGny0udW0COFVdZOzPXnaWmzJTUo40JSAqWr4Zsa6G6FDFDR7VUCqB5tGz1N18LdRm0YMBSsIMKIZWCt/vpGnVyIVg2lwMRfEtuFm862tj84VABkgmPxEjsPTw28bo8W63LyZZTagMa0eMXoESfrdfZAQjtW4eEWT2VkoMGvQI/MxEO3+7vyEONwy9f2du7QElK46aRMHHr8CMYwM+wrfmZyg1DAUj6RA/5niOjW7SuRjTgOdtDroaWYGUETemEQGyAIaEchO+wqsGAIy8A2haJz5IQ4TuqB5YG+01+eO1leZM9CkDkZ4YokLDdH0D4HYDWHBzd8tCgn3VFpsQ2lGehHRvI2fq+oBqQdk5JwhnQjNmVfu3lJhDz6lRb958J3hAFkunr6Yvv3g5/8Sq1Lqxr80uJe0HyhvKyZfBdEHRFMG7RrO3IadyvKgaBYjXT5mnqi8f0W26E095bSFUHIn0g8VkEZJCb9tPrNBpEf0x9WBrqO8q3fyaaKJKG8A8RGFeUCo7bSL0vcwRfW8Xpl1yvdtNuepvuiffLxKwGIM2fA8igmKeSQytX+j0Wh+IIdxjqra3Qhq9dCFV2FMHQktUW6qoQUnXtKADe9uz8tgT80tIgKVlXIGGysFV2+03J4r+jP/EzMla2z3gIg7mwx1QfsXTW6Qnm5TU4fVHFVJUlHfNCXZlfSAGm2o3P1R1I2u9mTcdX+IdTTv9n5C2jGs7FQU7Z0tqesgX7CcKTZc554IAQzt/S2NTg+joBCVuhY94sWvWJbKwtcX60U9LjVzla2yTP5NtIyNXu4nWhSZZQ3rZ1/mvBrBuQoIWhh96J6zI47+Rd3JR63h1vlPN3iyhTLYPtJ99QKRUCM7fTE2yag+ATj8EGEkbZaJu+O41l7PExN1/TcD4PPzdlJBMwSbprsuOlN/2Qv7EF32zrTAuc+SABCaNsvO6FeZoDXxFvGE6DKoA5we8BjOXQlFxfgcq1dXUDnvdBWwYBSBhlA0arg45CnThV9P5hAaBqD652ZaIt2HrP9AW8O9CY0wQy+3SBZEqOnUjHWl6Wo5VLo9Mk+wYVq/IL216wU+oWSsXgUTOQ/CdtEzKN8bm1AkkX/YQEiEM4/Wc6/haLCAzg+NP67eMcnU2DsPcXO0DUBgS3I8wDkBqCO4CgKHtM8wpq/hxJvUbH34Vb1WydzwaQDDl4R5wZkBAENALBHR0YuRH7pJ6Q6DkcSfoMHL+TSj7k6MpycvjS5ABTO5AwnihG2Qdx+s/07HjWGEn2v4O5yDL4Y47BfiAIMGVVgJRH2YluLGMNZGObI0n+/zX9EVS3Rzp+M0FB5rgcSOgnjmH1BBg/H8I5JNT6KZLe2azBoFWHJdR3qFZiGWxXfkbrjQSku+8wGWjELioW7cHxSpHEBjXib7ktJ9gUOU257sioxIJMjGrZ8bnZmBKPJFTHQbs6Doy5dTLiikcjaUQjGtGIRjSG6/gXf8K4g89ZMVAAAAAASUVORK5CYII='},
  'research_verified_at': '2026-11-21'},
 {'name': 'TotalEnergies SE',
  'ticker': 'TTE.PA',
  'isin': 'FR0000120271',
  'exchange': 'Euronext Paris',
  'currency': 'EUR',
  'quote_currency': 'EUR',
  'financial_currency': 'USD',
  'asset_type': 'stock',
  'instrument_subtype': 'integrated_energy',
  'cluster': 'Integrierte Energie',
  'share_basis': '1 Paris ordinary share; no ADR multiplier. NYSE ADS programme ended with ordinary-share listing '
                 'from2025-12-08; do not splice USD/EUR histories.',
  'identity_sources': ['https://totalenergies.com/fr/investisseurs/action-dividende/laction-totalenergies/actions-totalenergies-cotees-sur-le-nyse'],
  'hist_pe': 8.635922157262907,
  'hist_ps': None,
  'hist_ps_not_used': {'status': 'not_used_not_required_for_primary_valuation',
                       'checked_source_url': 'https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf',
                       'checked_source_pages': [453, 495, 636],
                       'available_consolidated_years': [2023, 2024, 2025],
                       'revenue_after_excise_usd_million': [218945, 195610, 182344],
                       'diluted_weighted_shares': [2433767578, 2314560722, 2214350417],
                       'excluded_five_year_table': {'page': 636,
                                                    'scope': 'Statutory financial statements of parent TotalEnergies SE, '
                                                             'not consolidated group',
                                                    'net_commercial_sales_2025_eur_million': 1903,
                                                    'reason': 'Do not substitute parent sales/EPS or year-end shares from '
                                                              'this five-year table for group revenue and diluted weighted '
                                                              'group shares.'},
                       'additional_source_check': {'url': 'https://totalenergies.com/sites/g/files/nytnzq121/files/documents/totalenergies_factbook-2025_2026_en.pdf',
                                                   'result': 'Web fetch exceeded27MB tool size limit; no unverified '
                                                             'revenue/share/FX series was inferred from unavailable full '
                                                             'text.'},
                       'remaining_requirement': 'For a full2021–25 reference: compatible2021/22 consolidated sales and '
                                                'diluted weighted shares plus each year official average USD/EUR, using '
                                                'revenue after excise consistently.2025 URD provides a directly comparable '
                                                'three-year set only.',
                       'valuation_choice': 'Primary reserve valuation uses basis-consistent IFRS P/E and '
                                           'cashflow/capital-return coverage. No mixed EUR-price/USD-sales multiple or '
                                           'shortened five-year reference is stored.'},
  'primary_valuation': 'pe_and_cashflow',
  'field_metadata': {'hist_pe': {'reference_version': 1,
                                 'provider': 'Emittenten-IR + Jahresendkurse',
                                 'label': '5J-Median · JE',
                                 'source_label': 'IFRS · IR / Kurse',
                                 'accounting_basis': 'IFRS diluted EPS',
                                 'currency': 'EUR',
                                 'share_basis': '1 Paris ordinary share',
                                 'period': 'FY2021–FY2025',
                                 'verified_at': '2026-11-21',
                                 'sample_count': 5,
                                 'window_count': 5,
                                 'observations': [{'fiscal_year': 2021,
                                                   'fiscal_end': '2021-12-31',
                                                   'price_date': '2021-12-31',
                                                   'close': 44.630001068115234,
                                                   'currency': 'EUR',
                                                   'source_url': 'https://totalenergies.com/sites/g/files/nytnzq121/files/documents/2023-03/TotalEnergies_URD_2022_EN.pdf',
                                                   'eps_source_url': 'https://totalenergies.com/sites/g/files/nytnzq121/files/documents/2023-03/TotalEnergies_URD_2022_EN.pdf',
                                                   'price_source_url': 'https://finance.yahoo.com/quote/TTE.PA/history/',
                                                   'share_basis': '1 Paris ordinary share',
                                                   'diluted_eps': 5.01,
                                                   'pe': 8.908183845931186},
                                                  {'fiscal_year': 2022,
                                                   'fiscal_end': '2022-12-31',
                                                   'price_date': '2022-12-30',
                                                   'close': 58.650001525878906,
                                                   'currency': 'EUR',
                                                   'source_url': 'https://totalenergies.com/system/files/documents/2024-03/totalenergies_universal-registration-document-2023_2023_en_pdf.pdf',
                                                   'eps_source_url': 'https://totalenergies.com/system/files/documents/2024-03/totalenergies_universal-registration-document-2023_2023_en_pdf.pdf',
                                                   'price_source_url': 'https://finance.yahoo.com/quote/TTE.PA/history/',
                                                   'share_basis': '1 Paris ordinary share',
                                                   'diluted_eps': 7.45,
                                                   'pe': 7.8724834262924706},
                                                  {'fiscal_year': 2023,
                                                   'fiscal_end': '2023-12-31',
                                                   'price_date': '2023-12-29',
                                                   'close': 61.599998474121094,
                                                   'currency': 'EUR',
                                                   'source_url': 'https://totalenergies.com/system/files/documents/2024-03/totalenergies_universal-registration-document-2023_2023_en_pdf.pdf',
                                                   'eps_source_url': 'https://totalenergies.com/system/files/documents/2024-03/totalenergies_universal-registration-document-2023_2023_en_pdf.pdf',
                                                   'price_source_url': 'https://finance.yahoo.com/quote/TTE.PA/history/',
                                                   'share_basis': '1 Paris ordinary share',
                                                   'diluted_eps': 8.02,
                                                   'pe': 7.680797814728316},
                                                  {'fiscal_year': 2024,
                                                   'fiscal_end': '2024-12-31',
                                                   'price_date': '2024-12-31',
                                                   'close': 53.369998931884766,
                                                   'currency': 'EUR',
                                                   'source_url': 'https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf',
                                                   'eps_source_url': 'https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf',
                                                   'price_source_url': 'https://finance.yahoo.com/quote/TTE.PA/history/',
                                                   'share_basis': '1 Paris ordinary share',
                                                   'diluted_eps': 6.18,
                                                   'pe': 8.635922157262907},
                                                  {'fiscal_year': 2025,
                                                   'fiscal_end': '2025-12-31',
                                                   'price_date': '2025-12-31',
                                                   'close': 55.59000015258789,
                                                   'currency': 'EUR',
                                                   'source_url': 'https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf',
                                                   'eps_source_url': 'https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf',
                                                   'price_source_url': 'https://finance.yahoo.com/quote/TTE.PA/history/',
                                                   'share_basis': '1 Paris ordinary share',
                                                   'diluted_eps': 5.12,
                                                   'pe': 10.857421904802322}],
                                 'source_url': 'https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf',
                                 'method': 'Letzter Handelsschluss am oder vor dem 31. Dezember geteilt durch das '
                                           'berichtete verwässerte EPS desselben Geschäftsjahres; Median der fünf positiven '
                                           'Einzelwerte. Keine kursbereinigten Total-Return-Kurse. Jahresergebnis wurde '
                                           'erst nach dem Jahresende publiziert; rückblickende Bewertungsreferenz, keine '
                                           'handelbare zeitpunktgerechte Backtestreihe. Direkt vom Emittenten berichtetes '
                                           'verwässertes EUR-EPS: IFRS-USD-EPS vom Emittenten zum jeweiligen '
                                           'Jahresdurchschnittskurs übersetzt; keine eigene Spotumrechnung. '
                                           'Konzernabschluss bleibt USD.',
                                 'comparable_to_current': True,
                                 'comparison_note': 'Integrierter Energie-/LNG-/Stromkonzern mit hoher Cashgenerierung; '
                                                    'Öl-/Gas-/Raffineriezyklus bleibt wesentlich.'},
                     'hist_div': {'reference_version': 1,
                                  'provider': 'Yahoo Finance / datierte Emittentenprüfung',
                                  'label': 'Hist. Div. 5J',
                                  'accounting_basis': 'Ex-Tag-Barausschüttungen',
                                  'currency': 'EUR',
                                  'share_basis': '1 Paris ordinary share; no ADR multiplier. NYSE ADS programme ended with '
                                                 'ordinary-share listing from2025-12-08; do not splice USD/EUR histories.',
                                  'period': 'FY2021–FY2025',
                                  'verified_at': '2026-11-21',
                                  'sample_count': 5,
                                  'window_count': 5,
                                  'source_url': 'https://finance.yahoo.com/quote/TTE.PA/history/',
                                  'observations': [{'fiscal_year': 2021,
                                                    'price_date': '2021-12-31',
                                                    'close': 44.630001068115234,
                                                    'dividends_per_share': 2.64,
                                                    'dividend_yield': 5.915303465869915,
                                                    'currency': 'EUR',
                                                    'share_basis': '1 Paris ordinary share; no ADR multiplier. NYSE ADS '
                                                                   'programme ended with ordinary-share listing '
                                                                   'from2025-12-08; do not splice USD/EUR histories.',
                                                    'source_url': 'https://finance.yahoo.com/quote/TTE.PA/history/'},
                                                   {'fiscal_year': 2022,
                                                    'price_date': '2022-12-30',
                                                    'close': 58.650001525878906,
                                                    'dividends_per_share': 3.67,
                                                    'dividend_yield': 6.257459342742963,
                                                    'currency': 'EUR',
                                                    'share_basis': '1 Paris ordinary share; no ADR multiplier. NYSE ADS '
                                                                   'programme ended with ordinary-share listing '
                                                                   'from2025-12-08; do not splice USD/EUR histories.',
                                                    'source_url': 'https://finance.yahoo.com/quote/TTE.PA/history/'},
                                                   {'fiscal_year': 2023,
                                                    'price_date': '2023-12-29',
                                                    'close': 61.599998474121094,
                                                    'dividends_per_share': 2.86,
                                                    'dividend_yield': 4.642857257864253,
                                                    'currency': 'EUR',
                                                    'share_basis': '1 Paris ordinary share; no ADR multiplier. NYSE ADS '
                                                                   'programme ended with ordinary-share listing '
                                                                   'from2025-12-08; do not splice USD/EUR histories.',
                                                    'source_url': 'https://finance.yahoo.com/quote/TTE.PA/history/'},
                                                   {'fiscal_year': 2024,
                                                    'price_date': '2024-12-31',
                                                    'close': 53.369998931884766,
                                                    'dividends_per_share': 3.06,
                                                    'dividend_yield': 5.733558293500112,
                                                    'currency': 'EUR',
                                                    'share_basis': '1 Paris ordinary share; no ADR multiplier. NYSE ADS '
                                                                   'programme ended with ordinary-share listing '
                                                                   'from2025-12-08; do not splice USD/EUR histories.',
                                                    'source_url': 'https://finance.yahoo.com/quote/TTE.PA/history/'},
                                                   {'fiscal_year': 2025,
                                                    'price_date': '2025-12-31',
                                                    'close': 55.59000015258789,
                                                    'dividends_per_share': 4.13,
                                                    'dividend_yield': 7.429393755466171,
                                                    'currency': 'EUR',
                                                    'share_basis': '1 Paris ordinary share; no ADR multiplier. NYSE ADS '
                                                                   'programme ended with ordinary-share listing '
                                                                   'from2025-12-08; do not splice USD/EUR histories.',
                                                    'source_url': 'https://finance.yahoo.com/quote/TTE.PA/history/'}],
                                  'method': 'Median der tatsächlichen 12M-Cash-Ausschüttungen nach Ex-Tag bis zum letzten '
                                            'Jahreshandelsschluss geteilt durch diesen Kurs. Gemeldete Cash-Sonderzahlungen '
                                            'enthalten; keine künftige Zusage. Bei O ist Orion-Sachdividende ausgeschlossen '
                                            'und die synthetische 1,032-Rückrechnung aufgehoben.'},
                     'ps_ratio': {'provider': 'Yahoo Finance',
                                  'source_url': 'https://finance.yahoo.com/quote/TTE.PA/key-statistics/',
                                  'role': 'informational_only',
                                  'note': 'Kein primärer Kaufmaßstab für diesen Reservewert; historische Vergleichsbasis '
                                          'nicht angesetzt. Sektorpassende Kennzahlen und Methoden stehen in '
                                          'primary_valuation/valuation_references.'}},
  'buy_trigger': 'Forward P/E ≤ 9x und reguläre Dividendenrendite ≥ 5%',
  'buy_trigger_type': 'proposal_not_backtested',
  'buy_trigger_rationale': 'Vorschlag: Bei integriertem Energiegeschäft Bewertungsdisziplin mit mindestens5% regulärem '
                           'Cash-Ertrag verbinden. Rohstoff- und Raffineriezyklen können das KGV kurzfristig drücken; 9x/5% '
                           'sind eigene Sicherheitsabstände, keine belegte optimale Einstiegsregel.',
  'buy_trigger_evaluation_policy': 'Forward-Bedingung nur nach Prüfung von Schätzperiode, EUR-/USD-Umrechnung und '
                                   'Ordinary-Share-Basis. Bei ungeklärter Analystenbasis kein positives automatisches '
                                   'Signal; aktuelle IFRS-EUR-TTM-Rekonstruktion ist kein Forward-EPS-Ersatz.',
  'buy_trigger_sources': [{'url': 'https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf',
                           'label': 'Emittentenbericht / aktuelle Kennzahlen'},
                          {'url': 'https://www.sec.gov/Archives/edgar/data/879764/000110465926120831/tm2628570d1_ex99-9.htm',
                           'label': 'Emittentenbericht / aktuelle Kennzahlen'}],
  'classification_note': 'Integrierter Energie-/LNG-/Stromkonzern mit hoher Cashgenerierung; Öl-/Gas-/Raffineriezyklus '
                         'bleibt wesentlich.',
  'current_primary_ttm': {'diluted_eps': 8.626151385897959,
                          'currency': 'EUR',
                          'period_end': '2026-09-30',
                          'label': 'IR rekonstruiert (ca.)',
                          'formula': '5.12EUR FY2025 + (8.73USD 9M2026 /1.1610USDperEUR) − (4.49USD 9M2025 '
                                     '/1.1188USDperEUR)',
                          'inputs': {'fy2025_diluted_eps_eur': 5.12,
                                     'fy2025_diluted_eps_usd': 5.78,
                                     'nine_months_2026_diluted_eps_usd': 8.73,
                                     'nine_months_2025_diluted_eps_usd': 4.49,
                                     'nine_months_2026_average_usd_per_eur': 1.161,
                                     'nine_months_2025_average_usd_per_eur': 1.1188},
                          'ttm_usd_eps': 10.020000000000001,
                          'fx_method': 'Same period-average translation convention as the issuer annual EUR EPS; USD9M GAAP '
                                       'EPS divided by official period-average FX; not current spot FX. Not a full EUR '
                                       'financial statement.',
                          'rounding_caveat': 'Approximately reconstructed from two-decimal EPS and four-decimal average FX. '
                                             'Sum/difference of separately translated EPS uses varying period diluted share '
                                             'counts; not an issuer-published exact TTM EUR EPS.',
                          'source_urls': ['https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf',
                                          'https://www.sec.gov/Archives/edgar/data/879764/000110465926120831/tm2628570d1_ex99-9.htm'],
                          'source_pages': 'FY2025 USD dilutedEPS URDp453; FY2025/2024EUR dilutedEPS URDp495; 9M2026/25USD '
                                          'dilutedEPS Q3releasep30; averageFX Q3releasep4 footnote5.',
                          'price_date': '2026-11-20',
                          'price': 78.19,
                          'pe': 9.06429721692864,
                          'vendor_comparison': {'yahoo_paris_trailing_eps': 8.52,
                                                'yahoo_paris_pe': 9.177230046948358,
                                                'yahoo_nyse_ttm_eps_usd': 10.02,
                                                'note': 'Vendor Paris EPS8.52 differs from the disclosed period-average '
                                                        'reconstruction. financialCurrency=USD alone does not establish an '
                                                        'EUR/USD error. Keep vendor raw fields and provenance; do not '
                                                        'silently overwrite or spot-multiply forward estimate.'}},
  'dividend_policy': {'regular_fy2025': {'value': 3.4,
                                         'unit': 'EUR/ordinary share',
                                         'period': 'FY2025 declared',
                                         'source_url': 'https://totalenergies.com/investors/shares-and-dividends/dividends'},
                      'latest_interim': {'value': 0.9,
                                         'unit': 'EUR/ordinary share',
                                         'period': 'FY2026 third interim declared2026-10-29',
                                         'source_url': 'https://totalenergies.com/investors/shares-and-dividends/dividends',
                                         'note': 'Ex2027-03-31; cash2027-04-02Paris. First2026interim.90already '
                                                 'paid2026-10-02; second.90ex2026-12-31.'},
                      'annualized_regular_assumption': {'value': 3.6,
                                                        'currency': 'EUR/share',
                                                        'formula': '4×latest.90interim',
                                                        'not_declared_fy_total': True,
                                                        'source_url': 'https://totalenergies.com/investors/shares-and-dividends/dividends'},
                      'official_ttm_gross_cash': {'value': 3.45,
                                                  'currency': 'EUR/share',
                                                  'period': '12 months to2026-11-20',
                                                  'components': [{'ex': '2025-12-31', 'cash': 0.85},
                                                                 {'ex': '2026-03-31', 'cash': 0.85},
                                                                 {'ex': '2026-06-30', 'cash': 0.85},
                                                                 {'ex': '2026-09-30', 'cash': 0.9}],
                                                  'source_url': 'https://totalenergies.com/investors/shares-and-dividends/dividends'},
                      'special_history_example': {'fiscal_year': 2022,
                                                  'ordinary': 2.81,
                                                  'special': 1.0,
                                                  'total': 3.81,
                                                  'currency': 'EUR/share',
                                                  'special_ex_date': '2022-12-06',
                                                  'source_url': 'https://totalenergies.com/investors/shares-and-dividends/dividends'},
                      'projection_note': 'Ordentliche EUR-Dividende separat von2022Sonderdividende. Historische '
                                         'Gesamtrendite darf Sondercash enthalten, regelmäßiges künftiges '
                                         'Dividendenwachstum nicht ungeprüft daraus ableiten.'},
  'issuer_snapshot': {'as_of': '2026-09-30',
                      'verified_at': '2026-11-21',
                      'currency': 'USD',
                      'metrics': {'cfo_ifrs': {'value': 29609,
                                               'unit': 'USD million',
                                               'period': '9M2026',
                                               'source_url': 'https://www.sec.gov/Archives/edgar/data/879764/000110465926120831/tm2628570d1_ex99-9.htm'},
                                  'cffo_ex_working_capital': {'value': 30635,
                                                              'unit': 'USD million',
                                                              'period': '9M2026',
                                                              'source_url': 'https://www.sec.gov/Archives/edgar/data/879764/000110465926120831/tm2628570d1_ex99-9.htm'},
                                  'net_investments': {'value': 11204,
                                                      'unit': 'USD million',
                                                      'period': '9M2026',
                                                      'source_url': 'https://www.sec.gov/Archives/edgar/data/879764/000110465926120831/tm2628570d1_ex99-9.htm'},
                                  'net_cashflow': {'value': 19431,
                                                   'unit': 'USD million',
                                                   'period': '9M2026',
                                                   'source_url': 'https://www.sec.gov/Archives/edgar/data/879764/000110465926120831/tm2628570d1_ex99-9.htm',
                                                   'note': 'CFFO less net investments; issuer-defined non-GAAP.'},
                                  'cash_dividends_paid_parent': {'value': 6372,
                                                                 'unit': 'USD million',
                                                                 'period': '9M2026',
                                                                 'source_url': 'https://www.sec.gov/Archives/edgar/data/879764/000110465926120831/tm2628570d1_ex99-9.htm'},
                                  'gearing_ex_leases': {'value': 9.1,
                                                        'unit': '%',
                                                        'period': '2026-09-30',
                                                        'source_url': 'https://www.sec.gov/Archives/edgar/data/879764/000110465926120831/tm2628570d1_ex99-9.htm'},
                                  'capital_return_policy': {'value': 40,
                                                            'unit': '% of CFFO',
                                                            'basis': 'Dividends PLUS share buybacks; not dividend-only '
                                                                     'payout ratio.',
                                                            'source_url': 'https://www.sec.gov/Archives/edgar/data/879764/000110465926120831/tm2628570d1_ex99-9.htm'}},
                      'source_urls': ['https://totalenergies.com/system/files/documents/totalenergies_universal-registration-document-2025_2026_en.pdf',
                                      'https://www.sec.gov/Archives/edgar/data/879764/000110465926120831/tm2628570d1_ex99-9.htm'],
                      'refresh_policy': 'Datiertes Emittenten-Research; bei neuer Berichtsperiode erneut prüfen. Aktuelle '
                                        'Kurse, Dividendenhistorien und Renditen werden täglich aktualisiert.'},
  'regular_dividend_reference': {'value': 3.6,
                                 'currency': 'EUR',
                                 'as_of': '2026-10-29',
                                 'kind': 'latest_interim_times_four_not_declared_fy_total',
                                 'source_url': 'https://totalenergies.com/investors/shares-and-dividends/dividends'},
  'vendor_cross_listing_check': {'symbol': 'TTE',
                                 'currency': 'USD',
                                 'financialCurrency': 'USD',
                                 'currentPrice': 85.2,
                                 'trailingEps': 10.02,
                                 'forwardEps': 12.14362,
                                 'trailingPE': 8.502994,
                                 'forwardPE': 7.01603,
                                 'bookValue': 60.135},
  'hist_div': 5.915303465869915,
  'reference_set': 'income-universe-2026',
  'tranche_policy': 'individual_only',
  'active_in_portfolios': False,
  'prepared_for': 'future_custom_portfolio_simulation',
  'logo': {'source_url': 'https://totalenergies.com/themes/custom/totalenergies_com/dist/img/favicon.png',
           'homepage': 'https://totalenergies.com/',
           'mime_type': 'image/png',
           'data_uri': 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAGySURBVHja1dNPSBQBHMXx725qXYSlf2xBNR0qUsgMOgZbFEV0kISCNFIiQiTYoYW9rWNUIB02Imohwks3yb11k0FDxKyWpMxtraEgTCiGivRgy+t3mIMIeQrCBx9+l8e7/VglufUzhVct4GqCbk1yST5tugCAYK85a1ImxvI8CHvIz8sGhCvRJcU7pE1npMYW5REERkuUTNG411qv9jZ5IyL/KyC3eJi0EqRUs61Vx08eq4anjugHgkGjlSzg6DMnvk7RNT7MvWcPN46FuYYvunJg4Q1EESRUT0aNSFspCW6/2N0Q+M3N+lh/MPzAuflXZPWUgoprhsL7yYrvObMOy6M2RtTONADDox5jg6p7fv0RIubhxQP617FS5OIpg5QmgV9MxCf6gkOTp3WjXDvnzzA0947H1TKf9Jb+vw2sV5aibABg58vuHZ3TezoLFfzR98zMVhhfLNOrEg7/JILzZp9JRNaapGkxG0zNkm5d1NlsYqYWQbvpMTeNa+5+g7Qga3K/4aJg4DvcEWRMnxkwHeYJgu3GiZb3m9RrSMqu2WUck5qCLYKj5nLUa5Ld//9nfwA5v/MkQ6im0wAAAABJRU5ErkJggg=='},
  'research_verified_at': '2026-11-21'}]
RESERVE_TICKERS = frozenset(reference['ticker'] for reference in RESERVE_REFERENCES)


def apply_classic_annual_roe(stock: dict[str, Any], previous: dict[str, Any]) -> None:
    """A disclosed annual observation fills an absent vendor ROE, not a fake TTM."""
    if stock.get('reference_set') != 'classic-2026':
        return
    metadata = stock.setdefault('field_metadata', {})
    if stock.get('field_sources', {}).get('roe') == 'live':
        metadata.pop('roe', None)
        return
    prior_meta = previous.get('field_metadata', {}).get('roe')
    prior_is_annual = (isinstance(prior_meta, dict) and prior_meta.get('reference_version') == 1
                       and 'owner_net_income_eur_million' in prior_meta.get('calculation_inputs', {}))
    if stock.get('field_sources', {}).get('roe') == 'previous' and finite_number(stock.get('roe')) is not None:
        if not prior_is_annual:
            # A later provider TTM observation outranks the older annual proxy.
            metadata.pop('roe', None)
            return
        metadata['roe'] = deepcopy(prior_meta)
    ref = stock.get('annual_roe_reference')
    if not isinstance(ref, dict) or ref.get('reference_version') != 1 or ref.get('currency') != stock.get('currency'):
        return
    inputs = ref.get('calculation_inputs', {})
    income = finite_number(inputs.get('owner_net_income_eur_million'))
    opening = finite_number(inputs.get('owner_equity_open_eur_million'))
    closing = finite_number(inputs.get('owner_equity_close_eur_million'))
    value = finite_number(ref.get('value'))
    if None in (income, opening, closing, value) or opening + closing <= 0:
        return
    if not math.isclose(value, income / ((opening + closing) / 2) * 100, abs_tol=0.000001, rel_tol=0):
        return
    if prior_is_annual and str(prior_meta.get('as_of', '')) > str(ref.get('as_of', '')):
        return
    stock['roe'] = value
    stock['field_sources']['roe'] = 'previous'
    stock['field_updated_at']['roe'] = ref['verified_at']
    metadata['roe'] = deepcopy({k:v for k,v in ref.items() if k != 'value'})
    stock['warnings'].append('ROE ist der belegte, berechnete Jahreswert FY2025 aus durchschnittlichem Eigenkapital; kein aktueller TTM-Wert.')

EXPECTED_STOCK_COUNT = sum(len(group["stocks"]) for group in PORTFOLIOS)
ETF_TICKERS = frozenset(stock["ticker"] for group in PORTFOLIOS for stock in group["stocks"] if stock.get("asset_type") == "etf")


def finite_number(value: Any) -> float | None:
    """Reject bool, NaN and infinity before any JSON serialization."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def iso_now(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(BERLIN).isoformat(timespec="seconds")


def fear_greed_score(value: Any) -> float | None:
    """CNN uses a 0–100 score, not a fraction, percentage string or Boolean."""
    score = finite_number(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return score if score is not None and 0 <= score <= 100 else None


def fear_greed_rating(score: float) -> str:
    """Display bands for comparison scores, whose endpoint has no own label."""
    # Matches CNN's own page classifier, including its boundary values.
    return "extreme fear" if score <= 25 else "fear" if score < 45 else "neutral" if score <= 55 else "greed" if score < 75 else "extreme greed"


def _fear_greed_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("CNN-Zeitstempel fehlt oder ist kein ISO-Datum")
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("CNN-Zeitstempel hat keine Zeitzone")
    return instant.astimezone(timezone.utc)


def unavailable_fear_and_greed(stamp: str | None = None, note: str | None = None) -> dict[str, Any]:
    value = dict(version=FEAR_GREED_VERSION, provider="CNN", source_url=FEAR_GREED_SOURCE,
                 source_status="unavailable", score=None, rating=None, as_of=None, retrieved_at=None,
                 last_attempted_at=stamp,
                 comparisons={key: dict(score=None, rating=None) for key in FEAR_GREED_COMPARISONS})
    if note:
        value["note"] = note
    return value


def normalize_fear_and_greed(payload: Any, retrieved_at: str | None = None) -> dict[str, Any]:
    """Accept the official headline block only; never substitute a subindex."""
    stamp = retrieved_at or iso_now()
    checked = _fear_greed_time(stamp)
    headline = payload.get("fear_and_greed") if isinstance(payload, dict) else None
    if not isinstance(headline, dict):
        raise ValueError("CNN-Antwort enthält keinen Fear-&-Greed-Gesamtindex")
    score = fear_greed_score(headline.get("score"))
    rating = " ".join(headline.get("rating", "").lower().split()) if isinstance(headline.get("rating"), str) else None
    if score is None or rating not in FEAR_GREED_RATINGS or rating != fear_greed_rating(score):
        raise ValueError("CNN-Gesamtindex oder Einstufung ist ungültig")
    as_of = _fear_greed_time(headline.get("timestamp"))
    if not -300 <= (checked - as_of).total_seconds() <= FEAR_GREED_MAX_AGE:
        raise ValueError("CNN-Gesamtindex ist älter als vier Tage oder liegt in der Zukunft")
    comparisons = {}
    missing = False
    for key, raw_key in FEAR_GREED_COMPARISONS.items():
        comparison = fear_greed_score(headline.get(raw_key))
        comparisons[key] = dict(score=comparison, rating=fear_greed_rating(comparison) if comparison is not None else None)
        missing = missing or comparison is None
    result = dict(version=FEAR_GREED_VERSION, provider="CNN", source_url=FEAR_GREED_SOURCE,
                  source_status="live", score=score, rating=rating, as_of=as_of.isoformat(timespec="seconds"),
                  retrieved_at=checked.isoformat(timespec="seconds"), last_attempted_at=checked.isoformat(timespec="seconds"),
                  comparisons=comparisons)
    if missing:
        result["note"] = "Einzelne CNN-Vergleichswerte sind nicht verfügbar."
    return result


def valid_fear_and_greed(value: Any, stamp: str | None = None) -> dict[str, Any] | None:
    """Validate persisted observations independently of today's availability."""
    if not isinstance(value, dict) or value.get("version") != FEAR_GREED_VERSION or value.get("provider") != "CNN" or value.get("source_url") != FEAR_GREED_SOURCE or value.get("source_status") not in {"live", "previous"}:
        return None
    score = fear_greed_score(value.get("score"))
    if score is None or value.get("rating") not in FEAR_GREED_RATINGS or value.get("rating") != fear_greed_rating(score):
        return None
    try:
        checked = _fear_greed_time(stamp or iso_now())
        as_of = _fear_greed_time(value.get("as_of"))
        retrieved = _fear_greed_time(value.get("retrieved_at"))
        # A valid old observation may be displayed as previous for any age;
        # its original market and retrieval times must remain unchanged.
        if (as_of - retrieved).total_seconds() > 300 or (retrieved - checked).total_seconds() > 300 or (as_of - checked).total_seconds() > 300:
            return None
    except (ValueError, TypeError, OverflowError):
        return None
    comparisons = value.get("comparisons")
    if not isinstance(comparisons, dict):
        return None
    for key in FEAR_GREED_COMPARISONS:
        item = comparisons.get(key)
        if not isinstance(item, dict):
            return None
        score = fear_greed_score(item.get("score"))
        if item.get("score") is None:
            if item.get("rating") is not None:
                return None
        elif score is None or item.get("rating") != fear_greed_rating(score):
            return None
    return deepcopy(value)


def merge_fear_and_greed(previous: Any, payload: Any = None, stamp: str | None = None,
                        error: str | None = None) -> dict[str, Any]:
    stamp = stamp or iso_now()
    prior = valid_fear_and_greed(previous, stamp)
    try:
        if error:
            raise ValueError(error)
        fresh = normalize_fear_and_greed(payload, stamp)
        if prior and _fear_greed_time(fresh["as_of"]) < _fear_greed_time(prior["as_of"]):
            raise ValueError("CNN lieferte einen älteren Indexstand als den zuletzt bestätigten")
        return fresh
    except (ValueError, TypeError, OverflowError) as exc:
        note = "CNN-Abruf nicht bestätigt: " + str(exc)[:180]
        if prior:
            prior.update(source_status="previous", last_attempted_at=stamp, note=note)
            return prior
        return unavailable_fear_and_greed(stamp, note)


def fetch_fear_and_greed(timeout: float = 10.0) -> dict[str, Any]:
    """One bounded, dependency-free request to CNN's public data endpoint."""
    if not 0 < timeout <= 12:
        raise ValueError("CNN-Zeitlimit muss größer 0 und höchstens 12 Sekunden sein")
    request = Request(FEAR_GREED_ENDPOINT, headers={
        # CNN's public chart endpoint rejects the previous custom user agent
        # with HTTP 418. Use its normal browser JSON request headers.
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://edition.cnn.com",
        "Referer": FEAR_GREED_SOURCE,
    })
    maximum = 2_000_000
    with urlopen(request, timeout=timeout) as response:
        if response.status != 200:
            raise ValueError(f"CNN antwortete mit HTTP {response.status}")
        raw = response.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("CNN-Antwort überschreitet das Größenlimit")
    result = json.loads(raw.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Ungültige CNN-JSON-Zahl: " + value)))
    if not isinstance(result, dict):
        raise ValueError("CNN-Antwort ist kein JSON-Objekt")
    return result


def refresh_fear_and_greed(previous: Any = None,
                          fetcher: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    """Fetch once, isolating CNN failures from every stock's update status."""
    try:
        return merge_fear_and_greed(previous, (fetcher or fetch_fear_and_greed)())
    except Exception as exc:
        return merge_fear_and_greed(previous, error=f"{type(exc).__name__}: {str(exc)[:160]}")


def tranche_for(portfolio_id: str, forward_pe: Any) -> tuple[str, str, str]:
    if portfolio_id in {'classic', 'universe'}:
        return 'Individuell bewerten', 'slate', 'individual'
    pe = finite_number(forward_pe)
    if pe is None or pe <= 0:
        return "Keine Bewertung verfügbar", "slate", "unavailable"
    immediate = pe < 24.0 if portfolio_id == "tech" else pe <= 11.3
    dip = pe <= (29.0 if portfolio_id == "tech" else 13.0)
    if immediate:
        return "Tranche 1 (Sofortkauf)", "emerald", "buy"
    if dip:
        return "Tranche 2 (Rücksetzer kaufen)", "sky", "dip"
    return "Hold / Watchlist", "amber", "watch"


def add_derived(stock: dict[str, Any], portfolio_id: str) -> None:
    raw = finite_number(stock.get("current_pe"))
    stock["chart_pe"] = 260.0 if raw is not None and stock["ticker"] in CHART_CAPPED_TICKERS and (raw < 0 or raw > 280) else raw
    hist_pe = finite_number(stock.get("hist_pe"))
    comparable = stock.get("field_metadata", {}).get("hist_pe", {}).get("comparable_to_current") is not False
    spread = finite_number((raw / hist_pe - 1) * 100) if comparable and raw is not None and raw > 0 and hist_pe is not None and hist_pe > 0 else None
    stock["spread_pe_pct"] = round(spread, 4) if spread is not None else None
    add_buy_indicator(stock)
    price, sma = stock.get("price"), stock.get("sma200")
    distance = finite_number((price / sma - 1) * 100) if price and sma else None
    stock["sma200_distance_pct"] = round(distance, 4) if distance is not None else None
    if stock.get("asset_type") == "etf":
        stock["tranche"], stock["tranche_color"], stock["tranche_key"] = "ETF · separat bewerten", "slate", "unavailable"
        return
    stock["tranche"], stock["tranche_color"], stock["tranche_key"] = tranche_for(portfolio_id, stock.get("forward_pe"))
    if stock.get("ticker") == TOKIO_TICKER and (
            stock.get("field_sources", {}).get("forward_pe") != "live" or
            stock.get("field_metadata", {}).get("forward_pe", {}).get("validation_version") != TOKIO_METRIC_VERSION):
        stock["tranche"], stock["tranche_color"], stock["tranche_key"] = "Keine aktuelle IFRS-Prognose bestätigt", "slate", "unavailable"
    if raw is not None and raw <= 0:
        stock["warnings"].append("TTM-KGV ist nicht positiv; ein Bewertungsabschlag ist nicht sinnvoll interpretierbar.")
    if raw is not None and stock["chart_pe"] != raw:
        stock["warnings"].append("Nur der TTM-Chartbalken ist auf 260x begrenzt; Kachel und Tooltip zeigen den Rohwert.")
    if stock["tranche_key"] == "unavailable":
        stock["warnings"].append("Keine frisch verifizierte IFRS-Basic-EPS-Prognose; keine Tranche ableitbar." if stock.get("ticker") == TOKIO_TICKER else "Forward-KGV fehlt oder ist nicht positiv; keine Tranche ableitbar.")


def build_seed_data(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    stamp = iso_now(now)
    result = dict(schema_version=1,
                  last_updated=now.astimezone(BERLIN).strftime("%d.%m.%Y, %H:%M %Z"),
                  updated_at=stamp, last_attempted_at=None, data_mode="initial",
                  expected_stock_count=EXPECTED_STOCK_COUNT,
                  warnings=[INITIAL_WARNING, REFERENCE_WARNING], portfolios=[],
                  fear_and_greed=unavailable_fear_and_greed())
    for portfolio in PORTFOLIOS:
        group = dict(id=portfolio["id"], name=portfolio["name"], cluster_order=list(portfolio["cluster_order"]), stocks=[])
        for key in ('valuation_style', 'tranche_policy'):
            if key in portfolio:
                group[key] = portfolio[key]
        for ref in portfolio["stocks"]:
            stock = deepcopy({key: value for key, value in ref.items() if key != "initial"})
            stock.update({field: ref["initial"].get(field) for field in NUMERIC_FIELDS})
            stock.update(currency=None, source_status="initial", as_of=None,
                         field_sources={field: "initial" if stock[field] is not None else "unavailable" for field in NUMERIC_FIELDS},
                         field_updated_at={field: None for field in NUMERIC_FIELDS}, warnings=[INITIAL_WARNING])
            stock["field_sources"]["currency"] = "unavailable"
            stock["field_updated_at"]["currency"] = None
            apply_us_historical_reference(stock)
            apply_tsm_historical_reference(stock)
            if stock["ticker"] == TOKIO_TICKER:
                # Verified year-end references retain their own explicit period
                # and basis; the J-GAAP KGV series is not comparable to IFRS.
                stock["field_metadata"] = {
                    "hist_pe": tokio_historical_pe_metadata(),
                    "hist_div": dict(provider="Tokio Marine IR", label="Dividendenreferenz 5J",
                                     period="FY2021–FY2025", verified_at="2026-10-22",
                                     source_url=TOKIO_HIST_DIV_SOURCE,
                                     method="Arithmetisches Mittel der Jahresendrenditen FY2021–FY2025: DPS [85, 100, 123, 172, 218] / Jahresendkurse [2376, 2547, 4703, 5736, 7308] × 100 (einheitliche Aktienbasis); gedruckte Seite 74.")}
            if stock.get("asset_type") == "etf":
                for field in ETF_NUMERIC_FIELDS:
                    stock[field] = None
                    stock["field_sources"][field] = "unavailable"
                    stock["field_updated_at"][field] = None
                stock["distribution_as_of"] = None
            add_derived(stock, portfolio["id"])
            group["stocks"].append(stock)
        result["portfolios"].append(group)
    reserve_stocks = []
    for ref in RESERVE_REFERENCES:
        stock = deepcopy({key: value for key, value in ref.items() if key != "initial"})
        stock.update({field: None for field in NUMERIC_FIELDS})
        stock.update(currency=None, source_status="initial", as_of=None,
                     field_sources={field: "unavailable" for field in (*NUMERIC_FIELDS, "currency")},
                     field_updated_at={field: None for field in (*NUMERIC_FIELDS, "currency")},
                     warnings=[INITIAL_WARNING])
        add_derived(stock, 'universe')
        reserve_stocks.append(stock)
    result["security_universe"] = dict(
        schema_version=1, name="Income research",
        purpose="Prepared securities for future portfolio simulations",
        stocks=reserve_stocks, expected_stock_count=len(RESERVE_TICKERS),
        updated_at=stamp, last_attempted_at=None, data_mode="initial",
        status_counts=dict(Counter(stock["source_status"] for stock in reserve_stocks)))
    return result


def tokio_fiscal_year(stamp: str) -> int:
    """Tokio Marine's Japanese fiscal year runs from April through March."""
    current = datetime.fromisoformat(stamp)
    if current.tzinfo is None:
        raise ValueError("Dividenden-Zeitpunkt hat keine Zeitzone")
    current = current.astimezone(TOKYO)
    return current.year if current.month >= 4 else current.year - 1


def _read_tokio_source(url: str, maximum: int) -> bytes:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "www.tokiomarinehd.com" or not parsed.path.startswith("/en/ir/"):
        raise ValueError("Nicht zugelassene Tokio-Marine-IR-Quelle")
    with urlopen(Request(url, headers={"User-Agent": "ResearchStudio/1.0"}), timeout=8) as response:
        body = response.read(maximum + 1)
    if len(body) > maximum:
        raise ValueError("IR-Quelldatei ist unerwartet groß")
    return body


class _TokioReportLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: set[str] = set()

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self.links.add(urljoin("https://www.tokiomarinehd.com/", value))


def discover_tokio_reports(html: str, fiscal_year: int) -> list[tuple[int, str]]:
    """Only summary reports, never JGAAP duplicates or supplemental files."""
    parser = _TokioReportLinks()
    parser.feed(html)
    reports = []
    for url in parser.links:
        parsed = urlparse(url)
        match = re.fullmatch(r"(IFRS_)?([1-4])Q_FY(20\d{2})_Summary_Report_e\.pdf", parsed.path.rsplit("/", 1)[-1], flags=re.I)
        if (parsed.scheme != "https" or parsed.netloc != "www.tokiomarinehd.com" or
                not parsed.path.startswith("/en/ir/") or not match or int(match[3]) != fiscal_year):
            continue
        # IFRS was introduced for FY2025. Its explicit IFRS report must take
        # precedence over the separate FY2025 Japanese-GAAP summary report.
        if fiscal_year <= 2025 and not match[1]:
            continue
        reports.append((int(match[2]), url))
    return sorted(reports)


_TOKIO_NUMBER = r"-?\d[\d,]*(?:\.\d+)?"
_TOKIO_MONTH = r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"


def parse_tokio_report(text: str, source_url: str, fiscal_year: int, retrieved_at: str,
                       *, annual: bool, forecast_year: int | None = None) -> dict[str, dict[str, Any]]:
    """Extract reported IFRS basic EPS/ROE, with an explicit share-count proof.

    No Yahoo EPS, adjusted EPS or guessed split multiplier is accepted. A changed
    report layout or an unknown issued-share basis therefore fails closed.
    """
    text = text.replace("\u2212", "-").replace("\u00a0", " ")
    compact = re.sub(r"\s+", " ", text)
    if not re.search(r"Summary of Consolidated Business Results of Tokio Marine Holdings, Inc\.\s+under IFRS", compact):
        raise ValueError("Summary Report ist kein bestätigter IFRS-Konzernbericht")
    if not re.search(r"Securit(?:y|ies) Code Number:\s*8766\b", compact):
        raise ValueError("Wertpapierkennung des IR-Berichts ist nicht eindeutig")
    dates = re.findall(rf"(?m)^({_TOKIO_MONTH} \d{{1,2}}, 20\d{{2}})\s*$", text)
    if len(set(dates)) != 1:
        raise ValueError("Veröffentlichungsdatum des IR-Berichts ist nicht eindeutig")
    report_date = datetime.strptime(dates[0], "%B %d, %Y").date().isoformat()
    issued = re.findall(rf"As of {_TOKIO_MONTH} \d{{1,2}}, 20\d{{2}}\s+([\d,]+) shares", compact)
    if not issued or "Number of shares issued (common stock)" not in compact:
        raise ValueError("Nachweis der Aktienbasis fehlt im IR-Bericht")
    issued_shares = int(issued[0].replace(",", ""))
    if issued_shares == TOKIO_PRE_SPLIT_ISSUED:
        factor = TOKIO_SPLIT_FACTOR
    elif issued_shares == TOKIO_POST_SPLIT_ISSUED:
        factor = 1
    else:
        raise ValueError(f"Unbekannte Aktienbasis im IR-Bericht: {issued_shares}")
    if factor == TOKIO_SPLIT_FACTOR and (
            report_date >= "2026-10-01" or re.search(r"(?:stock|share)\s+split", compact, flags=re.I)):
        # IAS 33 may restate EPS for a split after a quarter-end while the
        # balance-date issued-share count still shows the old basis. A post-
        # split Q2 publication therefore cannot safely be divided by 15 merely
        # because its September share-count table is pre-split.
        raise ValueError("EPS-Aktienbasis nach Split nicht eindeutig belegt; keine automatische weitere Division")
    common = dict(provider="Tokio Marine IR", validation_version=TOKIO_METRIC_VERSION,
                  source_url=source_url, retrieved_at=retrieved_at, report_date=report_date,
                  report_fiscal_year=fiscal_year, currency="JPY", issued_shares=issued_shares,
                  split_factor=factor)
    metrics: dict[str, dict[str, Any]] = {}
    if annual:
        if not re.search(rf"under IFRS for the fiscal year ended March 31, {fiscal_year + 1}\b", compact):
            raise ValueError("Geschäftsjahr des jährlichen IFRS-Berichts stimmt nicht überein")
        table = re.search(r"Earnings per share\s*-\s*Basic(?P<table>[\s\S]{0,900}?)Total assets", text)
        if not table or not re.search(r"Ratio of net income to\s*equity attributable to\s*owners of the parent", table["table"]):
            raise ValueError("IFRS-Tabelle für Basic EPS und berichtete ROE fehlt")
        rows = re.findall(rf"(?m)^Fiscal year {fiscal_year}\s+({_TOKIO_NUMBER})\s+({_TOKIO_NUMBER})\s+({_TOKIO_NUMBER})\s+({_TOKIO_NUMBER})\s*$", table["table"])
        if len(rows) != 1:
            raise ValueError("Jährliche IFRS-EPS-/ROE-Zeile ist nicht eindeutig")
        raw_eps, _, roe, _ = [float(value.replace(",", "")) for value in rows[0]]
        if raw_eps <= 0 or not -1000 <= roe <= 1000:
            raise ValueError("Jährliche IFRS-Werte sind nicht sinnvoll interpretierbar")
        period_end = f"{fiscal_year + 1}-03-31"
        metrics["current_pe"] = dict(common, fact_type="ifrs_basic_eps_actual", fiscal_year=fiscal_year,
                                     label=f"KGV FY{fiscal_year}", period=f"FY{fiscal_year} · IFRS", as_of=period_end,
                                     period_end=period_end, raw_eps=raw_eps, normalized_eps=raw_eps / factor,
                                     method=f"Aktueller JPY-Kurs / berichtetes IFRS Basic EPS FY{fiscal_year}; EPS anhand der ausgewiesenen Aktienzahl auf die aktuelle Aktienbasis normiert (÷ {factor}). Kein TTM- oder Adjusted-EPS-KGV.")
        metrics["roe"] = dict(common, fact_type="ifrs_reported_roe", fiscal_year=fiscal_year,
                              label=f"ROE FY{fiscal_year}", period=f"FY{fiscal_year} · IFRS", as_of=period_end,
                              period_end=period_end, value=roe,
                              method="Berichtete Ratio of net income to equity attributable to owners of the parent; keine Adjusted-ROE-Prognose.")
    if forecast_year is not None:
        if not re.search(rf"3\. Consolidated Business Forecasts for the fiscal year {forecast_year}\s*\(April 1, {forecast_year} to March 31, {forecast_year + 1}\)", compact):
            raise ValueError("Keine eindeutig aktuelle ganzjährige IFRS-Ergebnisprognose")
        candidates = []
        for match in re.finditer(rf"(?m)^Fiscal year {forecast_year}\s+({_TOKIO_NUMBER})\s+({_TOKIO_NUMBER})\s+({_TOKIO_NUMBER})(?:\s+{_TOKIO_NUMBER})*\s*$", text):
            heading = text[max(0, match.start() - 450):match.start()]
            if "Earnings per share" in heading and "Basic" in heading and "Net income" in heading:
                candidates.append(float(match[3].replace(",", "")))
        if len(candidates) != 1 or candidates[0] <= 0:
            raise ValueError("Basic-EPS-Prognose ist nicht eindeutig; Adjusted EPS wird nicht verwendet")
        raw_eps = candidates[0]
        metrics["forward_pe"] = dict(common, fact_type="ifrs_basic_eps_forecast", fiscal_year=forecast_year,
                                     label=f"Forward FY{forecast_year}", period=f"FY{forecast_year} · IFRS · Prognose",
                                     as_of=report_date, period_end=f"{forecast_year + 1}-03-31",
                                     raw_eps=raw_eps, normalized_eps=raw_eps / factor,
                                     method=f"Aktueller JPY-Kurs / offizielle IFRS Basic-EPS-Prognose FY{forecast_year}; Aktienbasis anhand der ausgewiesenen Aktienzahl geprüft (EPS ÷ {factor}). Kein Analystenkonsens oder Adjusted EPS.")
    return metrics


def fetch_tokio_issuer_metrics() -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Discover the latest annual and current-year forecast on issuer indices."""
    from pypdf import PdfReader
    stamp = iso_now()
    year = tokio_fiscal_year(stamp)
    metrics: dict[str, dict[str, Any]] = {}
    warnings = []
    discovered: dict[int, list[tuple[int, str]]] = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        indices = {fy: pool.submit(_read_tokio_source, TOKIO_REPORT_INDEX.format(year=fy), 1_000_000) for fy in (year, year - 1)}
        for fy, future in indices.items():
            try:
                html = future.result().decode("utf-8")
                if not re.search(rf"<title>\s*FY{fy}\s*\|", html, flags=re.I) or "Summary report" not in html:
                    raise ValueError("IR-Index ist keine bestätigte Geschäftsjahres-Berichtsseite")
                discovered[fy] = discover_tokio_reports(html, fy)
            except Exception as exc:
                warnings.append(f"IR-Berichtsindex FY{fy}: {type(exc).__name__}: {str(exc)[:150]}")
        annual_candidates = [url for quarter, url in discovered.get(year - 1, []) if quarter == 4]
        latest = discovered.get(year, [])
        latest_quarter = max((quarter for quarter, _ in latest), default=0)
        forecast_candidates = [url for quarter, url in latest if quarter == latest_quarter]
        if (not latest and year in discovered and
                datetime.fromisoformat(stamp).astimezone(TOKYO).date().isoformat() < f"{year}-08-01"):
            # Before the first quarterly report, the latest annual report carries
            # the forecast for the current fiscal year. An unavailable/truncated
            # current index, or an unexpectedly empty one after Q1 season, must
            # never relabel an older annual forecast as freshly confirmed.
            forecast_candidates = annual_candidates
        jobs = {}
        for label, urls in (("annual", annual_candidates), ("forecast", forecast_candidates)):
            if len(urls) != 1:
                warnings.append(f"IR-{label}: kein eindeutiger neuester IFRS Summary Report.")
                continue
            url = urls[0]
            if url not in jobs:
                jobs[url] = pool.submit(_read_tokio_source, url, 6_000_000)
        for label, urls in (("annual", annual_candidates), ("forecast", forecast_candidates)):
            if len(urls) != 1:
                continue
            url = urls[0]
            try:
                reader = PdfReader(BytesIO(jobs[url].result()))
                if not 2 <= len(reader.pages) <= 200:
                    raise ValueError("Unerwarteter Umfang des IFRS-Berichts")
                # Normal extraction follows Form XObjects in these issuer PDFs;
                # pypdf's layout mode can silently return empty text here.
                text = "\n".join(page.extract_text() or "" for page in reader.pages[:2])
                report_year = int(re.search(r"FY(20\d{2})_Summary_Report_e\.pdf$", url)[1])
                parsed = parse_tokio_report(text, url, report_year, stamp, annual=label == "annual",
                                            forecast_year=year if label == "forecast" else None)
                metrics.update(parsed)
            except Exception as exc:
                warnings.append(f"IR-{label}: {type(exc).__name__}: {str(exc)[:180]}")
    return metrics, warnings


def valid_tokio_metric(value: Any, field: str, stamp: str) -> dict[str, Any] | None:
    """Cached ratios from the former Yahoo/seed path are never eligible."""
    if not isinstance(value, dict) or value.get("provider") != "Tokio Marine IR" or value.get("validation_version") != TOKIO_METRIC_VERSION:
        return None
    expected = {"current_pe": "ifrs_basic_eps_actual", "forward_pe": "ifrs_basic_eps_forecast", "roe": "ifrs_reported_roe"}
    if value.get("fact_type") != expected.get(field) or value.get("currency") != "JPY":
        return None
    if not isinstance(value.get("source_url"), str):
        return None
    source = urlparse(value["source_url"])
    if source.scheme != "https" or source.netloc != "www.tokiomarinehd.com" or not source.path.startswith("/en/ir/") or not re.search(r"[1-4]Q_FY20\d{2}_Summary_Report_e\.pdf$", source.path):
        return None
    fy = value.get("fiscal_year")
    current_fy = tokio_fiscal_year(stamp)
    if type(fy) is not int or (field == "forward_pe" and fy != current_fy) or (field != "forward_pe" and not current_fy - 2 <= fy < current_fy):
        return None
    try:
        retrieved = datetime.fromisoformat(value.get("retrieved_at", ""))
        report_date = datetime.strptime(value.get("report_date", ""), "%Y-%m-%d").date()
        if retrieved.tzinfo is None or report_date > retrieved.astimezone(TOKYO).date() or value.get("period_end") != f"{fy + 1}-03-31":
            return None
    except (ValueError, TypeError):
        return None
    factor = value.get("split_factor")
    issued = value.get("issued_shares")
    if type(issued) is not int or type(factor) is not int:
        return None
    expected_factor = {TOKIO_PRE_SPLIT_ISSUED: TOKIO_SPLIT_FACTOR, TOKIO_POST_SPLIT_ISSUED: 1}.get(issued)
    if expected_factor is None or factor != expected_factor:
        return None
    if factor == TOKIO_SPLIT_FACTOR and report_date.isoformat() >= "2026-10-01":
        return None
    if field == "roe":
        roe = finite_number(value.get("value"))
        if roe is None or not -1000 <= roe <= 1000:
            return None
    else:
        raw, normalized = finite_number(value.get("raw_eps")), finite_number(value.get("normalized_eps"))
        if raw is None or raw <= 0 or normalized is None or normalized <= 0 or not math.isclose(normalized, raw / factor, rel_tol=0, abs_tol=1e-9):
            return None
    return dict(value)


def apply_tokio_issuer_metrics(stock: dict[str, Any], previous: dict[str, Any], payload: dict[str, Any],
                               price: float | None, currency: str | None, stamp: str) -> None:
    fresh_facts = payload.get("issuer_metrics") or {}
    prior_facts = previous.get("field_metadata") or {}
    metadata = stock.setdefault("field_metadata", {})
    # Reapply the audited reference rather than copying former null/initial
    # values or allowing a provider payload to replace this separate series.
    stock["hist_pe"] = TOKIO_HIST_PE_MEAN
    metadata["hist_pe"] = tokio_historical_pe_metadata()
    stock["ps_ratio"] = None
    stock["field_sources"]["ps_ratio"] = "unavailable"
    stock["field_updated_at"]["ps_ratio"] = None
    metadata["ps_ratio"] = {"unavailable_reason": "Yahoo-Aktienzahl/KUV nach Split inkonsistent; keine geprüfte IFRS-KUV-Basis"}
    for field in ("current_pe", "forward_pe", "roe"):
        stock[field] = None
        stock["field_sources"][field] = "unavailable"
        stock["field_updated_at"][field] = None
        fresh = valid_tokio_metric(fresh_facts.get(field), field, stamp)
        prior = valid_tokio_metric(prior_facts.get(field), field, stamp)
        fact = fresh or prior
        if not fact:
            metadata[field] = {"unavailable_reason": "Keine eindeutig verifizierte aktuelle IFRS-Emittentenbasis; Yahoo- und Initialwerte verworfen"}
            continue
        metadata[field] = dict(fact)
        source = "live" if fresh else "previous"
        if field == "roe":
            result = fact["value"]
        elif price is not None and price > 0 and currency == "JPY":
            result = price / fact["normalized_eps"]
            metadata[field].update(calculation_price=price,
                                   price_as_of=payload.get("info_retrieved_at") or payload.get("history_retrieved_at") or stamp)
        else:
            old = finite_number(previous.get(field))
            old_price = finite_number(prior.get("calculation_price")) if prior else None
            if not (prior and old is not None and old_price and old_price > 0 and
                    previous.get("field_sources", {}).get(field) in {"live", "previous"} and
                    math.isclose(old, old_price / prior["normalized_eps"], rel_tol=0, abs_tol=0.000001)):
                continue
            result = old
            metadata[field] = dict(prior)
            source = "previous"
        stock[field] = round(result, 6)
        stock["field_sources"][field] = source
        stock["field_updated_at"][field] = fact["retrieved_at"] if source == "live" else metadata[field]["retrieved_at"]
    if any(stock["field_sources"][field] == "previous" for field in ("current_pe", "forward_pe", "roe")):
        stock["warnings"].append("IFRS-Kennzahlen verwenden zuletzt verifizierte Emittentenberichte; neuester Abruf nicht vollständig bestätigt.")


class _DividendTableParser(HTMLParser):
    """Read actual table cells, without relying on the issuer's CSS classes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.depth = 0
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "table":
            if not self.depth:
                self.rows = []
            self.depth += 1
        elif self.depth == 1:
            if tag == "tr":
                self.row = []
            elif tag in {"td", "th"} and self.row is not None:
                self.cell = []

    def handle_data(self, data: str) -> None:
        if self.depth == 1 and self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.depth == 1:
            if tag in {"td", "th"} and self.cell is not None and self.row is not None:
                self.row.append(re.sub(r"\s+", "", "".join(self.cell)))
                self.cell = None
            elif tag == "tr" and self.row is not None:
                self.rows.append(self.row)
                self.row = None
            elif tag == "table":
                self.tables.append(self.rows)
        if tag == "table" and self.depth:
            self.depth -= 1


def parse_tokio_dividend_forecast(html: str, retrieved_at: str) -> dict[str, Any]:
    """Use the current, explicitly forecast JPY annual dividend from issuer IR."""
    parser = _DividendTableParser()
    parser.feed(html)
    expected_year = tokio_fiscal_year(retrieved_at)
    forecasts = []
    for table in parser.tables:
        heading = "".join(cell for row in table[:2] for cell in row)
        if "配当金" not in heading or "円/株" not in heading or "年間" not in heading:
            continue
        for row in table:
            match = re.fullmatch(r"(20\d{2})[（(]予想[）)]", row[0]) if row else None
            if not match or int(match.group(1)) != expected_year or len(row) < 4:
                continue
            interim, year_end, annual = (finite_number(value.replace(",", "")) for value in row[1:4])
            if (interim is None or year_end is None or annual is None or
                    interim < 0 or year_end < 0 or annual <= 0 or
                    not math.isclose(interim + year_end, annual, rel_tol=0, abs_tol=0.011)):
                raise ValueError("IR-Dividendenprognose enthält widersprüchliche Jahresbeträge")
            forecasts.append(dict(annual_rate=annual, fiscal_year=expected_year, currency="JPY",
                                  source_url=TOKIO_DIVIDEND_SOURCE, retrieved_at=retrieved_at,
                                  basis="forecast", interim_rate=interim, year_end_rate=year_end))
    if len(forecasts) != 1:
        raise ValueError("Keine eindeutige aktuelle JPY-Dividendenprognose in der Emittenten-Tabelle")
    return forecasts[0]


def fetch_tokio_dividend_forecast() -> dict[str, Any]:
    # A Yahoo split mismatch must not be silently reintroduced by the next run.
    # The issuer table is fetched afresh; no dividend amount is hardcoded here.
    request = Request(TOKIO_DIVIDEND_SOURCE, headers={"User-Agent": "ResearchStudio/1.0"})
    with urlopen(request, timeout=8) as response:
        html = response.read(1_000_001)
        if len(html) > 1_000_000:
            raise ValueError("IR-Dividendenseite ist unerwartet groß")
        html = html.decode("utf-8")
    return parse_tokio_dividend_forecast(html, iso_now())


def valid_tokio_dividend_forecast(value: Any, stamp: str) -> dict[str, Any] | None:
    """Validate both freshly fetched and previously verified issuer metadata."""
    if not isinstance(value, dict):
        return None
    annual = finite_number(value.get("annual_rate"))
    if (annual is None or annual <= 0 or value.get("currency") != "JPY" or
            value.get("basis") != "forecast" or value.get("source_url") != TOKIO_DIVIDEND_SOURCE or
            type(value.get("fiscal_year")) is not int or value["fiscal_year"] != tokio_fiscal_year(stamp)):
        return None
    try:
        retrieved_at = datetime.fromisoformat(value.get("retrieved_at", ""))
        if retrieved_at.tzinfo is None:
            return None
    except (TypeError, ValueError):
        return None
    result = dict(value)
    result["annual_rate"] = annual
    return result


def apply_tokio_dividend(stock: dict[str, Any], previous: dict[str, Any], payload: dict[str, Any],
                         price: float | None, currency: str | None, stamp: str) -> None:
    """Use issuer forecasts only; Yahoo's split-corrupted yield is ineligible."""
    fresh = valid_tokio_dividend_forecast(payload.get("dividend_forecast"), stamp)
    prior = valid_tokio_dividend_forecast(previous.get("dividend_forecast"), stamp)
    forecast = fresh or prior
    stock["div_yield"] = None
    stock["field_sources"]["div_yield"] = "unavailable"
    stock["field_updated_at"]["div_yield"] = None
    if forecast:
        stock["dividend_forecast"] = dict(forecast)
    else:
        stock.pop("dividend_forecast", None)
    if forecast and price is not None and price > 0 and currency == "JPY":
        stock["div_yield"] = round(forecast["annual_rate"] / price * 100, 6)
        stock["field_sources"]["div_yield"] = "live" if fresh else "previous"
        stock["field_updated_at"]["div_yield"] = forecast["retrieved_at"]
        info = payload.get("info") or {}
        quote_available = any((finite_number(info.get(field)) or 0) > 0 for field in ("currentPrice", "regularMarketPrice"))
        price_stamp = payload.get("info_retrieved_at" if quote_available else "history_retrieved_at") or stamp
        stock["dividend_forecast"].update(calculation_price=price, price_as_of=price_stamp)
        if not fresh:
            stock["warnings"].append("Dividendenrendite aus letzter verifizierter IR-Prognose und aktuellem JPY-Kurs; Prognose nicht neu abgerufen.")
        return
    # A missing/unverified current JPY quote may retain a verified earlier yield,
    # but never the old Yahoo value or a yield from an expired fiscal forecast.
    old_yield = finite_number(previous.get("div_yield"))
    old_price = finite_number(prior.get("calculation_price")) if prior else None
    if (prior and old_yield is not None and old_yield >= 0 and old_price and old_price > 0 and
            previous.get("field_sources", {}).get("div_yield") in {"live", "previous"} and
            math.isclose(old_yield, prior["annual_rate"] / old_price * 100, rel_tol=0, abs_tol=0.000001)):
        stock["div_yield"] = old_yield
        stock["field_sources"]["div_yield"] = "previous"
        stock["field_updated_at"]["div_yield"] = prior["retrieved_at"]
        stock["dividend_forecast"] = dict(prior)
        stock["warnings"].append("Kein aktueller bestätigter JPY-Kurs; letzte verifizierte IR-Dividendenrendite beibehalten.")
    else:
        stock["warnings"].append("Keine verifizierte aktuelle IR-Dividendenprognose mit passendem JPY-Kurs; Yahoo-Dividendenfeld wegen Split-Unstimmigkeit nicht übernommen.")


def normalize_dividend_yield(info: dict[str, Any], price: float | None) -> tuple[float | None, list[str]]:
    """Resolve Yahoo's fraction/percent variants against independent fields.

    dividendRate / price and trailingAnnualDividendYield (Yahoo fraction)
    provide unit checks. With contradictory or absent checks, do not guess.
    A missing dividendYield may be reconstructed from a valid annual rate.
    """
    raw = finite_number(info.get("dividendYield"))
    annual_rate = finite_number(info.get("dividendRate"))
    trailing = finite_number(info.get("trailingAnnualDividendYield"))
    annual = finite_number(annual_rate / price * 100) if annual_rate is not None and annual_rate >= 0 and price and price > 0 else None
    trailing_pct = finite_number(trailing * 100) if trailing is not None and trailing >= 0 else None
    anchors = [value for value in (annual, trailing_pct) if value is not None]
    if raw is None:
        if annual is not None:
            return annual, ["Dividendenrendite aus dividendRate / aktuellem Kurs berechnet."]
        if trailing_pct is not None:
            return trailing_pct, ["Nur die nachlaufende Jahresdividendenrendite ist verfügbar."]
        return None, []
    if raw < 0:
        return None, ["Negative Dividendenrendite verworfen."]
    if raw == 0:
        if any(value > 0.000001 for value in anchors):
            return None, ["Dividendenfelder widersprechen sich (Nullwert trotz positiver Referenz)."]
        return 0.0, []
    if not anchors:
        return None, ["Einheit von dividendYield ohne dividendRate/Kurs oder nachlaufende Rendite nicht sicher bestimmbar."]

    def matches(candidate: float, anchor: float) -> bool:
        return math.isfinite(candidate) and abs(candidate - anchor) <= max(0.000001, abs(anchor) * 0.25)

    candidates = [raw, raw * 100]
    matches_by_anchor = [{index for index, candidate in enumerate(candidates) if matches(candidate, anchor)} for anchor in anchors if anchor > 0]
    all_matches = set().union(*matches_by_anchor) if matches_by_anchor else set()
    if len(all_matches) == 1:
        chosen = all_matches.pop()
        warnings = []
        if any(not match for match in matches_by_anchor):
            warnings.append("Aktuelle und nachlaufende Dividendenreferenz weichen ab; Einheit über die passende Referenz geprüft.")
        return candidates[chosen], warnings
    return None, ["Dividenden-Einheit oder Referenzfelder sind widersprüchlich; bisherigen Wert als gekennzeichneten Fallback behalten."]


def wilder_rsi(close: Any, period: int = 14) -> float | None:
    """Wilder's RSI with the arithmetic first 14 changes and recursive RMA."""
    import pandas as pd
    values = pd.to_numeric(pd.Series(close), errors="coerce").replace([float("inf"), -float("inf")], float("nan")).dropna()
    if len(values) <= period:
        return None
    changes = values.diff().iloc[1:]
    gains, losses = changes.clip(lower=0), -changes.clip(upper=0)
    # Seed explicitly: pandas ewm's default first observation is not Wilder's seed.
    gain_seed = pd.Series([float(gains.iloc[:period].mean()), *gains.iloc[period:].tolist()])
    loss_seed = pd.Series([float(losses.iloc[:period].mean()), *losses.iloc[period:].tolist()])
    avg_gain = float(gain_seed.ewm(alpha=1 / period, adjust=False).mean().iloc[-1])
    avg_loss = float(loss_seed.ewm(alpha=1 / period, adjust=False).mean().iloc[-1])
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    if avg_gain == 0:
        return 0.0
    return finite_number(100 - 100 / (1 + avg_gain / avg_loss))


def calculate_technicals(history: Any) -> tuple[dict[str, Any], list[str]]:
    import pandas as pd
    if history is None or history.empty or "Close" not in history:
        raise ValueError("Keine gültigen historischen Tageskurse verfügbar")
    history = history.sort_index()
    history = history.loc[~history.index.duplicated(keep="last")]
    close = pd.to_numeric(history["Close"], errors="coerce").replace([float("inf"), -float("inf")], float("nan")).dropna()
    close = close[close > 0]
    if close.empty:
        raise ValueError("Historische Schlusskurse fehlen")
    last_index = close.index[-1]
    as_of = last_index.isoformat() if hasattr(last_index, "isoformat") else str(last_index)
    sma = finite_number(close.rolling(window=200, min_periods=200).mean().iloc[-1])
    rsi = wilder_rsi(close)
    notes = []
    if sma is None:
        notes.append(f"SMA 200 nicht berechenbar: nur {len(close)} gültige Tageskurse.")
    if rsi is None:
        notes.append("RSI 14 nicht berechenbar: weniger als 15 gültige Tageskurse.")
    return dict(sma200=sma, rsi14=rsi, history_price=float(close.iloc[-1]), history_as_of=as_of), notes


def calculate_distributions(history: Any) -> tuple[dict[str, Any], list[str]]:
    """Sum actual cash distributions over one complete rolling calendar year.

    Yahoo calls ETF distributions "Dividends". They may include option income,
    so the dashboard deliberately does not call this corporate dividend yield.
    The open left boundary prevents counting 13 monthly payments when the same
    calendar date occurs at both ends of the annual history.
    """
    import pandas as pd
    if history is None or history.empty or "Dividends" not in history:
        return {}, ["ETF-Ausschüttungshistorie fehlt; keine 12M-Rendite berechnet."]
    history = history.sort_index()
    history = history.loc[~history.index.duplicated(keep="last")]
    if not isinstance(history.index, pd.DatetimeIndex):
        return {}, ["ETF-Ausschüttungshistorie hat keine gültigen Tagesdaten."]
    end = history.index[-1].normalize()
    cutoff = end - pd.DateOffset(years=1)
    # Trading-day sampling can begin a few days after the exact calendar cutoff.
    if history.index[0].normalize() > cutoff + pd.Timedelta(days=5):
        return {}, ["ETF-Ausschüttungshistorie deckt keine vollständigen 12 Monate ab."]
    in_window = (history.index.normalize() > cutoff) & (history.index.normalize() <= end)
    distributions = pd.to_numeric(history.loc[in_window, "Dividends"], errors="coerce")
    if distributions.empty or not distributions.map(lambda value: finite_number(value) is not None and value >= 0).all():
        return {}, ["ETF-Ausschüttungshistorie enthält ungültige Werte; keine 12M-Rendite berechnet."]
    total = finite_number(distributions.sum())
    return dict(distributions_per_share_ttm=total, distribution_as_of=end.date().isoformat()), []


def unavailable_period_returns(ticker: str, reason: str) -> dict[str, Any]:
    return dict(
        version=RETURN_VERSION, basis="total_return", source_status="unavailable",
        as_of=None, retrieved_at=None, since=None,
        source_url=f"https://finance.yahoo.com/quote/{quote(ticker, safe='')}/history/",
        method=RETURN_METHOD,
        periods={key: dict(value=None, start=None, end=None,
                           annualized=key in {"3y", "5y", "10y", "all"}, reason=reason)
                 for key in RETURN_PERIODS})


def parse_jepq_return_cash(payload: Any, retrieved_at: str | None = None) -> dict[str, Any]:
    """Validate the issuer's published cash events for the fixed JEPQ CUSIP."""
    stamp = retrieved_at or iso_now()
    retrieved = datetime.fromisoformat(stamp)
    if retrieved.tzinfo is None or not isinstance(payload, dict) or payload.get("fundTypeCode") != "N_ETF":
        raise ValueError("JEPQ-Emittentenantwort ist nicht eindeutig als ETF bestätigt")
    dividends = payload.get("dividendsDistributionHistoryList")
    capital_gains = payload.get("capitalGainsDistributionHistoryList")
    if not isinstance(dividends, list) or len(dividends) < 2 or not isinstance(capital_gains, list):
        raise ValueError("JEPQ-Ausschüttungshistorie fehlt beim Emittenten")
    events = {}
    for item in dividends:
        if not isinstance(item, dict):
            raise ValueError("Ungültiger JEPQ-Ausschüttungsdatensatz")
        try:
            ex_date = datetime.strptime(item.get("exDate", ""), "%Y-%m-%d").date()
            pay_date = datetime.strptime(item.get("payDate", ""), "%Y-%m-%d").date()
        except (TypeError, ValueError) as exc:
            raise ValueError("Ungültiger JEPQ-Ex- oder Auszahlungstag") from exc
        amount = finite_number(item.get("dividendAmount"))
        if amount is None or amount <= 0 or pay_date < ex_date or ex_date.isoformat() in events:
            raise ValueError("JEPQ-Barausschüttung ist nicht eindeutig")
        events[ex_date.isoformat()] = dict(date=ex_date.isoformat(), dividend=amount, capital_gain=0.0)
    # A new capital-gain schema must not be silently added twice or ignored.
    # JEPQ's verified current response reports no separate capital-gain events.
    if capital_gains:
        raise ValueError("Separate JEPQ-Kapitalgewinnzahlungen benötigen eine eindeutig bestätigte Feldbasis")
    return dict(provider="JPMorgan Asset Management", source_url=JEPQ_RETURN_CASH_SOURCE,
                retrieved_at=stamp, events=[events[key] for key in sorted(events)])


def fetch_jepq_return_cash() -> dict[str, Any]:
    request = Request(JEPQ_RETURN_CASH_SOURCE, headers={"User-Agent": "research-studio/1.0", "Accept": "application/json"})
    with urlopen(request, timeout=8) as response:
        body = response.read(2_000_001)
    if len(body) > 2_000_000:
        raise ValueError("JEPQ-Emittentenantwort überschreitet das Größenlimit")
    return parse_jepq_return_cash(json.loads(body), iso_now())


def _completed_return_history(history: Any, info: dict[str, Any], now: datetime) -> Any:
    """Exclude future/current incomplete daily bars without changing SMA input."""
    import pandas as pd
    if not isinstance(history.index, pd.DatetimeIndex):
        raise ValueError("Renditehistorie enthält keine gültigen Tagesdaten")
    zone_name = info.get("exchangeTimezoneName")
    if not isinstance(zone_name, str):
        zone_name = str(history.index.tz) if history.index.tz is not None else "UTC"
    try:
        zone = ZoneInfo(zone_name)
    except (ValueError, KeyError):
        zone = timezone.utc
    local_now = now.astimezone(zone)
    local_index = history.index.tz_convert(zone) if history.index.tz is not None else history.index.tz_localize(zone)
    dates = local_index.date
    keep_today = False
    # Prefer the exchange's actual session boundary where Yahoo provides it.
    trading = info.get("currentTradingPeriod", {})
    regular = trading.get("regular", {}) if isinstance(trading, dict) else {}
    end_epoch = finite_number(regular.get("end")) if isinstance(regular, dict) else None
    has_today_boundary = False
    if end_epoch is not None:
        try:
            end = datetime.fromtimestamp(end_epoch, zone)
            if end.date() == local_now.date():
                has_today_boundary = True
                keep_today = now.timestamp() >= end_epoch + 30 * 60
        except (OverflowError, OSError, ValueError):
            pass
    if not has_today_boundary:
        closes = {"America/New_York": (16, 30), "US/Eastern": (16, 30),
                  "Asia/Tokyo": (16, 0), "Asia/Singapore": (17, 30),
                  "Europe/Berlin": (18, 0), "Europe/Paris": (18, 0),
                  "Europe/Zurich": (18, 0)}
        hour_minute = closes.get(zone_name)
        if hour_minute is not None:
            keep_today = (local_now.hour, local_now.minute) >= hour_minute
    mask = [date < local_now.date() or (date == local_now.date() and keep_today) for date in dates]
    return history.loc[mask]


# Outlook dividend and currency inputs. All cash histories use ex dates.
DIVIDEND_GROWTH_VERSION = 1
ECB_FX_SOURCE = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
DIVIDEND_GROWTH_METHOD = (
    "Geometrisches jährliches Wachstum (CAGR) der rollierenden 12-Monats-Barausschüttung je Aktie: "
    "(Enddividende / Anfangsdividende)^(1 / Jahre) − 1. Der reguläre Zeitraum sind zehn Kalenderjahre "
    "bis zum letzten abgeschlossenen Handelstag. Cash-Ereignisse werden dem Ex-Tag zugeordnet, "
    "nicht dem späteren Zahlungstag; linke Jahresgrenze offen, rechte Grenze geschlossen. "
    "Yahoo-Cashbeträge sind bereits auf die aktuelle Aktienbasis splitbereinigt und werden nicht "
    "erneut durch Splits dividiert. Enthalten sind gemeldete Sonderdividenden und Kapitalrückzahlungen; "
    "brutto vor Steuern/Depotgebühren, in der jeweiligen Handelswährung. Kürzere Historien oder ein "
    "Null-Anfangswert verwenden den frühesten vollständigen positiven 12M-Jahresanker innerhalb "
    "der zehn Jahre mit der üblichen Anzahl von Zahlungen. Eine solche Ersatzrate ist keine 10J-Rate."
)
DIVIDEND_PRIMARY_SOURCES = {
    "AMZN": "https://ir.aboutamazon.com/faqs/default.aspx",
    "AMD": "https://ir.amd.com/contacts-faq/faq",
    "NOW": "https://s205.q4cdn.com/916135447/files/doc_downloads/annual-meeting-of-share-holders/2026/NOW-2025-Annual-Report-bookmarked.pdf",
    "ANET": "https://s21.q4cdn.com/861911615/files/doc_financials/2025/ar/Arista-2026-Proxy-Statement-2025-Annual-Report-Bookmarked.pdf",
    "PANW": "https://www.sec.gov/Archives/edgar/data/1327567/000132756726000023/panw-20260731.htm",
    "CRWD": "https://www.sec.gov/Archives/edgar/data/1535527/000153552726000010/crwd-20260131.htm",
    "NET": "https://www.sec.gov/Archives/edgar/data/1477333/000147733326000016/cloud-20251231.htm",
    "GOOGL": "https://abc.xyz/assets/e1/57/8a65483e43feaa709f6cc5cc0737/annualreport2024-web.pdf",
    "META": "https://www.sec.gov/Archives/edgar/data/1326801/000132680124000012/meta-20231231.htm",
    "DTG.DE": "https://www.daimlertruck.com/en/investors/share/dividend",
    "NVDA": "https://investor.nvidia.com/news/press-release-details/2026/NVIDIA-Announces-Financial-Results-for-Second-Quarter-Fiscal-2027/",
    "MSFT": "https://www.microsoft.com/investor/reports/ar26/",
    "AAPL": "https://investor.apple.com/dividend-history/",
    "ALV.DE": "https://www.allianz.com/en/investor_relations/share/dividend.html",
    "MUV2.DE": "https://www.munichre.com/de/unternehmen/investoren/aktien/dividende.html",
    "8766.T": "https://www.tokiomarinehd.com/en/ir/stock/return.html",
    "D05.SI": "https://www.dbs.com/investors/dividend/default.page",
    "TSM": "https://investor.tsmc.com/english/dividend",
    "JEPQ": JEPQ_RETURN_CASH_SOURCE,
    "VOW3.DE": "https://annualreport2016.volkswagenag.com/notes/balance-sheet-disclosures/liabilities/equity.html",
}


def unavailable_dividend_growth(ticker: str, reason: str) -> dict[str, Any]:
    return dict(version=DIVIDEND_GROWTH_VERSION, ticker=ticker, status="unavailable", source_status="unavailable",
                rate_pct=None, projection_rate_pct=None, years=None, start=None, end=None,
                start_annual_dividend=None, end_annual_dividend=None, currency=None,
                method=DIVIDEND_GROWTH_METHOD, source_url=f"https://finance.yahoo.com/quote/{quote(ticker, safe='')}/history/",
                retrieved_at=None, warning=reason, observations=[])


def calculate_dividend_growth(history: Any, ticker: str, info: dict[str, Any] | None = None,
                              retrieved_at: str | None = None, issuer_cash: dict[str, Any] | None = None) -> dict[str, Any]:
    """Compare uniform, already split-normalized 12M cash per listed share."""
    import pandas as pd
    stamp = retrieved_at or iso_now()
    now = datetime.fromisoformat(stamp)
    if now.tzinfo is None:
        raise ValueError("Dividenden-Abrufzeitpunkt hat keine Zeitzone")
    if history is None or history.empty or "Dividends" not in history or "Close" not in history:
        raise ValueError("Vollständige Kurs-/Dividendenhistorie fehlt")
    frame = history.sort_index().copy()
    frame = frame.loc[~frame.index.duplicated(keep="last")]
    frame = _completed_return_history(frame, info or {}, now)
    if frame.empty:
        raise ValueError("Keine abgeschlossene Dividendensitzung")
    dates = pd.DatetimeIndex([pd.Timestamp(index.date()) for index in frame.index])
    if dates.duplicated().any():
        raise ValueError("Doppelte lokale Handelstage")
    cash = pd.Series(pd.to_numeric(frame["Dividends"], errors="coerce").to_numpy(), index=dates)
    corrections = []
    if ticker == "JEPQ":
        if (not isinstance(issuer_cash, dict) or issuer_cash.get("provider") != "JPMorgan Asset Management"
                or issuer_cash.get("source_url") != JEPQ_RETURN_CASH_SOURCE):
            raise ValueError("JEPQ benötigt bestätigte Emittenten-Ausschüttungen")
        # The issuer endpoint covers the latest rolling year, not inception.
        # Keep older Yahoo events, and replace (never add) the covered tail.
        issuer_dates = [pd.Timestamp(item["date"]) for item in issuer_cash.get("events", [])]
        if not issuer_dates:
            raise ValueError("Keine JEPQ-Emittentenereignisse")
        cash.loc[(cash.index >= min(issuer_dates)) & (cash.index <= max(issuer_dates))] = 0.0
        for item in issuer_cash.get("events", []):
            date = pd.Timestamp(item["date"])
            amount = finite_number(item.get("dividend"))
            gain = finite_number(item.get("capital_gain"))
            if amount is None or amount < 0 or gain is None or gain < 0:
                raise ValueError("Ungültiges JEPQ-Cashereignis")
            if date in cash.index:
                cash.loc[date] = amount + gain
        if not (cash > 0).any() or (dates[-1] - cash[cash > 0].index[-1]).days > 45:
            raise ValueError("JEPQ-Emittentenereignisse sind nicht aktuell vollständig")
    elif "Capital Gains" in frame:
        gains = pd.Series(pd.to_numeric(frame["Capital Gains"], errors="coerce").to_numpy(), index=dates)
        if ((gains > 0) & (cash > 0) & ((gains - cash).abs() < 1e-7)).any():
            raise ValueError("Nicht eindeutig getrennte doppelte Barausschüttung")
        cash += gains
    if ticker == TOKIO_TICKER:
        date = pd.Timestamp("2026-09-29")
        if date in cash.index:
            amount = finite_number(cash.loc[date])
            split = finite_number(frame.loc[frame.index[dates.get_loc(date)], "Stock Splits"]) if "Stock Splits" in frame else None
            if amount is not None and math.isclose(amount, 122.5, abs_tol=1e-6) and split == 15:
                cash.loc[date] = amount / 15
                corrections.append(dict(date="2026-09-29", original=amount, normalized=amount / 15,
                                        source_url=TOKIO_DIVIDEND_SOURCE, reason="Einzeln bestätigtes fehlerhaftes 15:1-Splitereignis"))
            elif amount is None or not math.isclose(amount, 122.5 / 15, abs_tol=.01):
                raise ValueError("Tokio-Splitdividende nicht auf einheitlicher Aktienbasis bestätigt")
    if not cash.map(lambda value: finite_number(value) is not None and value >= 0).all():
        raise ValueError("Negative oder fehlende historische Cashereignisse")
    currency = (info or {}).get("currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("Währung der Dividendenhistorie fehlt")
    end, first = dates[-1], dates[0]
    source_url = f"https://finance.yahoo.com/quote/{quote(ticker, safe='')}/history/"
    def observation(date: Any) -> dict[str, Any]:
        left = date - pd.DateOffset(years=1)
        in_window = (dates > left) & (dates <= date)
        complete = first <= left + pd.Timedelta(days=5) and int(in_window.sum()) >= 180
        values = cash.loc[in_window]
        event_count = int((values > 0).sum())
        # This fund distributes monthly. A missing month is not a genuine zero.
        if ticker == "JEPQ" and event_count < 12:
            complete = False
        return dict(as_of=date.date().isoformat(), window_start_exclusive=left.date().isoformat(),
                    annual_dividend=round(float(values.sum()), 10) if complete else None,
                    event_count=event_count, complete=bool(complete))
    observations = [observation(end - pd.DateOffset(years=back)) for back in range(10, -1, -1)]
    latest = observations[-1]
    if not latest["complete"]:
        raise ValueError("Keine vollständige aktuelle 12M-Dividendenhistorie")
    annual = latest["annual_dividend"]
    recent_dates = cash[(cash > 0) & (dates > end - pd.DateOffset(years=5))].index
    gaps = [(recent_dates[i] - recent_dates[i - 1]).days for i in range(1, len(recent_dates))]
    cadence = median(gaps) if gaps else 365
    expected = min([1, 2, 4, 12], key=lambda frequency: abs(365.2425 / frequency - cadence))
    start_observation = observations[0]
    status, rate, projected, years, reason = "confirmed", None, None, 10.0, ""
    if not (cash > 0).any():
        if ticker not in {"AMZN", "NOW", "AMD", "ANET", "PANW", "CRWD", "NET"}:
            raise ValueError("Leere Cashhistorie eines nicht als Nichtzahler bestätigten Wertpapiers; keine Null-Dividende erfinden")
        status, projected, years = "no_dividend", 0.0, None
        reason = "Keine gemeldeten Barausschüttungen in der verfügbaren Historie; es wird keine künftige Dividende erfunden. Kein definiertes Dividendenwachstum."
        start_observation = dict(as_of=None, annual_dividend=None)
    else:
        if not start_observation["complete"] or start_observation["annual_dividend"] <= 0:
            candidates = [row for row in observations[:-1] if row["complete"] and row["annual_dividend"] > 0 and row["event_count"] >= expected]
            if candidates:
                start_observation = candidates[0]
                years = float(10 - observations.index(start_observation))
                status = "short_history"
                reason = ("Keine definierte vollständige 10J-CAGR (zu kurze Börsen-/Dividendenhistorie oder Null-Anfangsdividende). "
                          "Ersatzrate aus dem frühesten vollständigen positiven 12M-Jahresanker mit üblicher Zahl von Ausschüttungen.")
            else:
                status, projected, years = "assumption", 0.0, None
                reason = "Noch keine zwei vollständigen positiven 12M-Jahresanker: neutral 0% Dividendendynamik als ausdrücklich markierte Annahme, keine historische 10J-Rate."
                start_observation = dict(as_of=None, annual_dividend=None)
        if years is not None:
            initial_cash = start_observation["annual_dividend"]
            rate = -100.0 if annual == 0 else (math.pow(annual / initial_cash, 1 / years) - 1) * 100
            rate = round(rate, 8)
            projected = rate
    warning = reason + (" " if reason else "") + "Ex-Tag-Basis, brutto; gemeldete Sonder-/Kapitalrückzahlungen sind enthalten. Keine Prognose oder Garantie."
    if ticker == "NVDA":
        warning += " Die 2026 erfolgte Anhebung der Quartalsdividende von 0,01 auf 0,25 USD beeinflusst die CAGR stark; nicht als nachhaltige Wachstumszusage verstehen."
    if ticker == "VOW3.DE":
        warning += " Die niedrige Ausgangszahlung von 0,17 EUR im Jahr2016 (Geschäftsjahr2015) ist außergewöhnlich; die daraus hohe Zehnjahresrate ist kein normalisiertes oder verlässliches künftiges Wachstum."
    if ticker == "D05.SI":
        warning += " DBS: 1:10-Bonusaktien 2024 sind bereits in Yahoo-DPS berücksichtigt; aktuelle 0,15 SGD Kapitalrückzahlung je Quartal ist zeitlich bis2027 geplant und wird im Szenario dennoch nur als unveränderte historische Basis fortgeschrieben."
    if ticker == "TSM":
        warning += " TSM an der NYSE: USD-Cash je ADS (ein ADS repräsentiert fünf Stammaktien); historische USD-Zahlungen enthalten Wechselkurseinflüsse und gegebenenfalls Depotbankeffekte. Keine TWD-Stammdividenden mit USD-ADS-Kursen gemischt."
    result = dict(version=DIVIDEND_GROWTH_VERSION, ticker=ticker, status=status, source_status="live",
                  rate_pct=rate, projection_rate_pct=projected, years=years, start=start_observation["as_of"],
                  end=end.date().isoformat(), start_annual_dividend=start_observation["annual_dividend"],
                  end_annual_dividend=annual, currency=currency, method=DIVIDEND_GROWTH_METHOD,
                  source_url=source_url, retrieved_at=stamp, warning=warning, observations=observations,
                  share_basis="Aktuelle gelistete Aktien-/ADS-Basis; Cashbeträge bereits splitbereinigt",
                  date_basis="ex_dividend_date", expected_payments_per_year=expected,
                  history_since=first.date().isoformat(), cash_events=[dict(date=index.date().isoformat(), amount=round(float(value), 10), source_url=JEPQ_RETURN_CASH_SOURCE if ticker == "JEPQ" and index in issuer_dates else source_url) for index, value in cash.items() if value > 0 and index > end - pd.DateOffset(years=11)])
    if ticker == "JEPQ":
        result["cash_source_url"] = JEPQ_RETURN_CASH_SOURCE
        result["issuer_cash_first_date"] = min(issuer_dates).date().isoformat()
        result["warning"] += " JEPQ: jüngste Emittentenereignisse ersetzen den Yahoo-Cash-Tail; ältere Vergleichszahlungen aus Yahoo-Historie. Optionsprämien-/Kapitalerträge sind Fonds-Ausschüttungen, keine Unternehmensdividenden."
    if ticker in DIVIDEND_PRIMARY_SOURCES:
        result["primary_source_url"] = DIVIDEND_PRIMARY_SOURCES[ticker]
    result["source_urls"] = list(dict.fromkeys([source_url] + ([result["cash_source_url"]] if "cash_source_url" in result else []) + ([result["primary_source_url"]] if "primary_source_url" in result else [])))
    if corrections:
        result["cash_corrections"] = corrections
    return result


def valid_dividend_growth(value: Any, ticker: str, currency: str | None = None) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("version") != DIVIDEND_GROWTH_VERSION or value.get("ticker") != ticker:
        return None
    if value.get("status") not in {"confirmed", "short_history", "no_dividend", "assumption"}:
        return None
    if value.get("source_url") != f"https://finance.yahoo.com/quote/{quote(ticker, safe='')}/history/":
        return None
    if currency and value.get("currency") != currency:
        return None
    annual = finite_number(value.get("end_annual_dividend"))
    projected = finite_number(value.get("projection_rate_pct"))
    if annual is None or annual < 0 or projected is None or projected < -100:
        return None
    try:
        end = datetime.fromisoformat(value["end"]).date()
        stamp = datetime.fromisoformat(value["retrieved_at"])
        if stamp.tzinfo is None or end > stamp.date():
            return None
        if value["status"] in {"confirmed", "short_history"}:
            start = datetime.fromisoformat(value["start"]).date()
            initial = finite_number(value.get("start_annual_dividend"))
            years = finite_number(value.get("years"))
            rate = finite_number(value.get("rate_pct"))
            if initial is None or initial <= 0 or years is None or not 0 < years <= 10 or rate is None or start >= end:
                return None
            expected = -100 if annual == 0 else (math.pow(annual / initial, 1 / years) - 1) * 100
            if not math.isclose(rate, expected, abs_tol=1e-6) or not math.isclose(rate, projected, abs_tol=1e-6):
                return None
            if value["status"] == "confirmed" and years != 10:
                return None
        elif value.get("rate_pct") is not None or projected != 0:
            return None
        if value["status"] == "no_dividend" and annual != 0:
            return None
    except (KeyError, ValueError, TypeError, OverflowError):
        return None
    return deepcopy(value)


def apply_dividend_growth(stock: dict[str, Any], previous: dict[str, Any], payload: dict[str, Any]) -> None:
    fresh = valid_dividend_growth(payload.get("dividend_growth"), stock["ticker"], stock.get("currency"))
    prior = valid_dividend_growth(previous.get("dividend_growth"), stock["ticker"], stock.get("currency"))
    chosen = fresh or prior
    if chosen:
        chosen["source_status"] = "live" if fresh else "previous"
        if not fresh:
            chosen["refresh_note"] = payload.get("dividend_growth_error") or "Kein neuer bestätigter Dividendenstand; Vorwert mit ursprünglichem Datum."
        stock["dividend_growth"] = chosen
        stock["dividend_annual"] = chosen["end_annual_dividend"]
        price = finite_number(stock.get("price"))
        stock["dividend_annual_yield_pct"] = chosen["end_annual_dividend"] / price * 100 if price is not None and price > 0 else None
    else:
        stock["dividend_growth"] = unavailable_dividend_growth(stock["ticker"], payload.get("dividend_growth_error") or "Dividendenhistorie noch nicht bestätigt")
        stock["dividend_annual"] = None
        stock["dividend_annual_yield_pct"] = None


def parse_ecb_fx(body: bytes, retrieved_at: str | None = None) -> dict[str, Any]:
    import xml.etree.ElementTree as ET
    root = ET.fromstring(body)
    dated = next((node for node in root.iter() if node.attrib.get("time")), None)
    if dated is None:
        raise ValueError("EZB-Datum fehlt")
    date = datetime.strptime(dated.attrib["time"], "%Y-%m-%d").date()
    stamp = retrieved_at or iso_now()
    if date > datetime.fromisoformat(stamp).date():
        raise ValueError("EZB-Kurs liegt in der Zukunft")
    rates = {"EUR": 1.0}
    for node in dated:
        code, rate = node.attrib.get("currency"), finite_number(node.attrib.get("rate"))
        if code in {"USD", "JPY", "CHF", "SGD"} and rate is not None and rate > 0:
            rates[code] = rate
    if set(rates) != {"EUR", "USD", "JPY", "CHF", "SGD"}:
        raise ValueError("Benötigte EZB-Währungen fehlen")
    return dict(base="EUR", rates=rates, as_of=date.isoformat(), source_url=ECB_FX_SOURCE,
                updated_at=stamp, status="live", method="1 EUR = rate Einheiten Fremdwährung; EUR-Wert = nativer Kurs × Stückzahl ÷ rate. Referenzkurse, keine handelbaren Brokerkurse.")


def valid_ecb_fx(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("base") != "EUR" or value.get("source_url") != ECB_FX_SOURCE or not isinstance(value.get("rates"), dict):
        return None
    if value["rates"].get("EUR") != 1 or any(finite_number(value["rates"].get(code)) is None or not 0 < value["rates"][code] < 1e9 for code in ["USD", "JPY", "CHF", "SGD"]):
        return None
    try:
        date = datetime.strptime(value["as_of"], "%Y-%m-%d").date()
        stamp = datetime.fromisoformat(value["updated_at"])
        if stamp.tzinfo is None or date > stamp.date():
            return None
    except (KeyError, ValueError, TypeError):
        return None
    return deepcopy(value)


def refresh_ecb_fx(previous: Any, fetcher: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    try:
        if fetcher:
            supplied = valid_ecb_fx(fetcher())
            if supplied is None:
                raise ValueError("Ungültige EZB-Kursantwort")
            supplied["status"] = "live"
            return supplied
        context = ssl.create_default_context()
        try:
            # Some local Python distributions omit the ECB issuer's CA from
            # their system store. Add the Mozilla roots already distributed
            # with yfinance's HTTPS dependencies; never disable verification.
            import certifi
            context.load_verify_locations(cafile=certifi.where())
        except ImportError:
            pass
        with urlopen(Request(ECB_FX_SOURCE, headers={"User-Agent": "ResearchStudio/1.0"}), timeout=8, context=context) as response:
            body = response.read(100_001)
        if len(body) > 100_000:
            raise ValueError("EZB-Antwort zu groß")
        return parse_ecb_fx(body)
    except Exception as exc:
        fallback = valid_ecb_fx(previous)
        if fallback:

            fallback.update(status="previous", refresh_note=f"EZB-Abruf: {type(exc).__name__}; Vorwert mit ursprünglichem Datum")
            return fallback
        return dict(base="EUR", rates={"EUR": 1.0}, as_of=None, updated_at=None, status="unavailable", source_url=ECB_FX_SOURCE,
                    refresh_note=f"Kein bestätigter EZB-Kurs: {type(exc).__name__}")


def calculate_period_returns(history: Any, ticker: str, info: dict[str, Any] | None = None,
                             retrieved_at: str | None = None, issuer_cash: dict[str, Any] | None = None) -> dict[str, Any]:
    """Reinvest cash dividends on raw split-adjusted Close, never Adj Close."""
    import pandas as pd
    stamp = retrieved_at or iso_now()
    now = datetime.fromisoformat(stamp)
    if now.tzinfo is None:
        raise ValueError("Rendite-Abrufzeitpunkt hat keine Zeitzone")
    if history is None or history.empty or "Close" not in history or "Dividends" not in history:
        raise ValueError("Vollständige Kurs-/Ausschüttungshistorie fehlt")
    frame = history.sort_index().copy()
    frame = frame.loc[~frame.index.duplicated(keep="last")]
    frame = _completed_return_history(frame, info or {}, now)
    if frame.empty:
        raise ValueError("Keine abgeschlossenen Handelssitzungen verfügbar")
    close = pd.to_numeric(frame["Close"], errors="coerce")
    dividend = pd.to_numeric(frame["Dividends"], errors="coerce")
    capital = pd.to_numeric(frame["Capital Gains"], errors="coerce") if "Capital Gains" in frame else pd.Series(0.0, index=frame.index)
    issuer_cash_applied = []
    if ticker == "JEPQ" and issuer_cash is not None:
        if issuer_cash.get("provider") != "JPMorgan Asset Management" or issuer_cash.get("source_url") != JEPQ_RETURN_CASH_SOURCE:
            raise ValueError("JEPQ-Ausschüttungsquelle nicht bestätigt")
        dates_to_indices = {index.date().isoformat(): index for index in frame.index}
        seen_cash_dates = set()
        for item in issuer_cash.get("events", []):
            date = item.get("date") if isinstance(item, dict) else None
            amount = finite_number(item.get("dividend")) if isinstance(item, dict) else None
            gain = finite_number(item.get("capital_gain")) if isinstance(item, dict) else None
            if not isinstance(date, str) or date in seen_cash_dates or amount is None or amount < 0 or gain is None or gain < 0:
                raise ValueError("JEPQ-Emittentenereignisse nicht eindeutig")
            seen_cash_dates.add(date)
            if date in dates_to_indices:
                index = dates_to_indices[date]
                dividend.loc[index] = amount
                capital.loc[index] = gain
                issuer_cash_applied.append(dict(date=date, dividend=amount, capital_gain=gain))
    bad_dates = {}
    for index in frame.index:
        if finite_number(close.loc[index]) is None or close.loc[index] <= 0:
            bad_dates[index.date()] = "Ungültiger Schlusskurs im Zeitraum"
        if (finite_number(dividend.loc[index]) is None or dividend.loc[index] < 0 or
                finite_number(capital.loc[index]) is None or capital.loc[index] < 0):
            bad_dates[index.date()] = "Ungültige Ausschüttung im Zeitraum"
    corrections = []
    if ticker == TOKIO_TICKER:
        # Yahoo's one malformed 15:1 split-day event retains the old share basis.
        # Older cash events are already normalized: never divide them again.
        for index in frame.index:
            if index.date().isoformat() != "2026-09-29":
                continue
            amount = finite_number(dividend.loc[index])
            split = finite_number(frame.loc[index, "Stock Splits"]) if "Stock Splits" in frame else None
            if amount is not None and math.isclose(amount, 122.5, abs_tol=0.000001) and split == 15:
                dividend.loc[index] = amount / 15
                corrections.append(dict(date="2026-09-29", original=122.5, normalized=122.5 / 15,
                                        source_url=TOKIO_DIVIDEND_SOURCE,
                                        reason="Emittenten-Interimsdividende 122,5 JPY auf 15:1-Aktienbasis normiert; nur dieses fehlerhafte Yahoo-Ereignis."))
            elif amount is None or not math.isclose(amount, 122.5 / 15, abs_tol=0.01):
                bad_dates[index.date()] = "Tokio-Splitdividende nicht eindeutig auf einheitlicher Aktienbasis bestätigt"
    valid = close.map(lambda value: finite_number(value) is not None and value > 0)
    close = close.loc[valid]
    dividend = dividend.loc[valid]
    capital = capital.loc[valid]
    if len(close) < 2:
        raise ValueError("Weniger als zwei gültige abgeschlossene Tageskurse")
    # Separate capital-gain cash is added only when it is independently reported;
    # duplicated identical nonzero fields are ambiguous and not double-counted.
    duplicate_cash = (dividend > 0) & (capital > 0) & ((dividend - capital).abs() < 0.0000001)
    for index in duplicate_cash[duplicate_cash].index:
        bad_dates[index.date()] = "Ausschüttung und Kapitalgewinnzahlung doppelt oder nicht eindeutig getrennt"
    cash = dividend + capital
    ratios = close / close.shift(1)
    if ticker == TOKIO_TICKER:
        # The provider's early Tokio history contains unexplained unit jumps.
        # Only windows crossing such a jump are ineligible, not the whole row.
        for index in ratios.index[1:]:
            ratio = float(ratios.loc[index])
            if ratio > 1.8 or ratio < 0.5:
                bad_dates[index.date()] = "Ungeklärter extremer Kurssprung in älterer Tokio-Historie"
    factors = (close + cash) / close.shift(1)
    dates = pd.DatetimeIndex([pd.Timestamp(index.date()) for index in close.index])
    end = dates[-1]
    first = dates[0]
    result = dict(version=RETURN_VERSION, basis="total_return", source_status="live",
                  as_of=end.date().isoformat(), retrieved_at=stamp, since=first.date().isoformat(),
                  source_url=f"https://finance.yahoo.com/quote/{quote(ticker, safe='')}/history/",
                  method=RETURN_METHOD, currency=(info or {}).get("currency"), periods={})
    cash_dates = [index.date().isoformat() for index in cash[cash > 0].index]
    result["last_distribution_date"] = cash_dates[-1] if cash_dates else None
    result["cash_events_count"] = len(cash_dates)
    if ticker == "JEPQ":
        # JEPQ distributes monthly. The known Yahoo max-history endpoint can
        # omit recent events; an old cash tail is not a complete total return.
        if not cash_dates or (end.date() - datetime.strptime(cash_dates[-1], "%Y-%m-%d").date()).days > 45:
            raise ValueError("JEPQ-Barausschüttungen nicht vollständig: letzter bestätigter Ex-Tag liegt mehr als 45 Tage zurück")
        if issuer_cash_applied:
            result["cash_source_url"] = JEPQ_RETURN_CASH_SOURCE
            result["cash_retrieved_at"] = issuer_cash.get("retrieved_at")
            result["issuer_cash_events"] = issuer_cash_applied
            result["issuer_cash"] = dict(provider="JPMorgan AM", source_url=JEPQ_RETURN_CASH_SOURCE,
                                         retrieved_at=issuer_cash.get("retrieved_at"), event_count=len(issuer_cash_applied))
            result["method"] += " JEPQ: die zuletzt veröffentlichten Barausschüttungen stammen vom Fondsanbieter; sie ersetzen Yahoo-Ereignisse am selben Ex-Tag und werden nicht doppelt addiert. Quelle: " + JEPQ_RETURN_CASH_SOURCE
    if corrections:
        result["cash_corrections"] = corrections
        result["method"] += " Tokio Marine: die bestätigte Splitdividende vom 29.09.2026 ist einzeln normalisiert; Yahoo Adj Close wird nicht verwendet."
    if bad_dates:
        result["quality_notes"] = [dict(date=date.isoformat(), reason=reason) for date, reason in sorted(bad_dates.items())]
    cutoffs = {"1w": end - pd.DateOffset(weeks=1), "1m": end - pd.DateOffset(months=1),
               "6m": end - pd.DateOffset(months=6), "ytd": pd.Timestamp(end.year - 1, 12, 31),
               "1y": end - pd.DateOffset(years=1), "3y": end - pd.DateOffset(years=3),
               "5y": end - pd.DateOffset(years=5), "10y": end - pd.DateOffset(years=10)}
    for key in RETURN_PERIODS:
        annualized = key in {"3y", "5y", "10y"} or (key == "all" and (end - first).days >= 365.2425)
        entry = dict(value=None, start=None, end=result["as_of"], annualized=annualized)
        if key == "1d":
            start_index = len(close) - 2
        elif key == "all":
            start_index = 0
        else:
            cutoff = cutoffs[key]
            eligible = dates <= cutoff
            if not eligible.any():
                entry["reason"] = "Historie deckt den vollständigen Zeitraum nicht ab"
                result["periods"][key] = entry
                continue
            start_index = int(eligible.nonzero()[0][-1])
            if (cutoff - dates[start_index]).days > 10:
                entry["reason"] = "Kein ausreichend naher Handelsschluss am Zeitraumstart"
                result["periods"][key] = entry
                continue
        start = dates[start_index]
        entry["start"] = start.date().isoformat()
        elapsed = (end - start).days
        problems = [reason for date, reason in bad_dates.items() if start.date() < date <= end.date()]
        if elapsed <= 0:
            entry["reason"] = "Zeitraum umfasst keine vollständige Handelssitzung"
        elif key == "1d" and elapsed > 10:
            entry["reason"] = "Vorherige Handelssitzung liegt mehr als zehn Kalendertage zurück"
        elif problems:
            entry["reason"] = problems[0]
        else:
            growth = finite_number(factors.iloc[start_index + 1:].prod(skipna=False))
            if growth is None or growth <= 0:
                entry["reason"] = "Gesamtrendite aus Kursen und Ausschüttungen nicht gültig berechenbar"
            else:
                try:
                    value = (growth ** (365.2425 / elapsed) - 1) * 100 if annualized else (growth - 1) * 100
                except (OverflowError, ValueError):
                    value = None
                value = finite_number(value)
                if value is None:
                    entry["reason"] = "Gesamtrendite außerhalb des gültigen Zahlenbereichs"
                else:
                    entry["value"] = round(value, 6)
        result["periods"][key] = entry
    # Keep every requested observed period unchanged. This separate observation
    # lets a projection disclose a shorter actual window instead of pretending
    # that missing pre-IPO years or a discontinuous early history were observed.
    excluded_through = max(bad_dates) if bad_dates else None
    clean_indices = [index for index, date in enumerate(dates)
                     if excluded_through is None or date.date() >= excluded_through]
    clean_start = clean_indices[0] if clean_indices else None
    clean_date = dates[clean_start] if clean_start is not None else None
    clean_days = (end - clean_date).days if clean_date is not None else 0
    available = dict(
        version=1, basis="total_return", currency=result["currency"],
        source_url=result["source_url"], retrieved_at=result["retrieved_at"],
        value=None, start=clean_date.date().isoformat() if clean_date is not None else None,
        end=result["as_of"], annualized=clean_days >= 365.2425,
        observations=len(close) - clean_start if clean_start is not None else 0,
        excluded_through=excluded_through.isoformat() if excluded_through is not None else None,
        method=AVAILABLE_HISTORY_METHOD,
        reason=("Frühere Zeiträume mit erkannten Datenbrüchen bleiben ausgeschlossen; "
                "die Rendite über den letzten Bruch wird nicht verwendet; Ausgangsbasis ist der erste gültige Schlusskurs am oder nach diesem Bruch."
                if excluded_through is not None else
                "Nutzt die vollständige verfügbare Kurshistorie; fehlende Jahre vor deren Beginn werden nicht ergänzt."))
    if clean_start is None or clean_days <= 0 or len(close) - clean_start < 2:
        available["reason"] = "Nach dem letzten erkannten Datenbruch liegen noch keine zwei gültigen Schlusskurse vor."
    else:
        growth = finite_number(factors.iloc[clean_start + 1:].prod(skipna=False))
        try:
            rate = ((growth ** (365.2425 / clean_days) - 1) * 100 if available["annualized"]
                    else (growth - 1) * 100) if growth is not None and growth > 0 else None
        except (OverflowError, ValueError):
            rate = None
        rate = finite_number(rate)
        if rate is None or rate <= -100:
            available["reason"] = "Gesamtrendite im zusammenhängenden Zeitraum nicht gültig berechenbar."
        else:
            available["value"] = round(rate, 6)
    result["available_history"] = available
    return result


O_SPINOFF_DATE = "2021-11-15"
O_SPINOFF_SOURCE = "https://www.sec.gov/Archives/edgar/data/726728/000072672822000046/R37.htm"
O_SPINOFF_FAIR_VALUE_PER_ONL_SHARE = 20.6272
O_SPINOFF_ONL_SHARES_PER_O_SHARE = 0.1


def prepare_reserve_history(history: Any, ticker: str) -> tuple[Any, Any, list[dict[str, Any]]]:
    """Keep O's synthetic noncash adjustment out of cash DPS and pure prices."""
    import pandas as pd
    cash_history = history.copy()
    actions: list[dict[str, Any]] = []
    if ticker != "O":
        return cash_history, cash_history.copy(), actions
    dates = pd.DatetimeIndex([pd.Timestamp(index.date()) for index in cash_history.index])
    event = pd.Timestamp(O_SPINOFF_DATE)
    if event not in dates:
        if len(dates) and dates.min() < event < dates.max():
            raise ValueError("Realty Income: Orion-Stichtag fehlt innerhalb der Historie; keine ungesicherte Langfristbasis verwenden")
        return cash_history, cash_history.copy(), actions
    location = dates.get_loc(event)
    factor = finite_number(cash_history.iloc[location].get("Stock Splits"))
    if factor is None or not math.isclose(factor, 1.032, abs_tol=1e-6):
        raise ValueError("Realty Income: providerseitige Orion-Rückrechnung nicht als bestätigter Faktor 1,032 erkannt; keine ungesicherte 10J-Basis verwenden")
    before = dates < event
    for field in ("Close", "Dividends", "Capital Gains"):
        if field in cash_history:
            cash_history.loc[before, field] = cash_history.loc[before, field] * factor
    cash_history.iloc[location, cash_history.columns.get_loc("Stock Splits")] = 0.0
    total_history = cash_history.copy()
    if "Capital Gains" not in total_history:
        total_history["Capital Gains"] = 0.0
    existing = finite_number(total_history.iloc[location]["Capital Gains"])
    equivalent = O_SPINOFF_FAIR_VALUE_PER_ONL_SHARE * O_SPINOFF_ONL_SHARES_PER_O_SHARE
    if existing is None or existing != 0:
        raise ValueError("Realty Income: zusätzliche, nicht eindeutig getrennte Orion-Bar-/Sachkomponente")
    total_history.iloc[location, total_history.columns.get_loc("Capital Gains")] = equivalent
    actions.append(dict(
        type="noncash_spinoff", effective_date="2021-11-12", calculation_date=O_SPINOFF_DATE,
        parent_ticker="O", distributed_ticker="ONL", child_shares_per_parent_share=0.1,
        provider_synthetic_split_undone=factor,
        normalized_fields=["Close", "Dividends", "Capital Gains"],
        child_fair_value_usd=O_SPINOFF_FAIR_VALUE_PER_ONL_SHARE,
        noncash_equivalent_usd_per_parent_share=round(equivalent, 8),
        valuation_basis="Vom Emittenten veröffentlichter 5-Tage-VWAP nach Ausgabe; kein belegter Schlusskurs des ersten Handelstags",
        source_url=O_SPINOFF_SOURCE,
        method="Vor dem 15.11.2021 werden Yahoos mit 1,032 rückgerechnete O-Kurse und Cash-Dividenden auf die tatsächliche O-Aktienbasis zurückgeführt. Die Orion-Zuteilung von 1 ONL je 10 O ist kein O-Aktiensplit. Nur für Gesamtrenditen wird ihr publizierter steuerlicher Fair Value von 20,6272 USD je ONL (2,06272 USD je O) am ersten regulären Handelstag als fiktiver Verkauf und Wiederanlage in O angesetzt. Diese bewertete Sachdividende ist eine ausdrücklich gekennzeichnete Näherung; sie ist weder regulärer Cash noch Teil des DPS-Wachstums. Reine Kursrenditen verwenden die zurückgeführten Kurse ohne Sachdividende."))
    return cash_history, total_history, actions


def calculate_reserve_history_payload(history: Any, ticker: str, info: dict[str, Any], stamp: str) -> dict[str, Any]:
    """Return isolated price, cash-DPS and total-return histories for reserves."""
    cash_history, total_history, actions = prepare_reserve_history(history, ticker)
    payload: dict[str, Any] = {}
    try:
        growth = calculate_dividend_growth(cash_history, ticker, info, stamp)
        if actions:
            growth["corporate_actions"] = deepcopy(actions)
            growth["warning"] += " Realty Income: Orion-Sachdividende 2021 ist aus der Cash-DPS-Reihe ausgeschlossen; die synthetische Provider-Splitrückrechnung wurde rückgängig gemacht."
            growth["share_basis"] = "Tatsächliche O-Common-Share-Basis; Orion-Sachdividende und synthetischer 1,032-Faktor sind keine O-Aktienvermehrung"
        payload["dividend_growth"] = growth
    except Exception as exc:
        payload["dividend_growth_error"] = f"Reserve-Dividendenhistorie: {type(exc).__name__}: {str(exc)[:180]}"
    try:
        total = calculate_period_returns(total_history, ticker, info, stamp)
        if actions:
            total["corporate_actions"] = deepcopy(actions)
            total["cash_component_note"] = "Zusätzlich zur Cash-Wiederanlage enthält die Reihe die bewertete Orion-Sachdividende; Zeiträume vor 15.11.2021 nutzen deren publizierten 5-Tage-VWAP als Wiederanlage-Näherung."
            for period in total["periods"].values():
                if period.get("value") is not None and period.get("start", "9999") < O_SPINOFF_DATE:
                    period["approximation"] = "Orion-Sachdividende zum veröffentlichten 5-Tage-VWAP statt unbelegtem erstem Schlusskurs bewertet"
            if total.get("available_history", {}).get("start", "9999") < O_SPINOFF_DATE:
                total["available_history"]["corporate_action_approximation"] = True
        payload["returns"] = total
    except Exception as exc:
        payload["returns_error"] = f"Reserve-Gesamtrendite: {type(exc).__name__}: {str(exc)[:180]}"
    try:
        price = calculate_reserve_price_returns(cash_history, ticker, info, stamp)
        if actions:
            price["corporate_actions"] = deepcopy(actions)
            price["cash_component_note"] = "Keine Cash-/Sachdividenden; O-Kurse vor Orion-Abspaltung aus der synthetischen 1,032-Provider-Rückrechnung zurückgeführt."
        payload["price_returns"] = price
    except Exception as exc:
        payload["price_returns_error"] = f"Reserve-Kursrendite: {type(exc).__name__}: {str(exc)[:180]}"
    return payload


def calculate_reserve_price_returns(history: Any, ticker: str, info: dict[str, Any] | None = None,
                                    retrieved_at: str | None = None) -> dict[str, Any]:
    """Use the same periods and split-adjusted closes, excluding every payout.

    The caller can provide issuer-corrected closes for corporate actions without
    changing the cash-inclusive return calculation or mutating its input frame.
    """
    if history is None:
        raise ValueError("Kursrenditehistorie fehlt")
    price_history = history.copy(deep=True)
    price_history["Dividends"] = 0.0
    price_history["Capital Gains"] = 0.0
    result = calculate_period_returns(price_history, ticker, info, retrieved_at)
    result.update(basis="price_return", type="price_return", method=PRICE_RETURN_METHOD)
    available = result.get("available_history")
    if isinstance(available, dict):
        available.update(basis="price_return", method=PRICE_RETURN_METHOD,
                         reason=("Kursentwicklung ohne Dividenden oder Ausschüttungen im längsten "
                                 "verfügbaren Zeitraum ohne erkannten Datenbruch; fehlende Jahre werden nicht ergänzt."))
    for period in result["periods"].values():
        if "reason" in period:
            period["reason"] = period["reason"].replace("Gesamtrendite", "Kursrendite")
    return result


def valid_available_history(available: Any, parent: dict[str, Any], *,
                            basis: str = "total_return") -> bool:
    """Validate optional projection provenance without accepting invented years."""
    if (not isinstance(available, dict) or available.get("version") != 1 or
            available.get("basis") != basis or
            not isinstance(parent.get("currency"), str) or not parent["currency"] or
            available.get("currency") != parent["currency"] or
            available.get("source_url") != parent.get("source_url") or
            available.get("retrieved_at") != parent.get("retrieved_at") or
            available.get("end") != parent.get("as_of") or
            available.get("method") != (AVAILABLE_HISTORY_METHOD if basis == "total_return" else PRICE_RETURN_METHOD) or
            not isinstance(available.get("reason"), str) or not available["reason"].strip() or
            type(available.get("annualized")) is not bool or
            type(available.get("observations")) is not int or available["observations"] < 0):
        return False
    try:
        first = datetime.strptime(parent["since"], "%Y-%m-%d").date()
        end = datetime.strptime(parent["as_of"], "%Y-%m-%d").date()
        start = datetime.strptime(available.get("start", ""), "%Y-%m-%d").date()
        notes = parent.get("quality_notes", [])
        if not isinstance(notes, list) or any(not isinstance(note, dict) or
                                             not isinstance(note.get("reason"), str) for note in notes):
            return False
        bad_dates = [datetime.strptime(note.get("date", ""), "%Y-%m-%d").date() for note in notes]
        last_bad = max(bad_dates) if bad_dates else None
        if available.get("excluded_through") != (last_bad.isoformat() if last_bad is not None else None):
            return False
        if (not first <= start <= end or any(date > end for date in bad_dates) or
                (last_bad is None and start != first) or
                (last_bad is not None and (start < max(first, last_bad) or (start - max(first, last_bad)).days > 10))):
            return False
        days = (end - start).days
        if (available["annualized"] != (days >= 365.2425) or
                available["observations"] > days + 1):
            return False
    except (TypeError, ValueError, KeyError):
        return False
    raw = available.get("value")
    if raw is None:
        return True
    rate = finite_number(raw)
    return rate is not None and rate > -100 and days > 0 and available["observations"] >= 2


def _valid_period_returns(value: Any, ticker: str, stamp: str, *,
                          basis: str) -> dict[str, Any] | None:
    """Shared date/period checks; callers specify the observed return basis."""
    if not isinstance(value, dict) or value.get("version") != RETURN_VERSION or value.get("basis") != basis:
        return None
    if value.get("source_status") not in {"live", "previous"}:
        return None
    expected_url = f"https://finance.yahoo.com/quote/{quote(ticker, safe='')}/history/"
    if value.get("source_url") != expected_url:
        return None
    try:
        now = datetime.fromisoformat(stamp)
        retrieved = datetime.fromisoformat(value.get("retrieved_at", ""))
        end = datetime.strptime(value.get("as_of", ""), "%Y-%m-%d").date()
        first = datetime.strptime(value.get("since", ""), "%Y-%m-%d").date()
        if retrieved.tzinfo is None or now.tzinfo is None or retrieved > now or end > retrieved.astimezone(TOKYO).date() or first >= end:
            return None
    except (TypeError, ValueError):
        return None
    periods = value.get("periods")
    if not isinstance(periods, dict) or set(periods) != set(RETURN_PERIODS):
        return None
    has_value = False
    for key, period in periods.items():
        if not isinstance(period, dict) or type(period.get("annualized")) is not bool or period.get("end") != value["as_of"]:
            return None
        raw = period.get("value")
        if raw is None:
            if not isinstance(period.get("reason"), str):
                return None
            continue
        number = finite_number(raw)
        if number is None or number <= -100:
            return None
        try:
            start = datetime.strptime(period.get("start", ""), "%Y-%m-%d").date()
        except (TypeError, ValueError):
            return None
        if not first <= start < end:
            return None
        if key in {"3y", "5y", "10y"} and not period["annualized"]:
            return None
        if key in {"1d", "1w", "1m", "6m", "ytd", "1y"} and period["annualized"]:
            return None
        if key == "all" and period["annualized"] != ((end - first).days >= 365.2425):
            return None
        has_value = True
    if "available_history" in value and not valid_available_history(value["available_history"], value, basis=basis):
        return None
    return deepcopy(value) if has_value else None


def valid_period_returns(value: Any, ticker: str, stamp: str) -> dict[str, Any] | None:
    """Accept only this documented total-return schema, including cached rows."""
    return _valid_period_returns(value, ticker, stamp, basis="total_return")


def valid_price_returns(value: Any, ticker: str, stamp: str) -> dict[str, Any] | None:
    """A price series cannot be accepted as reinvested total returns or vice versa."""
    if (not isinstance(value, dict) or value.get("type") != "price_return" or
            value.get("method") != PRICE_RETURN_METHOD):
        return None
    return _valid_period_returns(value, ticker, stamp, basis="price_return")


def apply_period_returns(stock: dict[str, Any], previous: dict[str, Any], payload: dict[str, Any], stamp: str) -> None:
    fresh = valid_period_returns(payload.get("returns"), stock["ticker"], stamp)
    prior = valid_period_returns(previous.get("returns"), stock["ticker"], stamp)
    rejection = payload.get("returns_error") or (
        "Abgerufene Gesamtrendite-Antwort hat die Prüfung von Struktur, Datums-/Periodenangaben oder Zahlenwerten nicht bestanden; erneuter Abruf erforderlich."
        if "returns" in payload and fresh is None else None)
    if fresh and (not prior or fresh["as_of"] >= prior["as_of"]):
        fresh["source_status"] = "live"
        stock["returns"] = fresh
    elif prior:
        prior["source_status"] = "previous"
        prior["refresh_note"] = rejection or "Kein neuerer bestätigter Gesamtrendite-Datenstand; geprüfter Vorwert mit ursprünglichem Stichtag."
        stock["returns"] = prior
    else:
        stock["returns"] = unavailable_period_returns(stock["ticker"], rejection or "Gesamtrenditehistorie noch nicht verfügbar")


def apply_reserve_price_returns(stock: dict[str, Any], previous: dict[str, Any],
                                payload: dict[str, Any], stamp: str) -> None:
    fresh = valid_price_returns(payload.get("price_returns"), stock["ticker"], stamp)
    prior = valid_price_returns(previous.get("price_returns"), stock["ticker"], stamp)
    if fresh and (not prior or fresh["as_of"] >= prior["as_of"]):
        chosen = fresh
        chosen["source_status"] = "live"
    elif prior:
        chosen = prior
        chosen["source_status"] = "previous"
        chosen["refresh_note"] = payload.get("price_returns_error") or "Kein neuerer bestätigter Kursrendite-Datenstand; geprüfter Vorwert mit ursprünglichem Stichtag."
    else:
        chosen = unavailable_period_returns(stock["ticker"], payload.get("price_returns_error") or "Kursrenditehistorie noch nicht verfügbar")
        chosen.update(basis="price_return", type="price_return", method=PRICE_RETURN_METHOD)
    stock["price_returns"] = chosen
    stock["field_sources"]["price_returns"] = chosen["source_status"]
    stock["field_updated_at"]["price_returns"] = chosen["retrieved_at"]


def valid_tsm_fx(value: Any, stamp: str) -> dict[str, Any] | None:
    """The Yahoo TWD=X pair is TWD per USD, never the inverse rate."""
    if not isinstance(value, dict) or value.get("base_currency") != "USD" or value.get("quote_currency") != "TWD" or value.get("source_url") != TSM_FX_SOURCE:
        return None
    rate = finite_number(value.get("rate"))
    if rate is None or rate <= 0:
        return None
    try:
        checked = datetime.fromisoformat(stamp)
        retrieved = datetime.fromisoformat(value.get("retrieved_at", ""))
        as_of = datetime.fromisoformat(value.get("as_of", ""))
        if any(item.tzinfo is None for item in (checked, retrieved, as_of)) or not as_of <= retrieved <= checked:
            return None
        if not 0 <= (checked.astimezone(timezone.utc).date() - as_of.astimezone(timezone.utc).date()).days <= 5:
            return None
    except (ValueError, TypeError):
        return None
    return {**value, "rate": rate}


def valid_tsm_ps_metadata(value: Any, ratio: Any, stamp: str) -> dict[str, Any] | None:
    """Only a reproducible, currency-correct prior P/S may be reused."""
    if not isinstance(value, dict) or value.get("provider") != "Yahoo Finance" or value.get("validation_version") != TSM_PS_VERSION or value.get("source_url") != TSM_PS_SOURCE:
        return None
    if value.get("quote_currency") != "USD" or value.get("financial_currency") != "TWD" or not value.get("method"):
        return None
    inputs = value.get("calculation_inputs")
    if not isinstance(inputs, dict):
        return None
    market_cap, revenue, rate = (finite_number(inputs.get(key)) for key in ("market_cap_usd", "total_revenue_twd", "usd_twd_rate"))
    ratio = finite_number(ratio)
    if any(item is None or item <= 0 for item in (market_cap, revenue, rate, ratio)):
        return None
    try:
        checked = datetime.fromisoformat(stamp)
        retrieved = datetime.fromisoformat(value.get("retrieved_at", ""))
        info_retrieved = datetime.fromisoformat(value.get("info_retrieved_at", ""))
        quote_as_of = datetime.fromisoformat(value.get("quote_as_of", ""))
        if any(item.tzinfo is None for item in (checked, retrieved, info_retrieved, quote_as_of)) or not quote_as_of <= info_retrieved <= retrieved <= checked:
            return None
    except (ValueError, TypeError):
        return None
    fx = dict(rate=rate, as_of=value.get("fx_as_of"), retrieved_at=value.get("fx_retrieved_at"),
              base_currency="USD", quote_currency="TWD", source_url=value.get("fx_source_url"))
    # A cached corrected observation remains usable as a labelled previous
    # value; its FX had to be current when that observation was verified.
    if not valid_tsm_fx(fx, value["retrieved_at"]) or not math.isclose(ratio, market_cap * rate / revenue, rel_tol=0, abs_tol=0.000001):
        return None
    return deepcopy(value)


def apply_tsm_ps_ratio(stock: dict[str, Any], previous: dict[str, Any], payload: dict[str, Any], stamp: str) -> None:
    """Yahoo's TSM P/S mixes USD market cap with TWD revenue; ignore it."""
    stock["ps_ratio"] = None
    stock["field_sources"]["ps_ratio"] = "unavailable"
    stock["field_updated_at"]["ps_ratio"] = None
    metadata = stock.setdefault("field_metadata", {})
    metadata["ps_ratio"] = {"unavailable_reason": "TSMC-KUV benötigt USD/TWD-Umrechnung; unbereinigtes Yahoo-KUV verworfen."}
    info = payload.get("info") or {}
    fx = valid_tsm_fx(payload.get("tsm_fx"), stamp)
    market_cap, revenue = finite_number(info.get("marketCap")), finite_number(info.get("totalRevenue"))
    fresh = None
    result = None
    if fx and info.get("currency") == "USD" and info.get("financialCurrency") == "TWD" and market_cap is not None and market_cap > 0 and revenue is not None and revenue > 0:
        try:
            quote_epoch = finite_number(info.get("regularMarketTime"))
            quote_as_of = datetime.fromtimestamp(quote_epoch, timezone.utc).isoformat(timespec="seconds") if quote_epoch else None
            result = market_cap * fx["rate"] / revenue
            fresh = dict(provider="Yahoo Finance", validation_version=TSM_PS_VERSION,
                         source_url=TSM_PS_SOURCE, quote_currency="USD", financial_currency="TWD",
                         method="Marktkapitalisierung (USD) × USD/TWD (TWD je USD) ÷ TTM-Umsatz (TWD); keine zusätzliche ADS-Umrechnung.",
                         calculation_inputs=dict(market_cap_usd=market_cap, total_revenue_twd=revenue, usd_twd_rate=fx["rate"]),
                         fx_source_url=fx["source_url"], fx_as_of=fx["as_of"], fx_retrieved_at=fx["retrieved_at"],
                         quote_as_of=quote_as_of, info_retrieved_at=payload.get("info_retrieved_at"),
                         retrieved_at=max(datetime.fromisoformat(fx["retrieved_at"]), datetime.fromisoformat(payload.get("info_retrieved_at", ""))).isoformat(timespec="seconds"))
            fresh = valid_tsm_ps_metadata(fresh, result, stamp)
        except (OverflowError, OSError, ValueError, TypeError):
            fresh = None
    if fresh:
        stock["ps_ratio"] = round(result, 6)
        stock["field_sources"]["ps_ratio"] = "live"
        stock["field_updated_at"]["ps_ratio"] = fresh["retrieved_at"]
        metadata["ps_ratio"] = fresh
        return
    prior = valid_tsm_ps_metadata(previous.get("field_metadata", {}).get("ps_ratio"), previous.get("ps_ratio"), stamp)
    if prior and previous.get("field_sources", {}).get("ps_ratio") in {"live", "previous"}:
        stock["ps_ratio"] = previous["ps_ratio"]
        stock["field_sources"]["ps_ratio"] = "previous"
        stock["field_updated_at"]["ps_ratio"] = prior["retrieved_at"]
        metadata["ps_ratio"] = prior


def _fetch_worker(ticker: str, pipe: Any, attempts: int) -> None:
    """Separate process gives even Ticker.info a hard overall time limit."""
    try:
        import yfinance as yf
        info = {}
        # Each endpoint also has its own bounded retry, in addition to watchdog.
        for endpoint in ("info", "history"):
            for attempt in range(attempts):
                try:
                    security = yf.Ticker(ticker)
                    if endpoint == "info":
                        info = security.info
                        if not isinstance(info, dict) or not any(info.get(key) is not None for key in ("trailingPE", "forwardPE", "currentPrice", "regularMarketPrice")):
                            raise ValueError("Leere oder unvollständige Yahoo-Stammdaten")
                        pipe.send(dict(info=info, info_retrieved_at=iso_now()))
                    else:
                        # Yahoo Close is split-adjusted, without Adj Close's
                        # dividend adjustment; it matches the current quote.
                        is_etf = ticker in ETF_TICKERS
                        history = security.history(period="1y", interval="1d", auto_adjust=False, actions=is_etf, keepna=ticker == "D05.SI", timeout=12)
                        history, price_corrections = apply_history_corrections(
                            history, ticker, currency=info.get("currency"),
                            isin="SG1L01001701" if ticker == "D05.SI" else None,
                            auto_adjust=False, retrieved_at=iso_now())
                        technicals, notes = calculate_technicals(history)
                        if price_corrections:
                            technicals["price_corrections"] = price_corrections
                        if is_etf:
                            distributions, distribution_notes = calculate_distributions(history)
                            technicals.update(distributions)
                            notes.extend(distribution_notes)
                        pipe.send(dict(technicals=technicals, history_retrieved_at=iso_now(), warnings=notes))
                    break
                except Exception as exc:
                    if attempt + 1 == attempts:
                        pipe.send(dict(warnings=[f"{endpoint}: {type(exc).__name__}: {str(exc)[:180]}"]))
                    else:
                        time.sleep(min(2 ** attempt, 4))
            if endpoint == "info" and ticker == TOKIO_TICKER:
                try:
                    pipe.send(dict(dividend_forecast=fetch_tokio_dividend_forecast()))
                except Exception as exc:
                    pipe.send(dict(warnings=[f"Tokio Marine IR: {type(exc).__name__}: {str(exc)[:180]}"]))
        if ticker == TOKIO_TICKER:
            # Quotes and raw-Close technicals are sent before the slower issuer
            # reports, so the overall watchdog cannot discard those successes.
            try:
                metrics, notes = fetch_tokio_issuer_metrics()
                pipe.send(dict(issuer_metrics=metrics, warnings=notes))
            except Exception as exc:
                pipe.send(dict(warnings=[f"Tokio Marine IFRS: {type(exc).__name__}: {str(exc)[:180]}"]))
        if ticker in {"NET", "D05.SI"}:
            # Recheck issuer disclosures in every full daily batch. A loss is
            # an economic N/A, while DBS' disclosed cumulative ROE is not TTM.
            try:
                if ticker == "NET":
                    from issuer_net import fetch_net_earnings
                    pipe.send(dict(net_earnings=fetch_net_earnings()))
                else:
                    from issuer_dbs import fetch_dbs_roe
                    pipe.send(dict(dbs_roe=fetch_dbs_roe()))
            except Exception as exc:
                pipe.send(dict(warnings=[f"{ticker} Emittentenbericht: {type(exc).__name__}: {str(exc)[:150]}"]))
        # Optional long-term history comes last, preserving the fundamentals
        # already sent even if this endpoint reaches the overall watchdog.
        try:
            security = yf.Ticker(ticker)
            # Keep provider rows with missing prices. Otherwise yfinance can
            # silently drop an incomplete *previous* session once today's bar
            # arrives, making an older return history appear freshly complete.
            # calculate_period_returns must see the gap and reject it until
            # the provider supplies that session's actual closing price.
            long_history = security.history(period="max", interval="1d", auto_adjust=False, actions=True, keepna=True, timeout=12)
            metadata = security.get_history_metadata()
            return_info = {**(metadata if isinstance(metadata, dict) else {}), **info}
            long_history, price_corrections = apply_history_corrections(
                long_history, ticker, currency=return_info.get("currency"),
                isin="SG1L01001701" if ticker == "D05.SI" else None,
                auto_adjust=False, retrieved_at=iso_now())
            if ticker in RESERVE_TICKERS:
                try:
                    pipe.send(calculate_reserve_history_payload(long_history, ticker, return_info, iso_now()))
                except Exception as exc:
                    detail = f"Reserve-Historienbasis: {type(exc).__name__}: {str(exc)[:180]}"
                    pipe.send(dict(dividend_growth_error=detail, returns_error=detail, price_returns_error=detail))
            else:
                issuer_cash = fetch_jepq_return_cash() if ticker == "JEPQ" else None
                try:
                    pipe.send(dict(dividend_growth=calculate_dividend_growth(long_history, ticker, return_info, iso_now(), issuer_cash)))
                except Exception as exc:
                    pipe.send(dict(dividend_growth_error=f"Dividendenwachstum: {type(exc).__name__}: {str(exc)[:180]}"))
                returns = calculate_period_returns(long_history, ticker, return_info, iso_now(), issuer_cash)
                if price_corrections:
                    returns["price_corrections"] = price_corrections
                    returns["available_history"]["price_corrections"] = deepcopy(price_corrections)
                    returns["method"] += (
                        " DBS: fehlender Yahoo-Schlusskurs vom 23.11.2026 durch den belegten "
                        "SGX-Schlusskurs von 65,76 SGD ersetzt; fünf benachbarte Kurse bestätigen "
                        "die gleiche Aktienbasis. Quelle: " + price_corrections[0]["source_url"])
                pipe.send(dict(returns=returns))
        except Exception as exc:
            pipe.send(dict(returns_error=f"Gesamtrendite-Historie: {type(exc).__name__}: {str(exc)[:180]}"))
        if ticker == TSM_TICKER:
            # Keep quote, technicals and total returns even if this optional
            # currency endpoint reaches its own or the overall time limit.
            try:
                fx_security = yf.Ticker("TWD=X")
                fx_history = fx_security.history(period="5d", interval="1d", auto_adjust=False, actions=False, timeout=12)
                fx_close = fx_history["Close"].dropna()
                fx_metadata = fx_security.get_history_metadata()
                if fx_close.empty or (isinstance(fx_metadata, dict) and fx_metadata.get("currency", "TWD") != "TWD"):
                    raise ValueError("Kein USD/TWD-Kurs in der erwarteten Währung")
                pipe.send(dict(tsm_fx=dict(rate=finite_number(fx_close.iloc[-1]), as_of=fx_close.index[-1].isoformat(),
                                           retrieved_at=iso_now(), base_currency="USD", quote_currency="TWD", source_url=TSM_FX_SOURCE)))
            except Exception as exc:
                pipe.send(dict(warnings=[f"TSMC USD/TWD: {type(exc).__name__}: {str(exc)[:180]}"]))
        pipe.send(dict(done=True))
    except Exception as exc:
        pipe.send(dict(warnings=[f"Abruf: {type(exc).__name__}: {str(exc)[:180]}"]))
    finally:
        pipe.close()


def fetch_ticker(ticker: str, timeout: float = 45.0, attempts: int = 2) -> dict[str, Any]:
    context = mp.get_context("spawn")
    parent_pipe, child_pipe = context.Pipe(duplex=False)
    worker = context.Process(target=_fetch_worker, args=(ticker, child_pipe, attempts), daemon=True)
    result: dict[str, Any] = dict(warnings=[])
    worker.start()
    child_pipe.close()
    deadline = time.monotonic() + timeout
    completed = False
    try:
        while time.monotonic() < deadline:
            if parent_pipe.poll(min(0.25, max(0, deadline - time.monotonic()))):
                try:
                    message = parent_pipe.recv()
                except EOFError:
                    break
                result["warnings"].extend(message.pop("warnings", []))
                completed = message.pop("done", False)
                result.update(message)
                if completed:
                    break
            elif not worker.is_alive():
                break
        if not completed and worker.is_alive():
            result["warnings"].append(f"Ticker-Zeitlimit von {timeout:g} Sekunden erreicht; bereits geladene Felder bleiben nutzbar.")
    finally:
        if worker.is_alive():
            worker.terminate()
        worker.join(timeout=3)
        if worker.is_alive():
            worker.kill()
            worker.join(timeout=1)
        parent_pipe.close()
    return result


def _fallback(stock: dict[str, Any], previous: dict[str, Any], field: str) -> None:
    previous_value = previous.get(field)
    previous_source = previous.get("field_sources", {}).get(field)
    if field == "currency":
        good = isinstance(previous_value, str) and bool(previous_value.strip())
    else:
        previous_value = finite_number(previous_value)
        good = previous_value is not None
    if good and previous_source in {"live", "previous"}:
        stock[field] = previous_value
        stock["field_sources"][field] = "previous"
        stock["field_updated_at"][field] = previous.get("field_updated_at", {}).get(field)


def enrich_reserve_stock(stock: dict[str, Any], payload: dict[str, Any], stamp: str) -> None:
    """Derive price ratios from explicitly dated issuer facts, never buy signals."""
    info = payload.get("info") or {}
    price = finite_number(stock.get("price"))
    quoted_currency = stock.get("currency")
    expected_currency = stock.get("quote_currency")
    stock["vendor_metrics"] = dict(
        provider="Yahoo Finance", source_url=f"https://finance.yahoo.com/quote/{quote(stock['ticker'], safe='')}/key-statistics/",
        retrieved_at=payload.get("info_retrieved_at"), quote_currency=quoted_currency,
        financial_currency=info.get("financialCurrency"),
        trailing_pe=finite_number(info.get("trailingPE")), forward_pe=finite_number(info.get("forwardPE")),
        trailing_eps=finite_number(info.get("trailingEps")), forward_eps=finite_number(info.get("forwardEps")),
        dividend_yield=finite_number(info.get("dividendYield")), dividend_rate=finite_number(info.get("dividendRate")),
        note="Unveränderte Anbieterwerte. financialCurrency ist die Abschlusswährung und beweist allein keine EPS-/Quotierungswährung.")
    snapshot = stock.get("issuer_snapshot") or {}
    current = stock.get("current_primary_ttm") or {}
    expected_period = current.get("period_end") or snapshot.get("as_of")
    vendor_period = None
    try:
        epoch = finite_number(info.get("mostRecentQuarter"))
        if epoch:
            vendor_period = datetime.fromtimestamp(epoch, timezone.utc).date().isoformat()
        verified = datetime.strptime(snapshot.get("verified_at", ""), "%Y-%m-%d").date()
        age = (datetime.fromisoformat(stamp).date() - verified).days
    except (ValueError, TypeError, OverflowError, OSError):
        age = None
    matched = (quoted_currency == expected_currency and expected_period is not None and
               vendor_period == expected_period and age is not None and 0 <= age <= 180)
    snapshot["status"] = "matched_latest_provider_quarter" if matched else "review_required"
    snapshot["provider_latest_quarter"] = vendor_period
    snapshot["freshness_note"] = ("Berichtsperiode stimmt mit dem Kursanbieter überein; Emittentenwerte behalten ihren ausgewiesenen Berichts-/Prüfzeitpunkt."
                                  if matched else "Datierter Emittenten-Snapshot; neueste Periode oder Basis nicht mehr bestätigt. Vor aktuellen Bewertungssignalen erneut prüfen.")
    stock["issuer_snapshot"] = snapshot
    if current and matched and price is not None and price > 0:
        eps = finite_number(current.get("diluted_eps"))
        if current.get("currency") == quoted_currency and eps is not None and eps > 0:
            stock["current_pe"] = price / eps
            stock["field_sources"]["current_pe"] = "live" if stock["field_sources"].get("price") == "live" else "previous"
            stock["field_updated_at"]["current_pe"] = stock["field_updated_at"].get("price")
            stock.setdefault("field_metadata", {})["current_pe"] = dict(
                provider="Emittenten-IR + aktueller Kurs", label=current.get("label", "IR-TTM rekonstruiert (ca.)"),
                source_url=current["source_urls"][-1], source_urls=current["source_urls"],
                currency=quoted_currency, share_basis=stock.get("share_basis"),
                diluted_eps=eps, report_period_end=current["period_end"], verified_at=snapshot.get("verified_at"),
                method=current.get("formula", current.get("formula_eps", "")),
                rounding_caveat=current.get("rounding_caveat", current.get("caveat", "")),
                note="Aktueller Kurs geteilt durch datiert rekonstruierte, basisgleiche TTM-EPS. Kein Emittenten-Ausweis eines exakten Vierquartals-EPS; gerundete Perioden-EPS und variierende Durchschnittsaktien können abweichen.")
    stock.setdefault("field_metadata", {})["forward_pe"] = dict(
        provider="Yahoo Finance / Analystenschätzung", source_url=f"https://finance.yahoo.com/quote/{quote(stock['ticker'], safe='')}/analysis/",
        quote_currency=quoted_currency, financial_currency=info.get("financialCurrency"),
        share_basis=stock.get("share_basis"), forward_eps_vendor=finite_number(info.get("forwardEps")),
        note=("Anbieter-Schätzgröße; bei TotalEnergies ist die implizite EPS-Währungsumrechnung nicht unabhängig nachgebaut. Keine pauschale Spot-FX-Korrektur und kein automatisches positives Signal."
              if stock["ticker"] == "TTE.PA" else "Analysten-EPS-Schätzung des Anbieters auf der jeweiligen gelisteten Aktienbasis, keine Emittentengarantie."))
    metrics = snapshot.get("metrics") or {}
    sector = dict(price_as_of=stock.get("as_of"), issuer_period_end=expected_period,
                  issuer_snapshot_status=snapshot["status"], currency=quoted_currency,
                  note="Kursabhängige Quotienten auf datiertem Emittenten-Snapshot; keine Nachkauf-Bedingung wird automatisch positiv markiert.")
    if price is not None and price > 0 and quoted_currency == expected_currency:
        nav = finite_number(metrics.get("nav_per_share"))
        if nav is not None and nav > 0:
            sector["p_nav"] = price / nav
            sector["nav_per_share"] = nav
            sector["nav_method"] = "Aktueller USD-Kurs / zuletzt recherchierter NAV je derselben Common Share; NAV-Datum gesondert ausgewiesen."
        affo = finite_number(current.get("diluted_affo_per_share"))
        if affo is not None and affo > 0:
            sector["p_affo_ttm"] = price / affo
        guidance = metrics.get("fy2026_affo_guidance", {}).get("value") if isinstance(metrics.get("fy2026_affo_guidance"), dict) else None
        if isinstance(guidance, list) and len(guidance) == 2 and all(finite_number(value) is not None and value > 0 for value in guidance):
            sector["forward_p_affo_fy2026_midpoint"] = price / ((guidance[0] + guidance[1]) / 2)
            sector["affo_guidance"] = deepcopy(metrics["fy2026_affo_guidance"])
        reference = stock.get("regular_dividend_reference") or {}
        annual = finite_number(reference.get("value"))
        if reference.get("currency") == quoted_currency and annual is not None and annual >= 0:
            sector["regular_dividend_reference_yield_pct"] = annual / price * 100
            sector["regular_dividend_reference"] = deepcopy(reference)
    stock["sector_valuation"] = sector


def valid_researched_metric(value: Any, ticker: str, field: str, stamp: str,
                            info: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Accept only dated, period-specific observations from these two issuers."""
    if not isinstance(value, dict) or value.get("validation_version") != 1:
        return None
    expected = {("NET", "current_pe"): ("gaap_ttm_earnings", "USD"),
                ("D05.SI", "roe"): ("issuer_reported_roe", "SGD")}.get((ticker, field))
    if not expected or (value.get("fact_type"), value.get("currency")) != expected:
        return None
    allowed = {"www.dbs.com"} if ticker == "D05.SI" else {"www.sec.gov", "sec.gov", "data.sec.gov", "cloudflare.net", "www.cloudflare.net", "www.cloudflare.com"}
    urls = value.get("source_urls") or [value.get("source_url")]
    if not isinstance(urls, list) or not urls or value.get("source_url") not in urls:
        return None
    for url in urls:
        try:
            parsed = urlparse(url) if isinstance(url, str) else None
            safe = parsed and parsed.scheme == "https" and parsed.hostname in allowed and not parsed.username and not parsed.password and parsed.port in (None, 443)
        except ValueError:
            safe = False
        if not safe:
            return None
    now, retrieved = _batch_instant(stamp), _batch_instant(value.get("retrieved_at"))
    try:
        start, end, report = [datetime.strptime(value.get(key, ""), "%Y-%m-%d").date()
                              for key in ("period_start", "period_end", "report_date")]
    except (ValueError, TypeError):
        return None
    if not now or not retrieved or retrieved > now or not start < end <= report <= retrieved.date() <= now.date() or not 0 <= (now.date() - end).days <= 185:
        return None
    if not value.get("provider") or not value.get("accounting_basis") or not value.get("method") or not value.get("share_basis"):
        return None
    if info:
        quarter = finite_number(info.get("mostRecentQuarter"))
        if quarter:
            try:
                if datetime.fromtimestamp(quarter, timezone.utc).date() > end:
                    return None
            except (ValueError, OSError, OverflowError):
                return None
    if field == "roe":
        number = finite_number(value.get("value"))
        if number is None or not -1000 <= number <= 1000 or value.get("annualized") is not True:
            return None
    else:
        if finite_number(value.get("ttm_net_income_usd")) is None or not 360 <= (end - start).days <= 366:
            return None
    return deepcopy(value)


def confirmed_nonpositive_pe(stock: dict[str, Any], stamp: str | None = None) -> bool:
    """A sourced loss is resolved, never a numeric valuation or a buy signal."""
    if stock.get("ticker") != "NET" or stock.get("current_pe") is not None or stock.get("currency") != "USD":
        return False
    meta = stock.get("field_metadata", {}).get("current_pe")
    checked = valid_researched_metric(meta, "NET", "current_pe", stamp or iso_now())
    return bool(checked and checked["ttm_net_income_usd"] <= 0 and meta.get("not_applicable") is True
                and stock.get("field_sources", {}).get("current_pe") == "not_applicable"
                and stock.get("field_updated_at", {}).get("current_pe") == meta.get("retrieved_at"))


def apply_researched_metrics(stock: dict[str, Any], previous: dict[str, Any],
                             payload: dict[str, Any], stamp: str) -> None:
    """Prefer fresh issuer evidence; retain original dates if its fetch fails."""
    ticker, info = stock["ticker"], payload.get("info") or {}
    if ticker not in {"NET", "D05.SI"}:
        return
    metadata = stock.setdefault("field_metadata", {})
    field = "current_pe" if ticker == "NET" else "roe"
    candidate = payload.get("net_earnings" if ticker == "NET" else "dbs_roe")
    fresh = valid_researched_metric(candidate, ticker, field, stamp, info)
    if fresh and fresh["currency"] != stock.get("currency"):
        fresh = None
    if ticker == "D05.SI":
        if fresh:
            stock["roe"] = fresh["value"]
            stock["field_sources"]["roe"] = "live"
            stock["field_updated_at"]["roe"] = fresh["retrieved_at"]
            metadata["roe"] = fresh
        elif stock["field_sources"].get("roe") == "previous":
            prior = valid_researched_metric(previous.get("field_metadata", {}).get("roe"), ticker, field, stamp)
            if prior and stock.get("roe") == prior["value"]:
                metadata["roe"] = prior
        return

    # An absent/negative vendor multiple must never revive the original 260x.
    raw = finite_number(stock.get("current_pe"))
    if raw is not None and (raw <= 0 or stock["field_sources"].get(field) == "initial"):
        stock[field] = None
        stock["field_sources"][field] = "unavailable"
        stock["field_updated_at"][field] = None
    if fresh and fresh["ttm_net_income_usd"] <= 0:
        fresh.update(label="P/E", not_applicable=True,
                     not_applicable_reason="Kein sinnvolles P/E bei negativem oder fehlendem GAAP-Gewinn.")
        stock[field] = None
        stock["field_sources"][field] = "not_applicable"
        stock["field_updated_at"][field] = fresh["retrieved_at"]
        metadata[field] = fresh
    elif not fresh:
        prior = valid_researched_metric(previous.get("field_metadata", {}).get(field), ticker, field, stamp, info)
        trailing_eps = finite_number(info.get("trailingEps"))
        if prior and prior.get("not_applicable") is True and prior["ttm_net_income_usd"] <= 0 and not (trailing_eps is not None and trailing_eps > 0):
            stock[field] = None
            stock["field_sources"][field] = "not_applicable"
            stock["field_updated_at"][field] = prior["retrieved_at"]
            metadata[field] = prior


def merge_stock(seed: dict[str, Any], previous: dict[str, Any], payload: dict[str, Any], portfolio_id: str, stamp: str) -> dict[str, Any]:
    stock = {**deepcopy(seed), "field_sources": dict(seed["field_sources"]), "field_updated_at": dict(seed["field_updated_at"]), "warnings": list(payload.get("warnings", []))}
    info = payload.get("info") or {}
    technicals = payload.get("technicals") or {}
    quote_price = next((value for key in ("currentPrice", "regularMarketPrice") if (value := finite_number(info.get(key))) is not None and value > 0), None)
    history_price = finite_number(technicals.get("history_price"))
    price = quote_price or history_price
    is_etf = seed.get("asset_type") == "etf"
    is_tokio = seed.get("ticker") == TOKIO_TICKER
    is_tsm = seed.get("ticker") == TSM_TICKER
    if is_tsm:
        stock["technicals_as_of"] = technicals.get("history_as_of")
    if is_tokio or is_tsm:
        stock["field_metadata"] = {field: dict(value) for field, value in seed.get("field_metadata", {}).items()}
    dividend, dividend_warnings = (None, []) if is_etf or is_tokio else normalize_dividend_yield(info, price)
    stock["warnings"].extend(dividend_warnings)
    roe = finite_number(info.get("returnOnEquity"))
    values = dict(current_pe=finite_number(info.get("trailingPE")), forward_pe=finite_number(info.get("forwardPE")),
                  ps_ratio=finite_number(info.get("priceToSalesTrailing12Months")), div_yield=dividend,
                  roe=finite_number(roe * 100) if roe is not None else None, price=price,
                  sma200=finite_number(technicals.get("sma200")), rsi14=finite_number(technicals.get("rsi14")),
                  currency=info.get("currency") if isinstance(info.get("currency"), str) and info["currency"].strip() else None)
    fields = (*NUMERIC_FIELDS, "currency")
    if is_etf:
        # Funds have no company earnings forecast, ROE or sales valuation.
        for field in ("forward_pe", "ps_ratio", "roe", "div_yield"):
            stock[field] = values[field] = None
            stock["field_sources"][field] = "unavailable"
            stock["field_updated_at"][field] = None
        if values["current_pe"] is not None and values["current_pe"] <= 0:
            values["current_pe"] = None
        distributions = finite_number(technicals.get("distributions_per_share_ttm"))
        if distributions is not None and distributions < 0:
            distributions = None
        values["distributions_per_share_ttm"] = distributions
        values["distribution_yield_ttm"] = finite_number(distributions / price * 100) if distributions is not None and price and price > 0 else None
        fields = (*fields, *ETF_NUMERIC_FIELDS)
    for field in fields:
        if is_etf and field in {"forward_pe", "ps_ratio", "roe", "div_yield"}:
            continue
        if is_tokio and field in {"current_pe", "forward_pe", "ps_ratio", "roe", "div_yield"}:
            continue
        if is_tsm and field == "ps_ratio":
            continue
        value = values[field]
        if field in {"price", "sma200"} and value is not None and value <= 0:
            value = None
        if field in {"ps_ratio", "div_yield"} and value is not None and value < 0:
            value = None
        if field == "rsi14" and value is not None and not 0 <= value <= 100:
            value = None
        if value is None:
            _fallback(stock, previous, field)
        else:
            stock[field] = round(value, 6) if field != "currency" else value
            stock["field_sources"][field] = "live"
            history_field = field in {"sma200", "rsi14", *ETF_NUMERIC_FIELDS} or (field == "price" and quote_price is None)
            stock["field_updated_at"][field] = payload.get("history_retrieved_at" if history_field else "info_retrieved_at") or stamp
    if is_tokio:
        apply_tokio_issuer_metrics(stock, previous, payload, price, values["currency"], stamp)
        apply_tokio_dividend(stock, previous, payload, price, values["currency"], stamp)
    if is_tsm:
        apply_tsm_ps_ratio(stock, previous, payload, iso_now())
    if is_etf:
        if stock.get("current_pe") is not None and stock["current_pe"] <= 0:
            stock["current_pe"] = None
            stock["field_sources"]["current_pe"] = "unavailable"
            stock["field_updated_at"]["current_pe"] = None
        stock["distribution_as_of"] = technicals.get("distribution_as_of") if stock["field_sources"]["distribution_yield_ttm"] == "live" else previous.get("distribution_as_of")
    quote_epoch = finite_number(info.get("regularMarketTime"))
    if quote_price and quote_epoch:
        try:
            stock["as_of"] = datetime.fromtimestamp(quote_epoch, timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            stock["as_of"] = payload.get("info_retrieved_at") or stamp
    elif history_price is not None:
        stock["as_of"] = technicals.get("history_as_of")
    elif stock["field_sources"].get("price") == "previous":
        stock["as_of"] = previous.get("as_of")
    else:
        stock["as_of"] = None
    apply_researched_metrics(stock, previous, payload, iso_now())
    if technicals.get("price_corrections"):
        for field in ("sma200", "rsi14"):
            if stock["field_sources"].get(field) == "live":
                stock.setdefault("field_metadata", {})[field] = dict(
                    label="Yahoo-Tageskurse mit belegter DBS-Schlusskurskorrektur",
                    currency=stock.get("currency"), history_as_of=technicals.get("history_as_of"),
                    retrieved_at=payload.get("history_retrieved_at"),
                    source_url=f"https://finance.yahoo.com/quote/{quote(stock['ticker'], safe='')}/history/",
                    price_corrections=deepcopy(technicals["price_corrections"]))
    for field in ("sma200", "rsi14"):
        prior_metadata = previous.get("field_metadata", {}).get(field, {})
        if stock["field_sources"].get(field) == "previous" and prior_metadata.get("price_corrections"):
            stock.setdefault("field_metadata", {})[field] = deepcopy(prior_metadata)
    if portfolio_id == 'classic':
        apply_classic_annual_roe(stock, previous)
    if portfolio_id == 'universe':
        enrich_reserve_stock(stock, payload, stamp)
        apply_dividend_growth(stock, previous, payload)
        growth = stock["dividend_growth"]
        cash_yield = finite_number(stock.get("dividend_annual_yield_pct"))
        # Reserve securities use actual trailing cash, including supplements,
        # consistently with Outlook. Keep vendor annualizations separately.
        stock["div_yield"] = cash_yield
        yield_live = growth.get("source_status") == "live" and stock["field_sources"].get("price") == "live"
        stock["field_sources"]["div_yield"] = "live" if cash_yield is not None and yield_live else "previous" if cash_yield is not None else "unavailable"
        stock["field_updated_at"]["div_yield"] = growth.get("retrieved_at") if cash_yield is not None else None
        stock.setdefault("field_metadata", {})["div_yield"] = dict(
            label="Tatsächliche Barausschüttungen TTM / aktueller Kurs",
            source_url=f"https://finance.yahoo.com/quote/{quote(stock['ticker'], safe='')}/history/",
            currency=stock.get("currency"), share_basis=stock.get("share_basis"),
            cash_dividends_ttm=stock.get("dividend_annual"), price=stock.get("price"),
            price_as_of=stock.get("as_of"), dividends_as_of=growth.get("end"),
            dividends_retrieved_at=growth.get("retrieved_at"),
            method="Summe tatsächlicher Barausschüttungen der letzten zwölf Monate am Ex-Tag / Kurs × 100. Sonder-/Zusatzdividenden enthalten; kein garantierter künftiger Ertrag. Reguläre Dividendenreferenz separat.")
    required = ["price", "sma200", "rsi14", "currency", "distribution_yield_ttm"] if is_etf else ["current_pe", "forward_pe", "price", "sma200", "rsi14", "currency", *( ["ps_ratio"] if portfolio_id == "tech" else ["div_yield", "roe"])]
    sources = [stock["field_sources"][field] for field in required]
    def resolved(field: str) -> bool:
        if stock["field_sources"][field] == "live":
            return True
        verified = _batch_instant(stock["field_updated_at"].get(field))
        started = _batch_instant(stamp)
        return field == "current_pe" and confirmed_nonpositive_pe(stock) and verified is not None and started is not None and verified >= started
    if all(resolved(field) for field in required):
        stock["source_status"] = "live"
    elif "live" in sources:
        stock["source_status"] = "partial"
    elif "previous" in sources:
        stock["source_status"] = "stale"
    else:
        stock["source_status"] = "initial"
    fallback_fields = [field for field in required if not resolved(field)]
    if fallback_fields:
        stock["warnings"].append("Nicht frisch verfügbar (Quelle je Feld gekennzeichnet): " + ", ".join(fallback_fields) + ".")
    if stock["field_sources"]["price"] != stock["field_sources"]["sma200"] and stock["price"] and stock["sma200"]:
        stock["warnings"].append("SMA-Abstand kombiniert unterschiedlich aktuelle Kurs-/SMA-Daten; Feldzeitpunkte beachten.")
    # The run stamp is its start, while this optional endpoint arrives later.
    # Validate against the actual merge time without changing its source stamp.
    apply_period_returns(stock, previous, payload, iso_now())
    if portfolio_id != 'universe':
        apply_dividend_growth(stock, previous, payload)
    if portfolio_id == 'universe':
        apply_reserve_price_returns(stock, previous, payload, iso_now())
        stock["simulation_ready"] = (finite_number(stock.get("price")) is not None and stock["price"] > 0
                                     and stock.get("currency") in {"USD", "EUR"}
                                     and valid_dividend_growth(stock.get("dividend_growth"), stock["ticker"], stock["currency"]) is not None
                                     and valid_period_returns(stock.get("returns"), stock["ticker"], iso_now()) is not None
                                     and valid_price_returns(stock.get("price_returns"), stock["ticker"], iso_now()) is not None)
        stock["simulation_readiness_note"] = "Daten für künftige Szenarien vorbereitet; keine Aufnahme in ein Depot und kein Kaufsignal."
    add_derived(stock, portfolio_id)
    return stock


def _batch_instant(value: Any) -> datetime | None:
    """Read a provenance timestamp without treating a missing timezone as fresh."""
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return instant.astimezone(timezone.utc) if instant.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def static_refresh_fields(stock: dict[str, Any], portfolio_id: str) -> list[str]:
    """A validated, explicitly labelled annual ROE is not a daily TTM feed."""
    if portfolio_id != "classic":
        return []
    ref, meta = stock.get("annual_roe_reference"), stock.get("field_metadata", {}).get("roe")
    if not isinstance(ref, dict) or not isinstance(meta, dict) or ref.get("reference_version") != 1 or meta.get("reference_version") != 1:
        return []
    if ref.get("currency") != stock.get("currency") or meta.get("currency") != stock.get("currency") or meta.get("as_of") != ref.get("as_of"):
        return []
    inputs = ref.get("calculation_inputs") or {}
    income, opening, closing, value, actual = [finite_number(item) for item in (
        inputs.get("owner_net_income_eur_million"), inputs.get("owner_equity_open_eur_million"),
        inputs.get("owner_equity_close_eur_million"), ref.get("value"), stock.get("roe"))]
    if None in (income, opening, closing, value, actual) or opening + closing <= 0:
        return []
    if not math.isclose(value, income / ((opening + closing) / 2) * 100, rel_tol=0, abs_tol=0.000001) or not math.isclose(value, actual, rel_tol=0, abs_tol=0.000001):
        return []
    return ["roe"]


def required_refresh_fields(stock: dict[str, Any], portfolio_id: str) -> list[str]:
    """Dynamic inputs used by this security's shopping/outlook views."""
    if stock.get("asset_type") == "etf":
        fields = ["price", "sma200", "rsi14", "currency", "distribution_yield_ttm"]
    else:
        fields = ["current_pe", "forward_pe", "price", "sma200", "rsi14", "currency",
                  *(["ps_ratio"] if portfolio_id == "tech" else ["div_yield", "roe"])]
    static = static_refresh_fields(stock, portfolio_id)
    return [*[field for field in fields if field not in static], "returns", "dividend_growth", *(["price_returns"] if portfolio_id == "universe" else [])]


def recent_return_price_gap(value: Any) -> bool:
    """A recent bad close cannot be resolved by one still-valid daily return.

    Anchor the ten-day check to the observed history end, not the wall clock.
    Explicit pre-IPO gaps and old, labelled historical discontinuities remain
    distinguishable from a recent price error invalidating observed periods.
    """
    if not isinstance(value, dict):
        return False
    notes, periods = value.get("quality_notes", []), value.get("periods", {})
    if not isinstance(notes, list) or not isinstance(periods, dict):
        return False
    try:
        end = datetime.strptime(value.get("as_of", ""), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    for note in notes:
        if not isinstance(note, dict) or note.get("reason") != "Ungültiger Schlusskurs im Zeitraum":
            continue
        try:
            bad = datetime.strptime(note.get("date", ""), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if not 0 <= (end - bad).days <= 10:
            continue
        for period in periods.values():
            if (not isinstance(period, dict) or period.get("value") is not None or
                    period.get("reason") != note["reason"]):
                continue
            try:
                start = datetime.strptime(period.get("start", ""), "%Y-%m-%d").date()
                period_end = datetime.strptime(period.get("end", ""), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if period_end == end and start < bad <= period_end:
                return True
    return False


def finalize_refresh_batch(result: dict[str, Any], started_at: str,
                           completed_at: str | None = None) -> None:
    """Assess one published snapshot; never overwrite any source's timestamps.

    A live value must have actually been retrieved during this run. Valid old
    financial research/reference periods remain untouched and are not required
    to change each day. Closed-market observations may be up to four days old.
    """
    completed_at = completed_at or iso_now()
    started, completed = _batch_instant(started_at), _batch_instant(completed_at)
    if started is None or completed is None or completed < started:
        raise ValueError("Ungültiger Zeitraum des gemeinsamen Datenabrufs")

    def retrieved_in_batch(value: Any) -> bool:
        instant = _batch_instant(value)
        return instant is not None and started <= instant <= completed

    counts: Counter[str] = Counter()
    groups = [*result.get("portfolios", []), dict(id="universe", stocks=result.get("security_universe", {}).get("stocks", []))]
    for group in groups:
        for stock in group.get("stocks", []):
            required = required_refresh_fields(stock, group["id"])
            missing, any_fresh = [], False
            for field in required:
                if field in {"returns", "dividend_growth", "price_returns"}:
                    item = stock.get(field) or {}
                    fresh = item.get("source_status") == "live" and retrieved_in_batch(item.get("retrieved_at"))
                    if field in {"returns", "price_returns"} and recent_return_price_gap(item):
                        fresh = False
                else:
                    value = stock.get(field)
                    valid = isinstance(value, str) and bool(value.strip()) if field == "currency" else finite_number(value) is not None
                    fresh = valid and stock.get("field_sources", {}).get(field) == "live" and retrieved_in_batch(stock.get("field_updated_at", {}).get(field))
                    if field == "current_pe" and confirmed_nonpositive_pe(stock, completed_at):
                        fresh = retrieved_in_batch(stock.get("field_updated_at", {}).get(field))
                any_fresh = any_fresh or fresh
                if not fresh:
                    missing.append(field)
            quote_at = _batch_instant(stock.get("as_of"))
            if quote_at is None or not -300 <= (completed - quote_at).total_seconds() <= FEAR_GREED_MAX_AGE:
                missing.append("quote_as_of")
            status = "live" if not missing else "partial" if any_fresh else "stale" if stock.get("source_status") != "initial" else "initial"
            stock["refresh"] = dict(version=1, batch_id=started_at, status=status,
                                    required_fields=required, missing_fields=missing,
                                    not_applicable_fields=["current_pe"] if confirmed_nonpositive_pe(stock, completed_at) else [],
                                    static_fields=static_refresh_fields(stock, group["id"]))
            counts[status] += 1

    cnn, fx = result.get("fear_and_greed") or {}, result.get("fx_rates") or {}
    cnn_status = cnn.get("source_status", "unavailable")
    if cnn_status == "live" and not retrieved_in_batch(cnn.get("retrieved_at")):
        cnn_status = "previous"
    if cnn_status == "live" and any(fear_greed_score((cnn.get("comparisons") or {}).get(key, {}).get("score")) is None for key in FEAR_GREED_COMPARISONS):
        cnn_status = "partial"
    fx_status = fx.get("status", "unavailable")
    if fx_status == "live" and not retrieved_in_batch(fx.get("updated_at")):
        fx_status = "previous"
    total = sum(counts.values())
    yahoo_status = "live" if total and counts["live"] == total else "partial" if counts["live"] or counts["partial"] else "unavailable"
    statuses = [yahoo_status, cnn_status, fx_status]
    result["refresh_batch"] = dict(
        version=1, id=started_at, started_at=started_at, completed_at=completed_at,
        status="live" if all(status == "live" for status in statuses) else "partial" if any(status in {"live", "partial"} for status in statuses) else "stale",
        stock_counts=dict(counts),
        providers=dict(
            yahoo=dict(status=yahoo_status, stock_count=total, complete_stock_count=counts["live"]),
            cnn=dict(status=cnn_status, as_of=cnn.get("as_of"), retrieved_at=cnn.get("retrieved_at")),
            ecb=dict(status=fx_status, as_of=fx.get("as_of"), retrieved_at=fx.get("updated_at"))))


def build_live_data(previous: dict[str, Any] | None = None,
                    fetcher: Callable[[str], dict[str, Any]] = fetch_ticker,
                    now: datetime | None = None,
                    fear_greed_fetcher: Callable[[], dict[str, Any]] | None = None,
                    fx_fetcher: Callable[[], dict[str, Any]] | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    result = build_seed_data(now)
    result["warnings"] = [REFERENCE_WARNING]
    result["last_attempted_at"] = iso_now(now)
    previous = previous or {}
    old_stocks = {(group.get('id'), stock['ticker']): stock
                  for group in previous.get('portfolios', []) for stock in group.get('stocks', [])
                  if isinstance(stock, dict) and 'ticker' in stock}
    prior_universe = previous.get('security_universe') or {}
    old_stocks.update({('universe', stock['ticker']): stock
                       for stock in prior_universe.get('stocks', [])
                       if isinstance(stock, dict) and 'ticker' in stock})
    groups = [*result['portfolios'], dict(id='universe', stocks=result['security_universe']['stocks'])]
    seeds = {(group['id'], stock['ticker']): deepcopy(stock)
             for group in groups for stock in group['stocks']}
    unique_tickers = list(dict.fromkeys(stock['ticker'] for group in groups for stock in group['stocks']))
    # One bounded request pipeline per distinct security. Four workers keep a
    # larger watchlist within the workflow timeout; overlaps reuse one payload.
    with ThreadPoolExecutor(max_workers=min(4, len(unique_tickers))) as executor:
        pending = {ticker: executor.submit(fetcher, ticker) for ticker in unique_tickers}
        for group in groups:
            for index, seed in enumerate(group['stocks']):
                print(f"[{group['id']}] {seed['ticker']}: lade Daten …", flush=True)
                prior = old_stocks.get((group['id'], seed['ticker']), {})
                try:
                    payload = pending[seed['ticker']].result()
                    if not isinstance(payload, dict):
                        raise ValueError('Ungültige Abrufantwort')
                    merged = merge_stock(seed, prior, payload, group['id'], result['last_attempted_at'])
                except Exception as exc:
                    merged = merge_stock(seed, prior, dict(warnings=[f"Ticker-Fehler: {type(exc).__name__}: {str(exc)[:180]}"]), group['id'], result['last_attempted_at'])
                group['stocks'][index] = merged
                print(f"  {merged['source_status']}: {merged['tranche']}", flush=True)
    repairs = repair_incomplete_stocks(result, seeds=seeds, previous=old_stocks,
                                       fetcher=fetcher, merge_stock=merge_stock,
                                       finalize_refresh_batch=finalize_refresh_batch,
                                       started_at=result["last_attempted_at"])
    print("Refresh repair check: " + json.dumps(repairs, ensure_ascii=False), flush=True)
    stocks = [stock for group in result["portfolios"] for stock in group["stocks"]]
    counts = Counter(stock["source_status"] for stock in stocks)
    result["status_counts"] = dict(counts)
    has_live = any("live" in stock["field_sources"].values() for stock in stocks)
    if counts["live"] == EXPECTED_STOCK_COUNT:
        result["data_mode"] = "live"
    elif has_live:
        result["data_mode"] = "partial"
        result["warnings"].append("Der Abruf ist teilweise erfolgreich; Fallbacks und fehlende Felder sind je Aktie ausgewiesen.")
    else:
        result["data_mode"] = "stale"
        result["warnings"].append("Keine neuen Yahoo-Finance-Daten; bisherige Werte bzw. Startwerte bleiben mit ursprünglicher Herkunft erhalten.")
        result["last_updated"] = previous.get("last_updated", result["last_updated"])
        result["updated_at"] = previous.get("updated_at", result["updated_at"])
    universe = result['security_universe']
    universe['last_attempted_at'] = result['last_attempted_at']
    reserve_counts = Counter(stock['source_status'] for stock in universe['stocks'])
    universe['status_counts'] = dict(reserve_counts)
    reserve_live = any('live' in stock['field_sources'].values() for stock in universe['stocks'])
    if reserve_counts['live'] == universe['expected_stock_count']:
        universe['data_mode'] = 'live'
    elif reserve_live:
        universe['data_mode'] = 'partial'
    else:
        universe['data_mode'] = 'stale'
        universe['updated_at'] = prior_universe.get('updated_at', universe['updated_at'])
    result["fear_and_greed"] = refresh_fear_and_greed(previous.get("fear_and_greed"), fear_greed_fetcher)
    result["fx_rates"] = refresh_ecb_fx(previous.get("fx_rates"), fx_fetcher)
    finalize_refresh_batch(result, result["last_attempted_at"])
    if result["data_mode"] != "stale":
        result["data_mode"] = result["refresh_batch"]["status"]
    return result


def load_previous(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Ungültige JSON-Zahl: {value}")))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(payload.get("portfolios"), list):
        raise ValueError("Vorhandene data.json hat kein unterstütztes Schema; Datei wird nicht überschrieben")
    return payload


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary_path = Path(handle.name)
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--seed", action="store_true", help="Nur die vorgegebenen Startwerte schreiben; kein Netzwerk")
    mode.add_argument("--sentiment-only", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().with_name("data.json"), help="Ziel-JSON; standardmäßig neben diesem Skript")
    parser.add_argument("--ticker-timeout", type=float, default=45.0, help="Gesamtzeitlimit pro Ticker in Sekunden (max. 45)")
    parser.add_argument("--attempts", type=int, default=2, choices=(1, 2, 3), help="Abrufversuche je Endpoint")
    args = parser.parse_args(argv)
    if not 0 < args.ticker_timeout <= 45:
        parser.error("--ticker-timeout muss größer 0 und höchstens 45 sein")
    if args.sentiment_only:
        parser.error("Separate CNN-Veröffentlichungen sind deaktiviert; bitte den gemeinsamen Vollabruf ohne --sentiment-only starten")
    try:
        if args.seed:
            result = build_seed_data()
        else:
            # Fail early on missing dependencies, rather than failing per holding.
            import pandas  # noqa: F401
            import pytz  # noqa: F401
            import yfinance  # noqa: F401
            import pypdf  # noqa: F401
            previous = load_previous(args.output)
            result = build_live_data(previous, fetcher=lambda ticker: fetch_ticker(ticker, args.ticker_timeout, args.attempts))
        atomic_write_json(args.output, result)
    except (OSError, ValueError, ImportError) as exc:
        print(f"Update fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    print(f"{EXPECTED_STOCK_COUNT} Werte → {args.output} | Modus: {result['data_mode']} | {result.get('status_counts', {'initial': EXPECTED_STOCK_COUNT})}")
    # Publish honest partial data, but keep the scheduled repair path open.
    # A successful process must mean every required stock/provider input was
    # verified, not merely that at least one Yahoo request returned a value.
    return 1 if not args.seed and result.get("refresh_batch", {}).get("status") != "live" else 0


if __name__ == "__main__":
    mp.freeze_support()
    raise SystemExit(main())
