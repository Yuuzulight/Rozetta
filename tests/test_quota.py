"""Quota accounting, including the Pacific-Time reset that's easy to get wrong."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest

from quota import PACIFIC, QuotaTracker


def test_fresh_tracker_has_full_budget(tracker):
    assert tracker.remaining_today() == 10_000
    assert tracker.used_today() == 0


def test_recording_spends_the_endpoint_cost(tracker):
    tracker.record("videos.list")
    assert tracker.used_today() == 1
    assert tracker.remaining_today() == 9_999

    tracker.record("channels.list")
    tracker.record("playlistItems.list")
    assert tracker.used_today() == 3


def test_search_list_cost_is_the_expensive_one(tracker):
    assert tracker.COSTS["search.list"] == 100
    assert tracker.cost_of("search.list") == 100


def test_unknown_endpoint_is_rejected_rather_than_costed_at_zero(tracker):
    with pytest.raises(KeyError, match="Unknown Data API endpoint"):
        tracker.cost_of("comments.list")


def test_would_exceed_is_false_with_budget_left(tracker):
    assert tracker.would_exceed("videos.list") is False
    assert tracker.would_exceed("search.list") is False


def test_would_exceed_is_true_once_budget_runs_out(tracker):
    tracker._write_state(tracker.current_pacific_date(), 10_000)
    assert tracker.remaining_today() == 0
    assert tracker.would_exceed("videos.list") is True


def test_would_exceed_accounts_for_the_cost_not_just_emptiness(tracker):
    tracker._write_state(tracker.current_pacific_date(), 9_950)
    assert tracker.remaining_today() == 50
    # - 50 units left: a 1-unit read still fits, a 100-unit search does not.
    assert tracker.would_exceed("videos.list") is False
    assert tracker.would_exceed("search.list") is True


def test_counter_resets_on_a_new_pacific_day(tracker, monkeypatch):
    day_one = date(2026, 8, 15)
    monkeypatch.setattr(QuotaTracker, "current_pacific_date", lambda self: day_one)
    tracker.record("videos.list")
    tracker.record("videos.list")
    assert tracker.used_today() == 2

    day_two = date(2026, 8, 16)
    monkeypatch.setattr(QuotaTracker, "current_pacific_date", lambda self: day_two)
    assert tracker.used_today() == 0
    assert tracker.remaining_today() == 10_000


def test_reset_is_keyed_to_pacific_not_to_local_midnight(tracker):
    reset = tracker.next_reset()
    assert reset.tzinfo is PACIFIC
    assert (reset.hour, reset.minute, reset.second) == (0, 0, 0)
    assert reset.date() == tracker.current_pacific_date() + timedelta(days=1)

    # - The whole point: this instant is midnight in Pacific, and (outside the
    #   handful of zones that share it) not midnight where the machine is.
    assert reset.astimezone(PACIFIC).hour == 0


def test_reset_description_names_pacific_and_local(tracker):
    text = tracker.reset_description()
    assert "Pacific" in text
    assert "local time" in text


def test_state_survives_a_new_tracker_instance(tmp_path):
    path = tmp_path / "quota.json"
    QuotaTracker(state_path=path).record("videos.list")
    assert QuotaTracker(state_path=path).used_today() == 1


def test_corrupted_state_file_is_treated_as_no_spend(tmp_path):
    path = tmp_path / "quota.json"
    path.write_text("{not json at all", encoding="utf-8")
    tracker = QuotaTracker(state_path=path)
    assert tracker.used_today() == 0
    tracker.record("videos.list")
    assert tracker.used_today() == 1


def test_negative_or_nonsense_usage_is_ignored(tmp_path):
    path = tmp_path / "quota.json"
    path.write_text(
        json.dumps({"pacific_date": datetime.now(PACIFIC).date().isoformat(), "used": "lots"}),
        encoding="utf-8",
    )
    assert QuotaTracker(state_path=path).used_today() == 0


def test_a_state_file_holding_the_wrong_shape_is_ignored(tmp_path):
    path = tmp_path / "quota.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert QuotaTracker(state_path=path).used_today() == 0


def test_state_path_can_come_from_the_environment(tmp_path, monkeypatch):
    target = tmp_path / "from-env.json"
    monkeypatch.setenv("ROZETTA_QUOTA_FILE", str(target))
    QuotaTracker().record("videos.list")
    assert target.exists()
