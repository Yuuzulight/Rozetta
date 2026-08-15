"""Rozetta — an MCP server for YouTube transcripts and statistics.

Entry point and tool registration. Transport is stdio only: the MCP client
spawns this process per session and talks to it over the pipe, so there is no
hosted mode and nothing listening on a port.

The API key is read here, once, from the environment. It is never a tool
argument and never appears in a tool's input schema, which keeps it out of the
tool-call logs on the client side.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# - Allow `python src/server.py` from anywhere without an install step.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server import MCPServer  # noqa: E402

import stats  # noqa: E402
from models import (  # noqa: E402
    ChannelStats,
    RozettaError,
    TranscriptResult,
    VideoInfo,
    VideoStats,
)
from quota import QuotaTracker  # noqa: E402
from transcript import fetch_transcript  # noqa: E402
from video_info import get_video_info as _get_video_info  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Read KEY=VALUE lines from a .env file if one is sitting next to the code.

    Small enough not to warrant a dependency. Real environment variables always
    win, since that's how the MCP client passes the key in.
    """
    env_file = path or (REPO_ROOT / ".env")
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


server = MCPServer(
    name="rozetta",
    version="0.1.0",
    instructions=(
        "Rozetta exposes YouTube transcripts and public statistics as raw data. "
        "It does no summarising of its own — read the data it returns and do the "
        "interpretation yourself.\n\n"
        "Convention when a user asks you to watch or analyse a video: call "
        "get_video_info first, show the title and channel to the user so they can "
        "confirm it's the video they meant, then call watch_video. Transcripts are "
        "returned in full and can be very long, so that confirmation step is worth "
        "the extra call."
    ),
)


@server.tool(
    name="get_video_info",
    title="Get YouTube video info",
    description=(
        "Confirm which video a URL or ID refers to. Returns the title, channel, "
        "duration in seconds, and the first few sentences of the description.\n\n"
        "Call this BEFORE watch_video and show the result to the user so they can "
        "confirm it is the video they meant. This is cheap (1 API quota unit); "
        "transcripts are not, and can be very long.\n\n"
        "It intentionally does not estimate transcript length — working that out "
        "accurately would cost as much as fetching the transcript, and guessing "
        "from duration is unreliable. Use duration_seconds and your own judgement "
        "to warn the user if a video looks long.\n\n"
        "Accepts any YouTube video URL (watch, youtu.be, shorts, embed, live) or a "
        "bare 11-character video ID. Requires YOUTUBE_API_KEY to be configured on "
        "the server."
    ),
)
def get_video_info(url_or_id: str) -> VideoInfo:
    return _get_video_info(url_or_id)


@server.tool(
    name="watch_video",
    title="Watch a YouTube video (transcript)",
    description=(
        "Fetch the full transcript of a public YouTube video. This is how you "
        "'watch' a video: you get the spoken words, not the picture.\n\n"
        "The transcript is returned complete and uncapped, which for a long video "
        "can be a very large amount of text. Call get_video_info first and confirm "
        "the video with the user before calling this.\n\n"
        "Returns transcript_text (plain prose, timestamps stripped) plus "
        "transcript_segments (each with text, start, duration) if you need to cite "
        "or jump to a moment.\n\n"
        "Set target_language to a language code such as 'en', 'es' or 'ja' to get "
        "the transcript in that language. A real caption track in that language is "
        "used if one exists; otherwise YouTube's own translation is applied. If "
        "the language is unavailable the call fails with a list of the languages "
        "that are available — it never silently returns untranslated text.\n\n"
        "Distinguish the failure modes: 'no transcript available' means the video "
        "has no captions, while 'transcript extraction failed' means the "
        "extraction mechanism itself broke and is worth retrying later. Private "
        "and age-restricted videos are not accessible; this server handles public "
        "videos only.\n\n"
        "Costs no API quota — transcripts do not go through the YouTube Data API."
    ),
)
def watch_video(url_or_id: str, target_language: str | None = None) -> TranscriptResult:
    return fetch_transcript(url_or_id, target_language=target_language)


@server.tool(
    name="get_video_stats",
    title="Get YouTube video statistics",
    description=(
        "Engagement metrics for a public video: view count, like count, comment "
        "count, publish date, duration, and tags.\n\n"
        "Use this when the question is about a video's performance or reach. For "
        "simply identifying a video, use get_video_info instead — it returns the "
        "identity fields without pulling vote and comment data.\n\n"
        "like_count and comment_count are null when the uploader has hidden likes "
        "or disabled comments. Null means 'not published', not zero.\n\n"
        "Costs 1 API quota unit. Requires YOUTUBE_API_KEY to be configured on the "
        "server."
    ),
)
def get_video_stats(url_or_id: str) -> VideoStats:
    return stats.get_video_stats(url_or_id)


@server.tool(
    name="get_channel_stats",
    title="Get YouTube channel statistics",
    description=(
        "Channel-level metrics: subscriber count, total video count, total view "
        "count, creation date, and a rough recent upload cadence.\n\n"
        "Accepts all four channel URL formats, but they are not equally reliable. "
        "An @handle or a UC... channel ID is looked up through the official API "
        "and is dependable. A legacy /user/ URL usually works. A legacy /c/ URL "
        "has no API equivalent at all and has to be resolved by reading the "
        "channel page, which can fail — if it does, ask the user for the @handle "
        "or channel ID rather than retrying.\n\n"
        "subscriber_count is null when the channel hides it. "
        "recent_upload_cadence is derived from the last page of the uploads "
        "playlist and is approximate.\n\n"
        "Costs 2 API quota units (1 for the channel, 1 for the upload cadence). "
        "Requires YOUTUBE_API_KEY to be configured on the server."
    ),
)
def get_channel_stats(channel_url_or_id: str) -> ChannelStats:
    return stats.get_channel_stats(channel_url_or_id)


def main() -> None:
    load_dotenv()
    # - Read once, at startup. Deliberately not a tool argument: keeping it out of
    #   the tool schemas keeps it out of client-side tool-call logs.
    stats.configure(os.environ.get("YOUTUBE_API_KEY"), tracker=QuotaTracker())
    server.run(transport="stdio")


__all__ = [
    "server",
    "main",
    "load_dotenv",
    "get_video_info",
    "watch_video",
    "get_video_stats",
    "get_channel_stats",
    "RozettaError",
]


if __name__ == "__main__":
    main()
