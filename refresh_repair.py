"""Bounded repairs for incomplete public market-data snapshots.

The caller supplies its existing fetch/merge/finalize functions. Fetches must
already have an overall limit of 45 seconds or less. The budget is checked
before each wave, so only its last in-flight wave can finish after the budget.
No provider credentials or private portfolio settings are read here.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timezone
import math
import time
from typing import Any, Callable, Mapping


def _records(value: dict[str, Any]) -> list[tuple[str, str, list, int]]:
    groups = list(value.get("portfolios", []))
    universe = value.get("security_universe") or {}
    groups.append({"id": "universe", "stocks": universe.get("stocks", [])})
    records = []
    for group in groups:
        group_id = group["id"]
        stocks = group.get("stocks", [])
        for index, stock in enumerate(stocks):
            records.append((group_id, stock["ticker"], stocks, index))
    return records


def _missing(stock: dict[str, Any]) -> set[str]:
    refresh = stock.get("refresh")
    if not isinstance(refresh, dict):
        return {"refresh_metadata"}
    fields = refresh.get("missing_fields")
    if not isinstance(fields, list) or any(not isinstance(field, str) or not field for field in fields):
        return {"refresh_metadata"}
    if not fields and refresh.get("status") != "live":
        return {"refresh_status"}
    return set(fields)


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def _day(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _no_regression(old: dict[str, Any], candidate: dict[str, Any], missing: set[str]) -> bool:
    old_quote, new_quote = _instant(old.get("as_of")), _instant(candidate.get("as_of"))
    if old_quote is not None and (new_quote is None or new_quote < old_quote):
        return False
    for field, key in (("returns", "as_of"), ("dividend_growth", "end"), ("price_returns", "as_of")):
        prior = old.get(field)
        if field in missing or not isinstance(prior, dict) or prior.get("source_status") != "live":
            continue
        original_day = _day(prior.get(key))
        item = candidate.get(field)
        new_day = _day(item.get(key)) if isinstance(item, dict) else None
        if original_day is not None and (new_day is None or new_day < original_day):
            return False
    return True


def repair_incomplete_stocks(
    result: dict[str, Any], *, seeds: Mapping[tuple[str, str], dict[str, Any]],
    previous: Mapping[tuple[str, str], dict[str, Any]], fetcher: Callable,
    merge_stock: Callable, finalize_refresh_batch: Callable, started_at: str,
    max_rounds: int = 2, budget_seconds: float = 180, workers: int = 4,
    clock: Callable[[], float] = time.monotonic, sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Mutate only improving complete records; return public repair metadata.

    ``seeds`` and ``previous`` use ``(group_id, ticker)`` keys. ``merge_stock``
    receives ``(seed, original_previous, new_payload, group_id, started_at)``.
    The finalizer receives ``(snapshot, started_at)`` and supplies each stock's
    ``refresh.status`` / ``refresh.missing_fields``. Assessment uses copies, so
    failed or nonimproving repairs preserve the existing record exactly.

    At most two rounds, four concurrent distinct tickers and 180 budget seconds
    are permitted. A candidate must remove at least one missing field without
    introducing another or rolling back previously fresh quote/history dates.
    Each accepted candidate is one coherent re-merge, never patched field by
    field with new timestamps attached to old observations.
    """
    if type(max_rounds) is not int or max_rounds < 0 or type(workers) is not int or workers < 1:
        raise ValueError("Invalid repair round/worker limit")
    if isinstance(budget_seconds, bool) or not isinstance(budget_seconds, (int, float)) or not math.isfinite(budget_seconds) or budget_seconds < 0:
        raise ValueError("Invalid repair time budget")
    max_rounds, workers = min(max_rounds, 2), min(workers, 4)
    deadline = clock() + min(budget_seconds, 180)
    assessed = deepcopy(result)
    finalize_refresh_batch(assessed, started_at)
    records, observed = _records(result), _records(assessed)
    if len(records) != len(observed) or any(left[:2] != right[:2] for left, right in zip(records, observed)):
        raise ValueError("Refresh assessment changed the security list")
    missing = {index: _missing(stocks[position]) for index, (_, _, stocks, position) in enumerate(observed)}
    initial_tickers = list(dict.fromkeys(ticker for index, (_, ticker, _, _) in enumerate(records) if missing[index]))
    rounds, attempts = 0, 0

    for round_index in range(max_rounds):
        tickers = list(dict.fromkeys(ticker for index, (group_id, ticker, _, _) in enumerate(records)
                                    if missing[index] and (group_id, ticker) in seeds))
        if not tickers or clock() >= deadline:
            break
        started_round = False
        for offset in range(0, len(tickers), workers):
            if clock() >= deadline:
                break
            wave = tickers[offset:offset + workers]
            if not started_round:
                rounds += 1
                started_round = True
            with ThreadPoolExecutor(max_workers=len(wave)) as executor:
                futures = {ticker: executor.submit(fetcher, ticker) for ticker in wave}
                attempts += len(wave)
                for ticker in wave:
                    try:
                        payload = futures[ticker].result()
                    except Exception:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    for index, (group_id, record_ticker, stocks, position) in enumerate(records):
                        key = (group_id, ticker)
                        if record_ticker != ticker or not missing[index] or key not in seeds:
                            continue
                        current = stocks[position]
                        try:
                            candidate = merge_stock(deepcopy(seeds[key]), deepcopy(previous.get(key, {})),
                                                    deepcopy(payload), group_id, started_at)
                            if not isinstance(candidate, dict) or candidate.get("ticker") != ticker:
                                continue
                            tiny = ({"portfolios": [], "security_universe": {"stocks": [candidate]}}
                                    if group_id == "universe" else
                                    {"portfolios": [{"id": group_id, "stocks": [candidate]}]})
                            finalize_refresh_batch(tiny, started_at)
                            candidate_missing = _missing(candidate)
                            if not candidate_missing < missing[index] or not _no_regression(current, candidate, missing[index]):
                                continue
                        except Exception:
                            continue
                        stocks[position] = candidate
                        missing[index] = candidate_missing
        if round_index + 1 < max_rounds and any(missing.values()):
            remaining = deadline - clock()
            if remaining > 0:
                sleeper(min(2.0, remaining))

    unresolved: dict[str, set[str]] = {}
    for index, (_, ticker, _, _) in enumerate(records):
        if missing[index]:
            unresolved.setdefault(ticker, set()).update(missing[index])
    metadata = dict(version=1, rounds=rounds, attempts=attempts,
                    recovered_tickers=[ticker for ticker in initial_tickers if ticker not in unresolved],
                    unresolved=[dict(ticker=ticker, fields=sorted(fields)) for ticker, fields in unresolved.items()])
    result["refresh_repairs"] = metadata
    return metadata
