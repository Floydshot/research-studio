"""Decide whether a shared market-data refresh is due in Europe/Berlin.

Manual workflow dispatches without scheduler inputs deliberately bypass the
daily deduplication guard. This module reads public market data only.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import re
from typing import Any
from zoneinfo import ZoneInfo


BERLIN = ZoneInfo("Europe/Berlin")
CLOCK_SKEW = timedelta(minutes=5)
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")


@dataclass(frozen=True)
class RefreshDecision:
    refresh: bool
    reason: str
    scheduler_day: str


def instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except (ValueError, OverflowError):
        return None


def refresh_is_current(payload: Any, due: datetime, now: datetime) -> bool:
    """Complete only after every security and all shared providers are current."""
    batch = payload.get("refresh_batch") if isinstance(payload, dict) else None
    if not isinstance(batch, dict) or batch.get("version") != 1:
        return False
    started, completed = instant(batch.get("started_at")), instant(batch.get("completed_at"))
    if started is None or completed is None or batch.get("id") != batch.get("started_at"):
        return False
    if not due <= started <= completed <= now + CLOCK_SKEW:
        return False
    if batch.get("status") != "live":
        return False
    providers = batch.get("providers")
    if not isinstance(providers, dict):
        return False
    yahoo = providers.get("yahoo")
    if not isinstance(yahoo, dict) or yahoo.get("status") != "live":
        return False
    count, complete = yahoo.get("stock_count"), yahoo.get("complete_stock_count")
    if (type(count) is not int or not 1 <= count <= 2**53 - 1 or
            type(complete) is not int or complete != count):
        return False
    counts = batch.get("stock_counts")
    if (not isinstance(counts, dict) or type(counts.get("live")) is not int or
            counts["live"] != count or any(
                status not in {"live", "partial", "stale", "initial"} or
                type(value) is not int or value < 0 or value > 2**53 - 1 or
                (status != "live" and value != 0)
                for status, value in counts.items())):
        return False
    for name in ("cnn", "ecb"):
        provider = providers.get(name)
        if not isinstance(provider, dict) or provider.get("status") != "live":
            return False
        retrieved = instant(provider.get("retrieved_at"))
        if retrieved is None or not started <= retrieved <= completed:
            return False
    return True


def decide_refresh(event_name: str, payload: Any, *, scheduler_day: str = "",
                   request_id: str = "", now: datetime | None = None) -> RefreshDecision:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Current time must include a timezone")
    today = now.astimezone(BERLIN).date()
    day = today.isoformat()
    if event_name == "push":
        return RefreshDecision(False, "deploy_only_push", day)
    if event_name == "workflow_dispatch" and not scheduler_day and not request_id:
        return RefreshDecision(True, "manual_force", day)
    if event_name not in {"schedule", "workflow_dispatch"}:
        raise ValueError("Unsupported workflow event")
    if event_name == "workflow_dispatch":
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", scheduler_day) or not REQUEST_ID.fullmatch(request_id):
            raise ValueError("Scheduler dispatch requires a valid day and request ID")
        try:
            requested_day = date.fromisoformat(scheduler_day)
        except ValueError as error:
            raise ValueError("Invalid scheduler day") from error
        if requested_day != today:
            return RefreshDecision(False, "different_berlin_day", day)
    due = datetime.combine(today, time(7, 30), BERLIN)
    if now < due:
        return RefreshDecision(False, "before_0730_berlin", day)
    if refresh_is_current(payload, due, now):
        return RefreshDecision(False, "batch_already_current", day)
    return RefreshDecision(True, "daily_refresh_due", day)


def read_public_data(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (OSError, ValueError):
        # Missing/corrupt data must cause a new refresh rather than a false skip.
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--scheduler-day", default="")
    parser.add_argument("--request-id", default="")
    parser.add_argument("--data", type=Path, default=Path("data.json"))
    args = parser.parse_args()
    try:
        result = decide_refresh(args.event_name, read_public_data(args.data),
                                scheduler_day=args.scheduler_day, request_id=args.request_id)
    except ValueError as error:
        parser.error(str(error))
    output = {"refresh": str(result.refresh).lower(), "reason": result.reason,
              "scheduler_day": result.scheduler_day}
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            for key, value in output.items():
                handle.write(f"{key}={value}\n")
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
