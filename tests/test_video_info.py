"""get_video_info: URL parsing, description previews, and what it deliberately omits."""

from __future__ import annotations

from dataclasses import fields

import pytest

from conftest import FakeDataAPI, configure_fake_api, video_item
from models import (
    InvalidVideoIdentifier,
    VideoInfo,
    YouTubeApiError,
    extract_video_id,
    parse_iso8601_duration,
    sentence_preview,
)
from video_info import get_video_info

VIDEO_ID = "dQw4w9WgXcQ"


# -- identifier parsing -----------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        VIDEO_ID,
        f"https://www.youtube.com/watch?v={VIDEO_ID}",
        f"http://youtube.com/watch?v={VIDEO_ID}&t=42s",
        f"https://youtu.be/{VIDEO_ID}",
        f"https://youtu.be/{VIDEO_ID}?si=abc123",
        f"https://www.youtube.com/shorts/{VIDEO_ID}",
        f"https://www.youtube.com/embed/{VIDEO_ID}",
        f"https://www.youtube.com/live/{VIDEO_ID}",
        f"https://www.youtube.com/v/{VIDEO_ID}",
        f"https://m.youtube.com/watch?app=desktop&v={VIDEO_ID}",
        f"  {VIDEO_ID}  ",
    ],
)
def test_video_ids_come_out_of_every_common_url_form(raw):
    assert extract_video_id(raw) == VIDEO_ID


@pytest.mark.parametrize("raw", ["", "   ", "not a url", "https://vimeo.com/12345", "abc"])
def test_unparseable_identifiers_are_rejected(raw):
    with pytest.raises(InvalidVideoIdentifier):
        extract_video_id(raw)


# -- duration ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("iso", "seconds"),
    [
        ("PT4M13S", 253),
        ("PT1H2M3S", 3_723),
        ("PT30S", 30),
        ("PT2H", 7_200),
        ("P1DT2H", 93_600),
        ("P0D", 0),
        ("", 0),
        ("nonsense", 0),
    ],
)
def test_iso_durations_become_seconds(iso, seconds):
    assert parse_iso8601_duration(iso) == seconds


# -- description preview ----------------------------------------------------


def test_preview_keeps_the_first_three_sentences():
    text = "One. Two. Three. Four. Five."
    assert sentence_preview(text) == "One. Two. Three."


def test_preview_collapses_the_link_dumps_youtube_descriptions_are_full_of():
    text = "Real sentence here.\n\n\nFollow me:\nhttps://example.com\n\nAnother sentence."
    preview = sentence_preview(text)
    assert "\n" not in preview
    assert preview.startswith("Real sentence here.")


def test_preview_truncates_a_runaway_first_sentence():
    preview = sentence_preview("word " * 500, max_chars=80)
    assert len(preview) <= 81
    assert preview.endswith("…")


def test_preview_of_an_empty_description_is_empty():
    assert sentence_preview("") == ""
    assert sentence_preview(None) == ""


# -- the tool ---------------------------------------------------------------


def test_returns_the_identity_fields(tracker):
    fake = FakeDataAPI({"videos": {"items": [video_item()]}})
    configure_fake_api(fake, tracker)

    info = get_video_info(f"https://youtu.be/{VIDEO_ID}")

    assert info.video_id == VIDEO_ID
    assert info.title == "Test Video"
    assert info.channel_title == "Test Channel"
    assert info.duration_seconds == 253
    assert info.description_preview == "First sentence. Second sentence. Third sentence."


def test_costs_a_single_quota_unit(tracker):
    fake = FakeDataAPI({"videos": {"items": [video_item()]}})
    configure_fake_api(fake, tracker)

    get_video_info(VIDEO_ID)

    assert tracker.used_today() == 1
    assert len(fake.requests) == 1


def test_there_is_no_transcript_length_estimate():
    # - Deliberate omission: an accurate estimate costs a transcript fetch, and a
    #   duration-based guess is unreliable enough to mislead.
    names = {f.name for f in fields(VideoInfo)}
    assert names == {
        "video_id",
        "title",
        "channel_title",
        "duration_seconds",
        "description_preview",
    }
    assert not any("transcript" in n or "estimate" in n or "length" in n for n in names)


def test_no_engagement_metrics_leak_into_the_identity_check():
    names = {f.name for f in fields(VideoInfo)}
    assert not names & {"view_count", "like_count", "comment_count", "tags"}


def test_missing_video_is_an_explicit_error(tracker):
    fake = FakeDataAPI({"videos": {"items": []}})
    configure_fake_api(fake, tracker)

    with pytest.raises(YouTubeApiError, match="No video found"):
        get_video_info(VIDEO_ID)


def test_a_video_with_no_description_still_works(tracker):
    fake = FakeDataAPI({"videos": {"items": [video_item(description="")]}})
    configure_fake_api(fake, tracker)

    assert get_video_info(VIDEO_ID).description_preview == ""
