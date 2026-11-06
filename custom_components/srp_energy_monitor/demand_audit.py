"""Pure comparisons of SRP's daily and interval demand; no bill or device writes."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .srp_api import _is_srp_on_peak_day

PHOENIX = ZoneInfo("America/Phoenix")
SOURCE = "SRP dailyDemandList versus demandList; both are utility portal series"
CAVEATS = [
    "A match compares related utility reports, not an independent meter or final bill.",
    "Only the displayed completed-day range has interval coverage checked.",
    "Cycle means are derived under the E-15 calendar; historical plan identity is unverified.",
    "Actual billed demand charges and invoice dollars are not retrieved.",
]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except ValueError:
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _stamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=PHOENIX) if parsed.tzinfo is None else parsed.astimezone(PHOENIX)


def _day(value: Any) -> date | None:
    parsed = _stamp(value)
    return parsed.date() if parsed else None


def peak_slots(day: date) -> list[datetime]:
    """Expected START-labelled E-15 half hours, in Phoenix local time."""
    if not _is_srp_on_peak_day(day):
        return []
    hours = range(14, 20) if 5 <= day.month <= 10 else [*range(5, 9), *range(17, 21)]
    return [datetime.combine(day, time(hour, minute), PHOENIX) for hour in hours for minute in (0, 30)]


def _daily_index(payload: dict) -> tuple[dict[date, float], dict[date, list[str]]]:
    values: dict[date, float] = {}
    issues: dict[date, list[str]] = {}
    for row in payload.get("dailyDemandList") or []:
        if not isinstance(row, dict):
            continue
        day = _day(row.get("date") or row.get("day"))
        if day is None:
            continue
        value = _number(row.get("onPeakDemand"))
        if value is None:
            issues.setdefault(day, []).append("invalid_daily_maximum")
        elif day in values and values[day] != value:
            issues.setdefault(day, []).append("conflicting_daily_maxima")
        else:
            values[day] = value
        if any(row[key] != "kW" for key in ("unit", "unitOfMeasurement", "unit_of_measurement") if key in row):
            issues.setdefault(day, []).append("wrong_daily_demand_unit")
    return values, issues


def _cycle_means(payload: dict, daily: dict, issues: dict, yesterday: date) -> list[dict]:
    cycles = []
    for row in payload.get("highDemandInfoList") or []:
        if not isinstance(row, dict):
            continue
        start, end = _day(row.get("billStartDate")), _day(row.get("billEndDate"))
        if start is None or end is None or start > end or (end - start).days > 62:
            continue
        through = min(end, yesterday)
        eligible = []
        cursor = start
        while cursor <= through:
            if _is_srp_on_peak_day(cursor):
                eligible.append(cursor)
            cursor += timedelta(days=1)
        present = [day for day in eligible if day in daily and not issues.get(day)]
        complete = bool(eligible) and len(present) == len(eligible)
        cycles.append({
            "cycle_start": start.isoformat(), "cycle_end": end.isoformat(),
            "data_through": through.isoformat(), "eligible_days": len(eligible),
            "present_days": len(present), "daily_coverage_complete": complete,
            "derived_average_kw": round(sum(daily[day] for day in present) / len(present), 2)
            if complete else None,
            "cycle_maximum_kw": _number(row.get("onPeakHighDemand")),
            "plan_history_verified": False, "billed_cost_verified": False,
        })
    return sorted(cycles, key=lambda item: item["cycle_start"], reverse=True)[:36]


def build_demand_audit(
    daily_payload: dict,
    raw_rows: list[dict],
    start: date,
    end: date,
    now: datetime,
    *,
    plan_code: str | None,
    timestamp_start_verified: bool = False,
    fetch_complete: bool = True,
    fetch_issue: str | None = None,
) -> dict[str, Any]:
    """Audit at most seven completed days using already fetched raw demand rows.

    Plan identity must be bound separately: isAverageDemandRate also covers E-16.
    Missing evidence is incomplete, never a zero maximum or a verified bill cost.
    """
    local_now = now.astimezone(PHOENIX)
    yesterday = local_now.date() - timedelta(days=1)
    requested_end = end
    end = min(end, yesterday)
    start = max(start, end - timedelta(days=6))
    result: dict[str, Any] = {
        "state": "incomplete", "last_checked": local_now.isoformat(),
        "latest_complete_day": None, "latest_daily_max_kw": None,
        "latest_interval_max_kw": None, "latest_complete_day_winning_intervals": [],
        "days": [], "events": [], "billed_cost_verified": False,
        "source": SOURCE, "caveats": CAVEATS.copy(), "plan_code": plan_code,
        "timestamp_role": "start", "timestamp_start_verified": timestamp_start_verified,
        "interval_minutes": 30, "unit": "kW", "tolerance_kw": 0.05,
        "audited_start": start.isoformat(), "audited_end": end.isoformat(),
        "fetch_complete": fetch_complete, "issues": [], "derived_cycle_means": [],
        "binding_source": "configured E15; account enrollment not independently confirmed"
        if plan_code == "E15" else "no supported E15 configuration binding",
    }
    if plan_code != "E15" or daily_payload.get("isAverageDemandRate") is not True:
        result["state"] = "unsupported"
        result["issues"].append("E15_plan_contract_not_verified")
    else:
        if not timestamp_start_verified:
            result["issues"].append("interval_timestamp_start_role_unverified")
        if not fetch_complete:
            result["issues"].append(fetch_issue or "interval_fetch_incomplete")
        if requested_end > yesterday:
            result["issues"].append("requested_unsettled_day")
        if start > end:
            result["issues"].append("no_completed_day_range")
        daily, daily_issues = _daily_index(daily_payload)
        if any(not isinstance(row, dict) or _day(row.get("date") or row.get("day")) is None
               for row in daily_payload.get("dailyDemandList") or []):
            result["issues"].append("invalid_daily_row_timestamp")
        slots: dict[datetime, float] = {}
        row_issues: dict[date, list[str]] = {}
        normalized_rows = []
        for row in raw_rows:
            if not isinstance(row, dict):
                result["issues"].append("invalid_interval_row")
                continue
            stamp = _stamp(row.get("date") or row.get("hour"))
            if stamp is None:
                result["issues"].append("invalid_interval_timestamp")
                continue
            day = stamp.date()
            if not start <= day <= end:
                continue
            normalized_rows.append({
                "start": stamp.isoformat(), "hour": str(row.get("hour", "")),
                "interval_minutes": _number(row.get("intervalTimeInMinutes")),
                "demand_kw": _number(row.get("onPeakDemand")),
                "units": [str(row[key]) for key in ("unit", "unitOfMeasurement", "unit_of_measurement") if key in row],
            })
            expected = peak_slots(day)
            value = _number(row.get("onPeakDemand"))
            if stamp not in expected:
                if value not in (None, 0) or (expected and stamp.hour in {s.hour for s in expected}):
                    row_issues.setdefault(day, []).append("unexpected_on_peak_slot")
                continue
            problems = row_issues.setdefault(day, [])
            hour = _stamp(row.get("hour")) if row.get("hour") else stamp
            if hour != stamp:
                problems.append("conflicting_interval_timestamps")
            if isinstance(row.get("intervalTimeInMinutes"), bool) or row.get("intervalTimeInMinutes") != 30:
                problems.append("wrong_interval_width")
            units = [row[key] for key in ("unit", "unitOfMeasurement", "unit_of_measurement") if key in row]
            if any(unit != "kW" for unit in units):
                problems.append("wrong_demand_unit")
            if value is None:
                problems.append("invalid_interval_demand")
            elif stamp in slots and slots[stamp] != value:
                problems.append("conflicting_interval_duplicates")
            else:
                slots[stamp] = value
        cursor = start
        while cursor <= end:
            expected = peak_slots(cursor)
            present = [stamp for stamp in expected if stamp in slots]
            issues = sorted(set(row_issues.get(cursor, []) + daily_issues.get(cursor, [])))
            daily_max = daily.get(cursor) if not daily_issues.get(cursor) else None
            interval_max = max((slots[stamp] for stamp in present), default=None) if not row_issues.get(cursor) else None
            all_winners = [{"start": stamp.isoformat(), "end": (stamp + timedelta(minutes=30)).isoformat(),
                        "demand_kw": slots[stamp]}
                       for stamp in present if slots[stamp] == interval_max]
            winners = all_winners[:1]  # Earliest tied maximum; bounded HA attributes.
            if not expected and not issues:
                status = "not_applicable"
            else:
                if len(present) != len(expected):
                    issues.append("missing_peak_slots")
                if daily_max is None:
                    issues.append("missing_daily_maximum")
                status = "incomplete" if issues or not timestamp_start_verified or not fetch_complete else (
                    "matches" if abs(daily_max - interval_max) <= 0.05 + 1e-9 else "mismatch"
                )
            day_result = {
                "day": cursor.isoformat(), "status": status, "expected_slots": len(expected),
                "present_slots": len(present), "daily_max_kw": daily_max,
                "interval_max_kw": interval_max, "winning_intervals": winners,
                "winning_interval_ties": len(all_winners),
                "issues": sorted(set(issues)),
            }
            result["days"].append(day_result)
            if status in ("matches", "mismatch"):
                result.update(latest_complete_day=cursor.isoformat(), latest_daily_max_kw=daily_max,
                              latest_interval_max_kw=interval_max, latest_complete_day_winning_intervals=winners)
                result["events"].extend({"day": cursor.isoformat(), **winner, "status": status} for winner in winners)
            cursor += timedelta(days=1)
        result["derived_cycle_means"] = _cycle_means(daily_payload, daily, daily_issues, yesterday)
        # Include every retained value, not just maxima: a non-winning slot
        # revision must still change the source checksum. Response order and
        # identical duplicate rows do not change normalized evidence.
        result["interval_content_hash"] = hashlib.sha256(
            json.dumps(sorted({json.dumps(row, sort_keys=True) for row in normalized_rows})).encode()
        ).hexdigest()
        result["daily_content_hash"] = hashlib.sha256(json.dumps([
            [day.isoformat(), daily.get(day) if not daily_issues.get(day) else None,
             sorted(set(daily_issues.get(day, [])))]
            for day in sorted(daily.keys() | daily_issues.keys())
        ], separators=(",", ":")).encode()).hexdigest()
        states = {item["status"] for item in result["days"]}
        result["state"] = "incomplete" if result["issues"] or "incomplete" in states or not states else (
            "mismatch" if "mismatch" in states else "matches"
        )
    result["issues"] = sorted(set(result["issues"]))
    # Observation time is not evidence content. Equivalent reordered responses
    # and an unchanged refresh produce the same digest for incident deduplication.
    content = {key: value for key, value in result.items() if key != "last_checked"}
    result["content_hash"] = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return result
