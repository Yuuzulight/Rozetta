"""Typed request/response shapes and the error hierarchy for Rozetta.

Every tool returns one of the dataclasses here, and every failure raises one of
the RozettaError subclasses. Keeping both in one place makes it easy to see, at
a glance, exactly what a caller can get back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


@dataclass
class VideoInfo:
    """Cheap identity check — is this the video the user meant?"""

    video_id: str
    title: str
    channel_title: str
    duration_seconds: int
    description_preview: str


@dataclass
class TranscriptResult:
    """Transcript only. Title/duration deliberately live on VideoInfo instead."""

    video_id: str
    transcript_text: str
    transcript_segments: list[dict] = field(default_factory=list)
    language: str = ""
    is_auto_generated: bool = False
    was_translated: bool = False


@dataclass
class VideoStats:
    """Engagement metrics from videos.list."""

    video_id: str
    title: str
    channel_title: str
    channel_id: str
    published_at: str
    view_count: int
    like_count: int | None
    comment_count: int | None
    duration_seconds: int
    tags: list[str] = field(default_factory=list)


@dataclass
class ChannelStats:
    """Channel-level metrics from channels.list (+ one playlistItems.list call)."""

    channel_id: str
    title: str
    subscriber_count: int | None
    video_count: int
    view_count: int
    created_at: str
    recent_upload_cadence: str | None = None


@dataclass
class ChannelQuery:
    """How we intend to ask the Data API about a channel.

    `param` is the literal channels.list query parameter, `source` records which
    of the four URL formats we started from — that distinction matters because
    two of them are reliable and two are legacy best-effort.
    """

    param: str  # "id" | "forHandle" | "forUsername"
    value: str
    source: str  # "channel_id" | "handle" | "legacy_user" | "legacy_custom"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RozettaError(Exception):
    """Base class so a caller can catch everything this server raises."""


class InvalidVideoIdentifier(RozettaError):
    """The string given isn't a YouTube video URL or an 11-character video ID."""


class ApiKeyMissing(RozettaError):
    """A Data API tool was called but YOUTUBE_API_KEY wasn't set at startup."""

    def __init__(self) -> None:
        super().__init__(
            "YOUTUBE_API_KEY is not set. Statistics tools need a YouTube Data API "
            "v3 key supplied as an environment variable when this server is "
            "launched. Transcript retrieval works without one."
        )


class QuotaExhausted(RozettaError):
    """Blocked locally before spending a request we know would fail."""


class YouTubeApiError(RozettaError):
    """The Data API answered, but with an error or with no matching item."""


class NoTranscriptAvailable(RozettaError):
    """The video genuinely has no captions — a fact about the video."""


class VideoNotAccessible(RozettaError):
    """Private, age-restricted, or otherwise unplayable without signing in."""


class TranscriptExtractionFailed(RozettaError):
    """The unofficial transcript library broke.

    Kept separate from NoTranscriptAvailable on purpose: this one usually means
    YouTube changed something upstream, not that the video lacks captions.
    """


class TranslationUnavailable(RozettaError):
    """Asked for a target language YouTube can't translate this transcript into."""


class ChannelResolutionFailed(RozettaError):
    """Couldn't turn the given channel URL into something the API accepts."""


# ---------------------------------------------------------------------------
# Small shared parsers
# ---------------------------------------------------------------------------

# - Every URL form YouTube uses for a single video. Bare IDs are handled separately.
_VIDEO_URL_PATTERNS = (
    re.compile(r"[?&]v=(?P<id>[0-9A-Za-z_-]{11})"),
    re.compile(r"youtu\.be/(?P<id>[0-9A-Za-z_-]{11})"),
    re.compile(r"youtube\.com/shorts/(?P<id>[0-9A-Za-z_-]{11})"),
    re.compile(r"youtube\.com/embed/(?P<id>[0-9A-Za-z_-]{11})"),
    re.compile(r"youtube\.com/live/(?P<id>[0-9A-Za-z_-]{11})"),
    re.compile(r"youtube\.com/v/(?P<id>[0-9A-Za-z_-]{11})"),
)

_BARE_VIDEO_ID = re.compile(r"^[0-9A-Za-z_-]{11}$")

_ISO_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$"
)


def extract_video_id(url_or_id: str) -> str:
    """Pull an 11-character video ID out of any common YouTube URL, or pass one through."""
    candidate = (url_or_id or "").strip()
    if not candidate:
        raise InvalidVideoIdentifier("No video URL or ID was given.")

    if _BARE_VIDEO_ID.match(candidate):
        return candidate

    for pattern in _VIDEO_URL_PATTERNS:
        match = pattern.search(candidate)
        if match:
            return match.group("id")

    raise InvalidVideoIdentifier(
        f"Could not find a YouTube video ID in {url_or_id!r}. Give a watch/share/"
        "shorts/embed URL, or the bare 11-character video ID."
    )


def parse_iso8601_duration(value: str) -> int:
    """Turn the Data API's ISO 8601 duration (e.g. 'PT4M13S') into whole seconds."""
    match = _ISO_DURATION.match((value or "").strip())
    if not match:
        # - Live streams report P0D and other oddities; 0 is more useful than a crash.
        return 0
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()}
    return (
        parts["days"] * 86_400
        + parts["hours"] * 3_600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def sentence_preview(description: str, max_sentences: int = 3, max_chars: int = 400) -> str:
    """First few sentences of a description, whitespace collapsed.

    YouTube descriptions routinely run to hundreds of lines of links and
    timestamps. All we want here is enough prose to recognise the video.
    """
    text = " ".join((description or "").split())
    if not text:
        return ""

    sentences = re.split(r"(?<=[.!?])\s+", text)
    preview = " ".join(sentences[:max_sentences]).strip()
    if len(preview) > max_chars:
        preview = preview[:max_chars].rstrip() + "…"
    return preview
