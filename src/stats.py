"""YouTube Data API v3 wrapper: get_video_stats and get_channel_stats.

This talks to the REST endpoints directly over httpx rather than going through
google-api-python-client. The three endpoints we touch are simple GETs with a
key in the query string, and the discovery-document machinery in the official
client buys us nothing here while making the code harder to test.

Every call goes through _get, which checks the local quota budget *before*
spending a request. That way an exhausted budget produces a sentence you can
act on instead of a raw 403.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

import httpx

from channel_resolve import (
    ChannelQuery,
    resolve_channel_query,
    resolve_legacy_user_via_page,
)
from models import (
    ApiKeyMissing,
    ChannelStats,
    QuotaExhausted,
    VideoStats,
    YouTubeApiError,
    extract_video_id,
    parse_iso8601_duration,
)
from quota import QuotaTracker

API_BASE = "https://www.googleapis.com/youtube/v3"

# - Quota keys map onto REST paths. Names match Google's own quota table.
ENDPOINT_PATHS = {
    "videos.list": "videos",
    "channels.list": "channels",
    "playlistItems.list": "playlistItems",
}

MAX_IDS_PER_CALL = 50
CADENCE_WINDOW_DAYS = 30


class YouTubeDataAPI:
    def __init__(
        self,
        api_key: str | None,
        tracker: QuotaTracker | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.api_key = api_key or None
        self.tracker = tracker or QuotaTracker()
        self._http = http
        self._owns_http = http is None

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(base_url=API_BASE, timeout=15.0)
        return self._http

    def close(self) -> None:
        if self._http is not None and self._owns_http:
            self._http.close()
            self._http = None

    # -- transport ----------------------------------------------------------

    def _get(self, endpoint: str, params: dict) -> dict:
        if not self.api_key:
            raise ApiKeyMissing()

        if self.tracker.would_exceed(endpoint):
            raise QuotaExhausted(
                f"YouTube Data API quota exhausted for today "
                f"({self.tracker.used_today()}/{self.tracker.DAILY_BUDGET} units used, "
                f"{self.tracker.cost_of(endpoint)} needed for {endpoint}). "
                f"Quota resets at {self.tracker.reset_description()}."
            )

        # - Recorded before the request goes out: Google bills the call whether or
        #   not we like the answer, and undercounting is the dangerous direction.
        self.tracker.record(endpoint)

        try:
            # - Key goes in a header, never the query string. Anything that logs a
            #   URL — httpx at INFO, a proxy, a crash report — would otherwise
            #   capture the key verbatim, and MCP server stderr lands in the
            #   client's log files.
            response = self.http.get(
                f"/{ENDPOINT_PATHS[endpoint]}",
                params=params,
                headers={"X-goog-api-key": self.api_key},
            )
        except httpx.HTTPError as exc:
            raise YouTubeApiError(f"Network error calling {endpoint}: {exc}") from exc

        if response.status_code != 200:
            raise self._api_error(endpoint, response)

        return response.json()

    def _api_error(self, endpoint: str, response: httpx.Response) -> Exception:
        try:
            payload = response.json().get("error", {})
        except ValueError:
            payload = {}

        message = payload.get("message") or response.text[:200] or "no detail given"
        reasons = {e.get("reason", "") for e in payload.get("errors", [])}

        if "quotaExceeded" in reasons or "dailyLimitExceeded" in reasons:
            return QuotaExhausted(
                f"YouTube rejected the call: daily quota exceeded ({message}). "
                f"Quota resets at {self.tracker.reset_description()}. Note the local "
                f"counter says {self.tracker.used_today()} units used today, so other "
                "applications may be sharing this API key."
            )
        if reasons & {"keyInvalid", "badRequest", "forbidden", "accessNotConfigured"}:
            return YouTubeApiError(
                f"YouTube rejected the API key for {endpoint}: {message}. Check that "
                "YOUTUBE_API_KEY is correct and that YouTube Data API v3 is enabled "
                "on the Google Cloud project the key belongs to."
            )
        return YouTubeApiError(
            f"{endpoint} failed with HTTP {response.status_code}: {message}"
        )

    # -- endpoints ----------------------------------------------------------

    def videos(self, video_ids: Sequence[str]) -> list[dict]:
        """videos.list for one or many IDs.

        Up to 50 IDs ride along in a single call for the same 1 unit, so there is
        no reason to make one request per video.
        """
        ids = list(video_ids)
        if not ids:
            return []

        items: list[dict] = []
        for start in range(0, len(ids), MAX_IDS_PER_CALL):
            chunk = ids[start : start + MAX_IDS_PER_CALL]
            payload = self._get(
                "videos.list",
                {"part": "snippet,contentDetails,statistics", "id": ",".join(chunk)},
            )
            items.extend(payload.get("items", []))
        return items

    def channel(self, query: ChannelQuery) -> dict | None:
        payload = self._get(
            "channels.list",
            {"part": "snippet,statistics,contentDetails", query.param: query.value},
        )
        items = payload.get("items", [])
        return items[0] if items else None

    def playlist_items(self, playlist_id: str, max_results: int = MAX_IDS_PER_CALL) -> list[dict]:
        payload = self._get(
            "playlistItems.list",
            {
                "part": "snippet",
                "playlistId": playlist_id,
                "maxResults": min(max_results, MAX_IDS_PER_CALL),
            },
        )
        return payload.get("items", [])


# ---------------------------------------------------------------------------
# Module-level client, configured once at server startup
# ---------------------------------------------------------------------------

_client: YouTubeDataAPI | None = None


def configure(
    api_key: str | None,
    tracker: QuotaTracker | None = None,
    http: httpx.Client | None = None,
) -> YouTubeDataAPI:
    """Install the API client the tools will use. Called once, at startup."""
    global _client
    _client = YouTubeDataAPI(api_key, tracker=tracker, http=http)
    return _client


def client() -> YouTubeDataAPI:
    if _client is None:
        configure(os.environ.get("YOUTUBE_API_KEY"))
    assert _client is not None
    return _client


def reset_client() -> None:
    """Drop the configured client. Tests use this; nothing else should."""
    global _client
    if _client is not None:
        _client.close()
    _client = None


# ---------------------------------------------------------------------------
# Tool-facing functions
# ---------------------------------------------------------------------------


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_int(value) -> int | None:
    """Hidden counts are absent from the response entirely, not zero."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def video_stats_from_item(item: dict) -> VideoStats:
    snippet = item.get("snippet", {})
    statistics = item.get("statistics", {})
    content = item.get("contentDetails", {})

    return VideoStats(
        video_id=item.get("id", ""),
        title=snippet.get("title", ""),
        channel_title=snippet.get("channelTitle", ""),
        channel_id=snippet.get("channelId", ""),
        published_at=snippet.get("publishedAt", ""),
        view_count=_to_int(statistics.get("viewCount")),
        like_count=_optional_int(statistics.get("likeCount")),
        comment_count=_optional_int(statistics.get("commentCount")),
        duration_seconds=parse_iso8601_duration(content.get("duration", "")),
        tags=list(snippet.get("tags", []) or []),
    )


