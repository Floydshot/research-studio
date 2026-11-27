"""Narrow, source-labelled corrections for independently observed daily closes.

The single approved exception below replaces one reported Yahoo price hole.
It is not an alternate history provider and never adds rows or cash events.
"""
from __future__ import annotations

from datetime import date, datetime
import math
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    import pandas as pd


DBS_TICKER = "D05.SI"
DBS_ISIN = "SG1L01001701"
DBS_CURRENCY = "SGD"
DBS_CLOSE_DATE = "2026-11-23"
DBS_CLOSE = 65.76
DBS_EVIDENCE_RETRIEVED_ON = "2026-11-27"
DBS_HISTORY_SOURCE = "https://www.investing.com/equities/dbs-group-holdings-historical-data"
DBS_IDENTITY_SOURCE = "https://www.investing.com/equities/dbs-group-holdings"
DBS_OVERLAP_CLOSES = (
    ("2026-11-19", 67.34),
    ("2026-11-20", 66.11),
    ("2026-11-24", 66.24),
    ("2026-11-25", 65.44),
    ("2026-11-26", 65.85),
)
DBS_QUOTE_TOLERANCE = 0.005
SINGAPORE = ZoneInfo("Asia/Singapore")


def _finite_number(value: Any) -> float | None:
    import pandas as pd

    if pd.api.types.is_bool(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def apply_history_corrections(history: pd.DataFrame, ticker: str, *,
                              currency: str | None, isin: str | None,
                              auto_adjust: bool, retrieved_at: str
                              ) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Return a copy and provenance; leave any unproven candidate unchanged.

Call only for daily history requested with ``auto_adjust=False``. Identity,
five surrounding closes and the existing missing-price session must all match
before replacing the Close cell. Naive indices are exchange session dates;
aware indices are interpreted in the listing's Asia/Singapore time zone.
"""
    import pandas as pd

    corrected = history.copy(deep=True)
    if (ticker != DBS_TICKER or currency != DBS_CURRENCY or isin != DBS_ISIN or
            auto_adjust is not False or not isinstance(history.index, pd.DatetimeIndex) or
            not history.columns.is_unique or "Close" not in history or history.empty or
            history.index.hasnans or history.index.has_duplicates):
        return corrected, []
    try:
        retrieved = datetime.fromisoformat(retrieved_at)
        if (retrieved.tzinfo is None or retrieved.utcoffset() is None or
                retrieved.astimezone(SINGAPORE).date() < date.fromisoformat(DBS_EVIDENCE_RETRIEVED_ON)):
            return corrected, []
        sessions = history.index
        if sessions.tz is not None:
            sessions = sessions.tz_convert(SINGAPORE)
        session_dates = [session.date().isoformat() for session in sessions]
    except (TypeError, ValueError, OverflowError):
        return corrected, []
    # Reject ambiguous duplicate sessions rather than silently choosing a row.
    if len(set(session_dates)) != len(session_dates) or DBS_CLOSE_DATE not in session_dates:
        return corrected, []
    positions = {session: position for position, session in enumerate(session_dates)}
    target_position = positions[DBS_CLOSE_DATE]
    close_position = history.columns.get_loc("Close")
    original = history.iloc[target_position, close_position]
    original_number = _finite_number(original)
    try:
        missing = bool(pd.isna(original))
    except (TypeError, ValueError):
        return corrected, []
    if not missing and (original_number is None or original_number > 0):
        return corrected, []
    # bool is not a missing market price even though False compares equal to 0.
    if pd.api.types.is_bool(original):
        return corrected, []
    overlaps = []
    for session, expected in DBS_OVERLAP_CLOSES:
        if session not in positions:
            return corrected, []
        observed = _finite_number(history.iloc[positions[session], close_position])
        if (observed is None or observed <= 0 or
                not math.isclose(observed, expected, rel_tol=0.0, abs_tol=DBS_QUOTE_TOLERANCE)):
            return corrected, []
        overlaps.append(dict(date=session, yahoo_close=observed, source_close=expected))
    corrected.iloc[target_position, close_position] = DBS_CLOSE
    correction = dict(
        version=1, type="observed_close_replacement", provider="Investing.com",
        ticker=DBS_TICKER, isin=DBS_ISIN, currency=DBS_CURRENCY,
        date=DBS_CLOSE_DATE, field="Close", original=original_number, value=DBS_CLOSE,
        source_url=DBS_HISTORY_SOURCE, identity_source_url=DBS_IDENTITY_SOURCE,
        source_as_of=DBS_CLOSE_DATE, evidence_retrieved_on=DBS_EVIDENCE_RETRIEVED_ON,
        applied_at=retrieved_at,
        share_basis="SGX-listed DBS Group Holdings ordinary share; no ADR or currency conversion",
        price_basis="Daily Close from auto_adjust=False history; five surrounding Yahoo closes agree",
        overlap_tolerance=DBS_QUOTE_TOLERANCE, overlap_checks=overlaps,
        reason="Approved dated Investing.com close replaces only Yahoo's missing/nonpositive 23 November 2026 Close; cash events remain unchanged.",
    )
    return corrected, [correction]
