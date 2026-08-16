"""Shared fixtures: a fake Data API transport and throwaway quota state.

Nothing in the test suite touches the network or the real quota file.
"""

from __future__ import annotations

import httpx
import pytest

import stats as stats_module
from quota import QuotaTracker
from stats import API_BASE, YouTubeDataAPI


class RecordedRequest:
    def __init__(self, request: httpx.Request) -> None:
        self.url = request.url
        self.path = request.url.path
        self.params = dict(request.url.params)
        # - Kept as httpx.Headers so lookups stay case-insensitive.
        self.headers = request.headers


class FakeDataAPI:
    """Serves canned JSON for videos/channels/playlistItems and records calls."""

    def __init__(self, responses: dict[str, object]) -> None:
        # - Values may be a dict (same answer every time) or a list (one per call).
        self.responses = responses
        self.requests: list[RecordedRequest] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(RecordedRequest(request))
        endpoint = request.url.path.rsplit("/", 1)[-1]

        if endpoint not in self.responses:
            raise AssertionError(f"Unexpected call to {endpoint}")

        payload = self.responses[endpoint]
        if isinstance(payload, list):
            index = sum(1 for r in self.requests if r.path.endswith(endpoint)) - 1
            payload = payload[min(index, len(payload) - 1)]

        if isinstance(payload, tuple):
            status, body = payload
            return httpx.Response(status, json=body)
        return httpx.Response(200, json=payload)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport, base_url=API_BASE)

    def paths_called(self) -> list[str]:
        return [r.path for r in self.requests]


class ExplodingTransport(httpx.MockTransport):
    """Fails the test loudly if anything actually tries to make a request."""

    def __init__(self) -> None:
        super().__init__(self._boom)

    @staticmethod
    def _boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"No HTTP call expected, but got {request.url}")


@pytest.fixture
def tracker(tmp_path) -> QuotaTracker:
    return QuotaTracker(state_path=tmp_path / "quota.json")


@pytest.fixture(autouse=True)
def _isolate_module_client(tmp_path, monkeypatch):
    """Keep the module-level client out of the real environment and home dir."""
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.setenv("ROZETTA_QUOTA_FILE", str(tmp_path / "autouse-quota.json"))
    stats_module.reset_client()
    yield
    stats_module.reset_client()


def configure_fake_api(fake: FakeDataAPI, tracker: QuotaTracker, api_key: str = "test-key"):
    """Point the module-level client at a FakeDataAPI."""
    return stats_module.configure(api_key, tracker=tracker, http=fake.client())


def video_item(
    video_id: str = "dQw4w9WgXcQ",
    title: str = "Test Video",
    description: str = "First sentence. Second sentence. Third sentence. Fourth sentence.",
    duration: str = "PT4M13S",
    statistics: dict | None = None,
    tags: list[str] | None = None,
) -> dict:
    return {
        "id": video_id,
        "snippet": {
            "title": title,
            "description": description,
            "channelTitle": "Test Channel",
            "channelId": "UCuAXFkgsw1L7xaCfnd5JJOw",
            "publishedAt": "2009-10-25T06:57:33Z",
            "tags": tags if tags is not None else ["music", "test"],
        },
        "contentDetails": {"duration": duration},
        "statistics": statistics
        if statistics is not None
        else {"viewCount": "1500000000", "likeCount": "17000000", "commentCount": "2200000"},
    }


def channel_item(
    channel_id: str = "UCuAXFkgsw1L7xaCfnd5JJOw",
    statistics: dict | None = None,
    uploads_playlist: str | None = "UUuAXFkgsw1L7xaCfnd5JJOw",
) -> dict:
    item = {
        "id": channel_id,
        "snippet": {"title": "Test Channel", "publishedAt": "2006-04-23T14:45:51Z"},
        "statistics": statistics
        if statistics is not None
        else {
            "subscriberCount": "12300000",
            "videoCount": "437",
            "viewCount": "3400000000",
            "hiddenSubscriberCount": False,
        },
        "contentDetails": {},
    }
    if uploads_playlist:
        item["contentDetails"] = {"relatedPlaylists": {"uploads": uploads_playlist}}
    return item


def playlist_items(published_dates: list[str]) -> dict:
    return {
        "items": [{"snippet": {"publishedAt": d}} for d in published_dates],
    }


__all__ = [
    "FakeDataAPI",
    "ExplodingTransport",
    "YouTubeDataAPI",
    "configure_fake_api",
    "video_item",
    "channel_item",
    "playlist_items",
]