def get_video_stats(url_or_id: str) -> VideoStats:
    video_id = extract_video_id(url_or_id)
    items = client().videos([video_id])
    if not items:
        raise YouTubeApiError(
            f"No video found for ID {video_id!r}. It may have been deleted, made "
            "private, or the URL may be wrong."
        )
    return video_stats_from_item(items[0])


def get_video_stats_batch(urls_or_ids: Sequence[str]) -> list[VideoStats]:
    """Several videos in as few calls as possible — 50 IDs per request."""
    video_ids = [extract_video_id(v) for v in urls_or_ids]
    items = client().videos(video_ids)
    by_id = {item.get("id"): item for item in items}
    return [video_stats_from_item(by_id[v]) for v in video_ids if v in by_id]


def _format_cadence(items: list[dict], page_was_full: bool) -> str | None:
    """Uploads-per-week over the last 30 days, from one playlistItems page."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=CADENCE_WINDOW_DAYS)

    recent = 0
    for item in items:
        published = item.get("snippet", {}).get("publishedAt")
        if not published:
            continue
        try:
            when = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            recent += 1

    if not items:
        return None
    if recent == 0:
        return f"no uploads in the last {CADENCE_WINDOW_DAYS} days"

    per_week = recent / CADENCE_WINDOW_DAYS * 7
    rendered = f"{per_week:.1f}".rstrip("0").rstrip(".")

    # - We only pull one page. If every video on it is recent, the real rate could
    #   be higher, so say "at least" rather than quietly understating a busy channel.
    prefix = "at least ~" if page_was_full and recent == len(items) else "~"
    return f"{prefix}{rendered} videos/week over last {CADENCE_WINDOW_DAYS} days"


def get_channel_stats(channel_url_or_id: str) -> ChannelStats:
    api = client()
    query = resolve_channel_query(channel_url_or_id)

    item = api.channel(query)

    if item is None and query.source == "legacy_user":
        # - The old username is gone; fall back to reading the page for the ID.
        item = api.channel(resolve_legacy_user_via_page(query))

    if item is None:
        raise YouTubeApiError(
            f"No channel found for {channel_url_or_id!r} (looked it up by "
            f"{query.param}={query.value!r}). Double-check the @handle or use the "
            "UC... channel ID."
        )

    snippet = item.get("snippet", {})
    statistics = item.get("statistics", {})
    uploads_playlist = (
        item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads")
    )

    cadence = None
    if uploads_playlist:
        uploads = api.playlist_items(uploads_playlist)
        cadence = _format_cadence(uploads, page_was_full=len(uploads) >= MAX_IDS_PER_CALL)

    hidden = statistics.get("hiddenSubscriberCount", False)

    return ChannelStats(
        channel_id=item.get("id", ""),
        title=snippet.get("title", ""),
        subscriber_count=None if hidden else _optional_int(statistics.get("subscriberCount")),
        video_count=_to_int(statistics.get("videoCount")),
        view_count=_to_int(statistics.get("viewCount")),
        created_at=snippet.get("publishedAt", ""),
        recent_upload_cadence=cadence,
    )
