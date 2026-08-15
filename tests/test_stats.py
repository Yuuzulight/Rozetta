"""The Data API wrapper: parsing, batching, quota gating, and error mapping."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

import stats as stats_module
from conftest import (
    ExplodingTransport,
    FakeDataAPI,
    channel_item,
    configure_fake_api,
    playlist_items,
    video_item,
)
from models import (
    ApiKeyMissing,
    ChannelQuery,
    QuotaExhausted,
    YouTubeApiError,
)
from stats import (
    API_BASE,
    YouTubeDataAPI,
    get_channel_stats,
    get_video_stats,
    get_video_stats_batch,
)

VIDEO_ID = "dQw4w9WgXcQ"
CHANNEL_ID = "UCuAXFkgsw1L7xaCfnd5JJOw"


def days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


def error_body(reason: str, message: str) -> dict:
    return {"error": {"code": 403, "message": message, "errors": [{"reason": reason}]}}


# -- video stats ------------------------------------------------------------


def test_video_stats_are_parsed_from_videos_list(tracker):
    fake = FakeDataAPI({"videos": {"items": [video_item()]}})
    configure_fake_api(fake, tracker)

    result = get_video_stats(f"https://www.youtube.com/watch?v={VIDEO_ID}")

    assert result.video_id == VIDEO_ID
    assert result.title == "Test Video"
    assert result.channel_title == "Test Channel"
    assert result.channel_id == CHANNEL_ID
    assert result.published_at == "2009-10-25T06:57:33Z"
    assert result.view_count == 1_500_000_000
    assert result.like_count == 17_000_000
    assert result.comment_count == 2_200_000
    assert result.duration_seconds == 253
    assert result.tags == ["music", "test"]


def test_hidden_like_count_is_none_not_zero(tracker):
    fake = FakeDataAPI(
        {"videos": {"items": [video_item(statistics={"viewCount": "500"})]}}
    )
    configure_fake_api(fake, tracker)

    result = get_video_stats(VIDEO_ID)

    assert result.view_count == 500
    assert result.like_count is None
    assert result.comment_count is None


def test_missing_video_is_an_explicit_error(tracker):
    fake = FakeDataAPI({"videos": {"items": []}})
    configure_fake_api(fake, tracker)

    with pytest.raises(YouTubeApiError, match="No video found"):
        get_video_stats(VIDEO_ID)


# -- batching ---------------------------------------------------------------


def test_multiple_ids_ride_in_one_request(tracker):
    ids = [f"vid{i:08d}".ljust(11, "x")[:11] for i in range(5)]
    fake = FakeDataAPI({"videos": {"items": [video_item(video_id=i) for i in ids]}})
    configure_fake_api(fake, tracker)

    results = get_video_stats_batch(ids)

    assert len(results) == 5
    assert len(fake.requests) == 1
    assert fake.requests[0].params["id"] == ",".join(ids)
    assert tracker.used_today() == 1


def test_more_than_fifty_ids_are_split_into_chunks_of_fifty(tracker):
    ids = [f"v{i:010d}"[:11] for i in range(60)]
    fake = FakeDataAPI({"videos": [{"items": []}, {"items": []}]})
    configure_fake_api(fake, tracker)

    get_video_stats_batch(ids)

    assert len(fake.requests) == 2
    assert len(fake.requests[0].params["id"].split(",")) == 50
    assert len(fake.requests[1].params["id"].split(",")) == 10


# -- quota ------------------------------------------------------------------


def test_each_call_records_its_quota_cost(tracker):
    fake = FakeDataAPI({"videos": {"items": [video_item()]}})
    configure_fake_api(fake, tracker)

    get_video_stats(VIDEO_ID)
    get_video_stats(VIDEO_ID)

    assert tracker.used_today() == 2


def test_exhausted_quota_blocks_before_any_request_is_made(tracker):
    tracker._write_state(tracker.current_pacific_date(), 10_000)
    api = YouTubeDataAPI(
        "test-key",
        tracker=tracker,
        http=httpx.Client(transport=ExplodingTransport(), base_url=API_BASE),
    )
    stats_module.configure("test-key", tracker=tracker, http=api.http)

    with pytest.raises(QuotaExhausted) as excinfo:
        get_video_stats(VIDEO_ID)

    message = str(excinfo.value)
    assert "quota exhausted for today" in message.lower()
    assert "Pacific" in message
    assert tracker.used_today() == 10_000


def test_a_403_from_google_is_reported_as_quota_not_as_a_raw_error(tracker):
    fake = FakeDataAPI(
        {"videos": (403, error_body("quotaExceeded", "You have exceeded your quota."))}
    )
    configure_fake_api(fake, tracker)

    with pytest.raises(QuotaExhausted) as excinfo:
        get_video_stats(VIDEO_ID)
    assert "resets at" in str(excinfo.value)


def test_quota_is_counted_even_when_the_request_fails(tracker):
    fake = FakeDataAPI({"videos": (500, {"error": {"message": "boom"}})})
    configure_fake_api(fake, tracker)

    with pytest.raises(YouTubeApiError):
        get_video_stats(VIDEO_ID)
    assert tracker.used_today() == 1


# -- credentials ------------------------------------------------------------


def test_a_missing_api_key_fails_before_any_request(tracker):
    stats_module.configure(
        None, tracker=tracker, http=httpx.Client(transport=ExplodingTransport(), base_url=API_BASE)
    )

    with pytest.raises(ApiKeyMissing) as excinfo:
        get_video_stats(VIDEO_ID)

    assert "YOUTUBE_API_KEY" in str(excinfo.value)
    assert tracker.used_today() == 0


def test_a_rejected_key_points_at_the_cloud_console(tracker):
    fake = FakeDataAPI({"videos": (400, error_body("keyInvalid", "API key not valid."))})
    configure_fake_api(fake, tracker)

    with pytest.raises(YouTubeApiError) as excinfo:
        get_video_stats(VIDEO_ID)

    message = str(excinfo.value)
    assert "YOUTUBE_API_KEY is correct" in message
    assert "YouTube Data API v3 is enabled" in message


def test_the_key_travels_in_the_query_string_not_from_the_caller(tracker):
    fake = FakeDataAPI({"videos": {"items": [video_item()]}})
    configure_fake_api(fake, tracker, api_key="secret-key")

    get_video_stats(VIDEO_ID)

    assert fake.requests[0].params["key"] == "secret-key"


def test_network_failure_is_wrapped(tracker):
    def handler(request):
        raise httpx.ConnectError("no route to host", request=request)

    stats_module.configure(
        "test-key",
        tracker=tracker,
        http=httpx.Client(transport=httpx.MockTransport(handler), base_url=API_BASE),
    )

    with pytest.raises(YouTubeApiError, match="Network error"):
        get_video_stats(VIDEO_ID)


# -- channel stats ----------------------------------------------------------


def test_channel_stats_are_parsed_and_cadence_is_derived(tracker):
    fake = FakeDataAPI(
        {
            "channels": {"items": [channel_item()]},
            "playlistItems": playlist_items([days_ago(2), days_ago(9), days_ago(16), days_ago(60)]),
        }
    )
    configure_fake_api(fake, tracker)

    result = get_channel_stats("@testchannel")

    assert result.channel_id == CHANNEL_ID
    assert result.title == "Test Channel"
    assert result.subscriber_count == 12_300_000
    assert result.video_count == 437
    assert result.view_count == 3_400_000_000
    assert result.created_at == "2006-04-23T14:45:51Z"
    assert result.recent_upload_cadence == "~0.7 videos/week over last 30 days"


def test_channel_lookup_costs_two_units_and_never_uses_search(tracker):
    fake = FakeDataAPI(
        {
            "channels": {"items": [channel_item()]},
            "playlistItems": playlist_items([days_ago(1)]),
        }
    )
    configure_fake_api(fake, tracker)

    get_channel_stats("@testchannel")

    assert tracker.used_today() == 2
    assert "/search" not in " ".join(fake.paths_called())


def test_handle_is_looked_up_with_for_handle(tracker):
    fake = FakeDataAPI(
        {"channels": {"items": [channel_item()]}, "playlistItems": playlist_items([])}
    )
    configure_fake_api(fake, tracker)

    get_channel_stats("https://www.youtube.com/@testchannel")

    assert fake.requests[0].params["forHandle"] == "@testchannel"


def test_hidden_subscriber_count_is_none(tracker):
    fake = FakeDataAPI(
        {
            "channels": {
                "items": [
                    channel_item(
                        statistics={
                            "videoCount": "10",
                            "viewCount": "100",
                            "hiddenSubscriberCount": True,
                            "subscriberCount": "0",
                        }
                    )
                ]
            },
            "playlistItems": playlist_items([days_ago(3)]),
        }
    )
    configure_fake_api(fake, tracker)

    assert get_channel_stats("@hidden").subscriber_count is None


def test_a_quiet_channel_says_so_rather_than_reporting_zero_per_week(tracker):
    fake = FakeDataAPI(
        {
            "channels": {"items": [channel_item()]},
            "playlistItems": playlist_items([days_ago(200), days_ago(400)]),
        }
    )
    configure_fake_api(fake, tracker)

    assert get_channel_stats("@quiet").recent_upload_cadence == "no uploads in the last 30 days"


def test_a_full_page_of_recent_uploads_is_reported_as_a_lower_bound(tracker):
    fake = FakeDataAPI(
        {
            "channels": {"items": [channel_item()]},
            "playlistItems": playlist_items([days_ago(1)] * 50),
        }
    )
    configure_fake_api(fake, tracker)

    cadence = get_channel_stats("@busy").recent_upload_cadence
    assert cadence.startswith("at least ~")


def test_channel_without_an_uploads_playlist_has_no_cadence(tracker):
    fake = FakeDataAPI({"channels": {"items": [channel_item(uploads_playlist=None)]}})
    configure_fake_api(fake, tracker)

    assert get_channel_stats("@nouploads").recent_upload_cadence is None


def test_missing_channel_is_an_explicit_error(tracker):
    fake = FakeDataAPI({"channels": {"items": []}})
    configure_fake_api(fake, tracker)

    with pytest.raises(YouTubeApiError) as excinfo:
        get_channel_stats("@ghost")

    message = str(excinfo.value)
    assert "No channel found" in message
    assert "UC... channel ID" in message


def test_legacy_user_url_falls_back_to_the_page_when_for_username_finds_nothing(
    tracker, monkeypatch
):
    fake = FakeDataAPI(
        {
            "channels": [{"items": []}, {"items": [channel_item()]}],
            "playlistItems": playlist_items([days_ago(4)]),
        }
    )
    configure_fake_api(fake, tracker)
    monkeypatch.setattr(
        stats_module,
        "resolve_legacy_user_via_page",
        lambda query, http=None: ChannelQuery("id", CHANNEL_ID, "legacy_user"),
    )

    result = get_channel_stats("https://www.youtube.com/user/OldSchool")

    assert result.channel_id == CHANNEL_ID
    assert fake.requests[0].params["forUsername"] == "OldSchool"
    assert fake.requests[1].params["id"] == CHANNEL_ID


def test_legacy_custom_url_is_resolved_before_the_api_call(tracker, monkeypatch):
    fake = FakeDataAPI(
        {
            "channels": {"items": [channel_item()]},
            "playlistItems": playlist_items([days_ago(5)]),
        }
    )
    configure_fake_api(fake, tracker)
    monkeypatch.setattr(
        stats_module,
        "resolve_channel_query",
        lambda raw, http=None: ChannelQuery("id", CHANNEL_ID, "legacy_custom"),
    )

    get_channel_stats("https://www.youtube.com/c/OldCustomName")

    assert fake.requests[0].params["id"] == CHANNEL_ID


# -- misc -------------------------------------------------------------------


def test_unparsable_publish_dates_are_skipped_not_fatal(tracker):
    fake = FakeDataAPI(
        {
            "channels": {"items": [channel_item()]},
            "playlistItems": {
                "items": [
                    {"snippet": {"publishedAt": "not-a-date"}},
                    {"snippet": {}},
                    {"snippet": {"publishedAt": days_ago(3)}},
                ]
            },
        }
    )
    configure_fake_api(fake, tracker)

    assert get_channel_stats("@messy").recent_upload_cadence is not None


def test_nonsense_counts_do_not_crash_the_parse(tracker):
    fake = FakeDataAPI(
        {
            "videos": {
                "items": [
                    video_item(
                        statistics={
                            "viewCount": "not-a-number",
                            "likeCount": "also-not",
                            "commentCount": None,
                        }
                    )
                ]
            }
        }
    )
    configure_fake_api(fake, tracker)

    result = get_video_stats(VIDEO_ID)

    assert result.view_count == 0
    assert result.like_count is None
    assert result.comment_count is None


def test_an_empty_id_list_makes_no_request_and_spends_nothing(tracker):
    api = YouTubeDataAPI(
        "test-key",
        tracker=tracker,
        http=httpx.Client(transport=ExplodingTransport(), base_url=API_BASE),
    )

    assert api.videos([]) == []
    assert tracker.used_today() == 0


def test_a_non_json_error_body_still_produces_a_readable_error(tracker):
    def handler(request):
        return httpx.Response(502, text="<html>Bad Gateway</html>")

    stats_module.configure(
        "test-key",
        tracker=tracker,
        http=httpx.Client(transport=httpx.MockTransport(handler), base_url=API_BASE),
    )

    with pytest.raises(YouTubeApiError, match="HTTP 502"):
        get_video_stats(VIDEO_ID)


def test_a_publish_date_without_a_timezone_is_treated_as_utc(tracker):
    naive = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S")
    fake = FakeDataAPI(
        {
            "channels": {"items": [channel_item()]},
            "playlistItems": {"items": [{"snippet": {"publishedAt": naive}}]},
        }
    )
    configure_fake_api(fake, tracker)

    assert get_channel_stats("@naive").recent_upload_cadence == "~0.2 videos/week over last 30 days"


def test_client_is_lazily_configured_from_the_environment(monkeypatch, tmp_path):
    stats_module.reset_client()
    monkeypatch.setenv("YOUTUBE_API_KEY", "from-env")
    monkeypatch.setenv("ROZETTA_QUOTA_FILE", str(tmp_path / "q.json"))

    assert stats_module.client().api_key == "from-env"
