"""Turning any of YouTube's four channel-identifier formats into an API query.

Two of the four are first-class citizens of the Data API and two are not, and
that difference is worth being loud about rather than hiding behind a uniform
interface:

    @handle          channels.list?forHandle=      official, 1 unit, reliable
    UC... id         channels.list?id=             official, 1 unit, reliable
    /user/Name       channels.list?forUsername=    official, but only works for
                                                   channels that still carry an
                                                   old-style username
    /c/CustomName    no API parameter exists       requires fetching the page and
                                                   digging the channel ID out of
                                                   the HTML

That last one isn't an oversight on Google's part. /c/ links are a vanity
redirect layer sitting on top of channel IDs, not an identifier the API ever
indexed. Scraping for it is in the same reliability bracket as the transcript
library: it works today and could stop working whenever YouTube reshuffles its
markup.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

import httpx

from models import ChannelQuery, ChannelResolutionFailed

CHANNEL_ID = re.compile(r"^UC[0-9A-Za-z_-]{22}$")

# - Three places the channel ID shows up in a rendered channel page. Any hit wins.
_PAGE_ID_PATTERNS = (
    re.compile(r'"channelId"\s*:\s*"(UC[0-9A-Za-z_-]{22})"'),
    re.compile(r'<meta\s+itemprop="identifier"\s+content="(UC[0-9A-Za-z_-]{22})"'),
    re.compile(r'href="https://www\.youtube\.com/channel/(UC[0-9A-Za-z_-]{22})"'),
)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_USE_HANDLE_INSTEAD = (
    "Give the channel's @handle (from the channel page header) or its UC... "
    "channel ID instead of the legacy URL."
)


def parse_channel_identifier(raw: str) -> ChannelQuery:
    """Classify a channel string. Legacy /c/ names come back with an empty `param`.

    An empty `param` is the signal that no API parameter can answer this and a
    page fetch is required — see resolve_channel_query.
    """
    text = (raw or "").strip()
    if not text:
        raise ChannelResolutionFailed("No channel URL, handle, or ID was given.")

    # - Bare forms first, before we bother parsing a URL out of it.
    if CHANNEL_ID.match(text):
        return ChannelQuery(param="id", value=text, source="channel_id")
    if text.startswith("@") and "/" not in text:
        return ChannelQuery(param="forHandle", value=text, source="handle")

    if "youtube.com" in text or "youtu.be" in text:
        return _parse_channel_url(text)

    # - Not a URL and not a UC id: treat it as a handle, which is what people
    #   type these days. If it was really an old /c/ name the API will come back
    #   empty and the caller's error explains the two formats that do work.
    return ChannelQuery(param="forHandle", value=f"@{text.lstrip('@')}", source="handle")


def _parse_channel_url(text: str) -> ChannelQuery:
    parsed = urlparse(text if "//" in text else f"https://{text}")
    segments = [unquote(s) for s in parsed.path.split("/") if s]

    if not segments:
        raise ChannelResolutionFailed(
            f"{text!r} is a YouTube URL but doesn't point at a channel. " + _USE_HANDLE_INSTEAD
        )

    head = segments[0]

    if head.startswith("@"):
        return ChannelQuery(param="forHandle", value=head, source="handle")

    if head == "channel" and len(segments) > 1:
        if not CHANNEL_ID.match(segments[1]):
            raise ChannelResolutionFailed(
                f"{segments[1]!r} doesn't look like a YouTube channel ID "
                "(they start with 'UC' and are 24 characters long)."
            )
        return ChannelQuery(param="id", value=segments[1], source="channel_id")

    if head == "user" and len(segments) > 1:
        return ChannelQuery(param="forUsername", value=segments[1], source="legacy_user")

    if head == "c" and len(segments) > 1:
        return ChannelQuery(param="", value=segments[1], source="legacy_custom")

    raise ChannelResolutionFailed(
        f"Couldn't tell which channel {text!r} refers to. " + _USE_HANDLE_INSTEAD
    )


def canonical_page_url(query: ChannelQuery) -> str:
    """The youtube.com URL to fetch when we have to fall back to scraping."""
    if query.source == "legacy_custom":
        return f"https://www.youtube.com/c/{query.value}"
    if query.source == "legacy_user":
        return f"https://www.youtube.com/user/{query.value}"
    if query.source == "handle":
        return f"https://www.youtube.com/{query.value}"
    return f"https://www.youtube.com/channel/{query.value}"


def scrape_channel_id(url: str, http: httpx.Client | None = None) -> str:
    """Fetch a channel page and pull the UC... ID out of the HTML.

    Unofficial by necessity. Raises ChannelResolutionFailed with an actionable
    message on any hiccup rather than returning something ambiguous.
    """
    client = http or httpx.Client(timeout=15.0, follow_redirects=True)
    try:
        response = client.get(url, headers=_BROWSER_HEADERS)
    except httpx.HTTPError as exc:
        raise ChannelResolutionFailed(
            f"Couldn't reach {url} to resolve the legacy channel URL ({exc}). "
            + _USE_HANDLE_INSTEAD
        ) from exc
    finally:
        if http is None:
            client.close()

    if response.status_code == 404:
        raise ChannelResolutionFailed(
            f"YouTube returned 404 for {url} — that legacy channel URL no longer exists. "
            + _USE_HANDLE_INSTEAD
        )
    if response.status_code != 200:
        raise ChannelResolutionFailed(
            f"YouTube returned HTTP {response.status_code} for {url}. " + _USE_HANDLE_INSTEAD
        )

    for pattern in _PAGE_ID_PATTERNS:
        match = pattern.search(response.text)
        if match:
            return match.group(1)

    raise ChannelResolutionFailed(
        f"Fetched {url} but couldn't find a channel ID in the page. This usually "
        "means YouTube changed its page structure, since legacy /c/ and /user/ "
        "URLs have no API equivalent and can only be resolved by reading the page. "
        + _USE_HANDLE_INSTEAD
    )


def resolve_channel_query(raw: str, http: httpx.Client | None = None) -> ChannelQuery:
    """Parse an identifier and, for /c/ URLs only, scrape to get a usable query."""
    query = parse_channel_identifier(raw)
    if query.param:
        return query

    channel_id = scrape_channel_id(canonical_page_url(query), http=http)
    return ChannelQuery(param="id", value=channel_id, source="legacy_custom")


def resolve_legacy_user_via_page(
    query: ChannelQuery, http: httpx.Client | None = None
) -> ChannelQuery:
    """Second attempt for /user/ URLs whose forUsername lookup came back empty.

    Plenty of old /user/ links now redirect to a handle without the channel
    keeping its legacy username, which makes forUsername return nothing at all.
    """
    channel_id = scrape_channel_id(canonical_page_url(query), http=http)
    return ChannelQuery(param="id", value=channel_id, source="legacy_user")
