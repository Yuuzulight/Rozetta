"""The get_video_info tool: a cheap "is this the right video?" check.

It works from the same videos.list response get_video_stats parses — 1 quota
unit — but hands back only the identity fields plus a short slice of the
description.

There is no transcript-length estimate here, on purpose. Getting a real number
means fetching the transcript, which costs exactly as much as just calling
watch_video, and guessing from the duration is unreliable enough to be worse
than saying nothing: a 40-minute lecture and a 40-minute music video have
wildly different transcript sizes.
"""

from __future__ import annotations

from models import VideoInfo, YouTubeApiError, extract_video_id, sentence_preview
from stats import client, video_stats_from_item


def get_video_info(url_or_id: str) -> VideoInfo:
    video_id = extract_video_id(url_or_id)

    items = client().videos([video_id])
    if not items:
        raise YouTubeApiError(
            f"No video found for ID {video_id!r}. It may have been deleted, made "
            "private, or the URL may be wrong."
        )

    item = items[0]
    stats = video_stats_from_item(item)
    description = item.get("snippet", {}).get("description", "")

    return VideoInfo(
        video_id=stats.video_id,
        title=stats.title,
        channel_title=stats.channel_title,
        duration_seconds=stats.duration_seconds,
        description_preview=sentence_preview(description),
    )
