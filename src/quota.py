"""Local tracking of YouTube Data API quota.

Google gives each project 10,000 units a day and resets the counter at midnight
**Pacific Time**, not at your local midnight and not at UTC midnight. Getting
that wrong is the classic way to think you have a fresh budget when you don't,
so every date decision in here goes through the Pacific clock.

State is a small JSON file in the user's home directory. The server is spawned
fresh per session by the MCP client, so the count has to outlive the process.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")

DEFAULT_STATE_PATH = Path.home() / ".rozetta" / "quota.json"


def pacific_now() -> datetime:
    return datetime.now(PACIFIC)


class QuotaTracker:
    DAILY_BUDGET = 10_000

    # - Costs straight from Google's published quota table.
    # - search.list is listed but never called; it's here so the cost is visible.
    COSTS = {
        "videos.list": 1,
        "channels.list": 1,
        "playlistItems.list": 1,
        "search.list": 100,
    }

    def __init__(self, state_path: Path | str | None = None) -> None:
        if state_path is None:
            state_path = os.environ.get("ROZETTA_QUOTA_FILE") or DEFAULT_STATE_PATH
        self.state_path = Path(state_path)

    # -- state file ---------------------------------------------------------

    def _read_state(self) -> dict:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # - A missing or corrupted file just means "no spend recorded yet".
            return {"pacific_date": self.current_pacific_date().isoformat(), "used": 0}

        if not isinstance(raw, dict):
            return {"pacific_date": self.current_pacific_date().isoformat(), "used": 0}
        return raw

    def _write_state(self, pacific_date: date, used: int) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"pacific_date": pacific_date.isoformat(), "used": used}
        self.state_path.write_text(json.dumps(payload), encoding="utf-8")

    # -- clock --------------------------------------------------------------

    def current_pacific_date(self) -> date:
        return pacific_now().date()

    def next_reset(self) -> datetime:
        """Midnight Pacific at the start of the next Pacific day."""
        tomorrow = self.current_pacific_date() + timedelta(days=1)
        return datetime.combine(tomorrow, time.min, tzinfo=PACIFIC)

    def reset_description(self) -> str:
        """Human-readable reset time in both Pacific and the machine's local zone."""
        reset = self.next_reset()
        local = reset.astimezone()
        return (
            f"{reset:%Y-%m-%d %H:%M} Pacific "
            f"({local:%Y-%m-%d %H:%M} local time)"
        )

    # -- accounting ---------------------------------------------------------

    def used_today(self) -> int:
        state = self._read_state()
        stored_date = state.get("pacific_date")
        if stored_date != self.current_pacific_date().isoformat():
            # - Stale file from a previous Pacific day: budget is fresh again.
            return 0
        used = state.get("used", 0)
        return used if isinstance(used, int) and used >= 0 else 0

    def remaining_today(self) -> int:
        return max(0, self.DAILY_BUDGET - self.used_today())

    def cost_of(self, endpoint: str) -> int:
        try:
            return self.COSTS[endpoint]
        except KeyError:
            raise KeyError(
                f"Unknown Data API endpoint {endpoint!r}; add its cost to QuotaTracker.COSTS."
            ) from None

    def would_exceed(self, endpoint: str) -> bool:
        return self.cost_of(endpoint) > self.remaining_today()

    def record(self, endpoint: str) -> None:
        cost = self.cost_of(endpoint)
        today = self.current_pacific_date()
        self._write_state(today, self.used_today() + cost)
