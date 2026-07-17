#!/usr/bin/env python3
"""Generate repository-owned GitHub activity summary cards."""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GRAPHQL_URL = "https://api.github.com/graphql"
QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        weeks {
          contributionDays {
            date
            contributionCount
          }
        }
      }
    }
  }
}
"""


class ResourceLimitError(RuntimeError):
    """Raised when GitHub refuses a contribution query as too expensive."""


@dataclass(frozen=True)
class Metrics:
    total: int
    active_days: int
    longest_streak: int
    monthly: OrderedDict[str, int]


def month_start(value: date) -> date:
    return value.replace(day=1)


def shift_month(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    return date(month_index // 12, month_index % 12 + 1, 1)


def iso_datetime(value: date, end_of_day: bool = False) -> str:
    suffix = "T23:59:59Z" if end_of_day else "T00:00:00Z"
    return value.isoformat() + suffix


def query_github(username: str, token: str, start: date, end: date) -> dict[str, int]:
    payload = json.dumps(
        {
            "query": QUERY,
            "variables": {
                "login": username,
                "from": iso_datetime(start),
                "to": iso_datetime(end, end_of_day=True),
            },
        }
    ).encode("utf-8")
    request = Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "profile-summary-generator",
        },
    )

    response_data: dict | None = None
    for attempt in range(3):
        try:
            with urlopen(request, timeout=30) as response:
                response_data = json.load(response)
            break
        except HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise RuntimeError(f"GitHub API returned HTTP {error.code}") from error
        except URLError:
            pass
        if attempt == 2:
            raise RuntimeError("GitHub API remained unavailable after three attempts")
        time.sleep(2**attempt)

    assert response_data is not None
    errors = response_data.get("errors", [])
    if errors:
        messages = "; ".join(str(error.get("message", error)) for error in errors)
        if any(error.get("type") == "RESOURCE_LIMITS_EXCEEDED" for error in errors):
            raise ResourceLimitError(messages)
        raise RuntimeError(f"GitHub GraphQL error: {messages}")

    user = response_data.get("data", {}).get("user")
    if user is None:
        raise RuntimeError(f"GitHub user not found: {username}")

    days: dict[str, int] = {}
    weeks = user["contributionsCollection"]["contributionCalendar"]["weeks"]
    for week in weeks:
        for item in week["contributionDays"]:
            item_date = item["date"]
            if start.isoformat() <= item_date <= end.isoformat():
                days[item_date] = int(item["contributionCount"])
    return days


def fetch_range(username: str, token: str, start: date, end: date) -> dict[str, int]:
    try:
        return query_github(username, token, start, end)
    except ResourceLimitError:
        span = (end - start).days + 1
        if span <= 7:
            raise
        midpoint = start + timedelta(days=span // 2 - 1)
        return {
            **fetch_range(username, token, start, midpoint),
            **fetch_range(username, token, midpoint + timedelta(days=1), end),
        }


def fetch_contributions(username: str, token: str, start: date, end: date) -> dict[date, int]:
    days: dict[str, int] = {}
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=89), end)
        days.update(fetch_range(username, token, cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)

    expected = (end - start).days + 1
    if len(days) != expected:
        raise RuntimeError(f"Expected {expected} contribution days, received {len(days)}")
    return {date.fromisoformat(day): count for day, count in days.items()}


def calculate_metrics(days: dict[date, int], start: date, end: date) -> Metrics:
    total = sum(days.values())
    active_days = sum(count > 0 for count in days.values())
    longest_streak = 0
    running_streak = 0
    cursor = start
    while cursor <= end:
        if days.get(cursor, 0) > 0:
            running_streak += 1
            longest_streak = max(longest_streak, running_streak)
        else:
            running_streak = 0
        cursor += timedelta(days=1)

    monthly: OrderedDict[str, int] = OrderedDict()
    month = month_start(start)
    while month <= end:
        monthly[month.strftime("%Y-%m")] = 0
        month = shift_month(month, 1)
    for day, count in days.items():
        monthly[day.strftime("%Y-%m")] += count

    return Metrics(total, active_days, longest_streak, monthly)


def bar_chart(monthly: OrderedDict[str, int], foreground: str, muted: str, accent: str) -> str:
    values = list(monthly.values())
    maximum = max(values) if values else 1
    chart_x = 510
    baseline = 164
    bar_width = 18
    gap = 8
    maximum_height = 82
    parts: list[str] = []

    for index, (month, value) in enumerate(monthly.items()):
        height = max(3, round(maximum_height * value / maximum)) if maximum else 3
        x = chart_x + index * (bar_width + gap)
        y = baseline - height
        opacity = 0.35 + (0.65 * value / maximum if maximum else 0)
        label = datetime.strptime(month, "%Y-%m").strftime("%b")
        parts.append(
            f'<g><title>{escape(label)}: {value:,} contributions</title>'
            f'<rect x="{x}" y="{y}" width="{bar_width}" height="{height}" rx="4" '
            f'fill="{accent}" opacity="{opacity:.2f}"/></g>'
        )
        parts.append(
            f'<text x="{x + bar_width / 2:.1f}" y="184" text-anchor="middle" '
            f'fill="{muted}" font-size="10">{escape(label[0])}</text>'
        )

    parts.append(
        f'<line x1="{chart_x - 8}" y1="164.5" x2="828" y2="164.5" stroke="{foreground}" opacity="0.12"/>'
    )
    return "".join(parts)


def render_svg(username: str, metrics: Metrics, updated: date, dark: bool) -> str:
    palette = (
        {
            "background": "#0d1117",
            "border": "#30363d",
            "foreground": "#f0f6fc",
            "muted": "#8b949e",
            "accent": "#58a6ff",
        }
        if dark
        else {
            "background": "#ffffff",
            "border": "#d0d7de",
            "foreground": "#1f2328",
            "muted": "#656d76",
            "accent": "#0969da",
        }
    )
    title = f"@{username} GitHub activity for the last 12 months"
    chart = bar_chart(metrics.monthly, palette["foreground"], palette["muted"], palette["accent"])
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="860" height="210" viewBox="0 0 860 210" role="img" aria-labelledby="title desc">
  <title id="title">{escape(title)}</title>
  <desc id="desc">{metrics.total:,} contributions across {metrics.active_days} active days. Longest streak: {metrics.longest_streak} days.</desc>
  <rect x="0.5" y="0.5" width="859" height="209" rx="10" fill="{palette['background']}" stroke="{palette['border']}"/>
  <g font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif">
    <text x="32" y="38" fill="{palette['foreground']}" font-size="16" font-weight="600">GitHub activity</text>
    <text x="154" y="38" fill="{palette['muted']}" font-size="13">last 12 months</text>

    <text x="32" y="89" fill="{palette['foreground']}" font-size="29" font-weight="700">{metrics.total:,}</text>
    <text x="32" y="111" fill="{palette['muted']}" font-size="12">contributions</text>

    <text x="205" y="89" fill="{palette['foreground']}" font-size="29" font-weight="700">{metrics.active_days}</text>
    <text x="205" y="111" fill="{palette['muted']}" font-size="12">active days</text>

    <text x="344" y="89" fill="{palette['foreground']}" font-size="29" font-weight="700">{metrics.longest_streak}</text>
    <text x="344" y="111" fill="{palette['muted']}" font-size="12">longest streak</text>

    <text x="32" y="184" fill="{palette['muted']}" font-size="11">Updated {updated.strftime('%d %b %Y')} · @{escape(username)}</text>
    {chart}
  </g>
</svg>
"""


def write_cards(username: str, output: Path, token: str, today: date) -> Metrics:
    start = shift_month(month_start(today), -11)
    days = fetch_contributions(username, token, start, today)
    metrics = calculate_metrics(days, start, today)

    light_path = output / "github" / "0-profile-details.svg"
    dark_path = output / "github_dark" / "0-profile-details.svg"
    light_path.parent.mkdir(parents=True, exist_ok=True)
    dark_path.parent.mkdir(parents=True, exist_ok=True)
    light_path.write_text(render_svg(username, metrics, today, dark=False), encoding="utf-8")
    dark_path.write_text(render_svg(username, metrics, today, dark=True), encoding="utf-8")
    return metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("username")
    parser.add_argument("--output", type=Path, default=Path("profile-summary-card-output"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN is required")
    today = datetime.now(timezone.utc).date()
    metrics = write_cards(args.username, args.output, token, today)
    print(
        f"Generated profile summary: {metrics.total:,} contributions, "
        f"{metrics.active_days} active days, {metrics.longest_streak}-day longest streak"
    )


if __name__ == "__main__":
    main()
