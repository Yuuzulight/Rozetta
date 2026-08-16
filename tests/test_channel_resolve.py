"""All four channel-identifier formats, including the legacy fallback failing."""

from __future__ import annotations

import httpx
import pytest

from channel_resolve import (
    canonical_page_url,
    parse_channel_identifier,
    resolve_channel_query,
    resolve_legacy_user_via_page,
    scrape_channel_id,
)
from models import ChannelResolutionFailed

CHANNEL_ID = "UCuAXFkgsw1L7xaCfnd5JJOw"


def page_client(status: int = 200, body: str = "", raise_error: bool = False) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if raise_error:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(status, text=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


# -- format 1: @handle ------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "@rickastley",
        "https://www.youtube.com/@rickastley",
        "youtube.com/@rickastley",
        "https://www.youtube.com/@rickastley/videos",
    ],
)
def test_handle_uses_for_handle(raw):
    query = parse_channel_identifier(raw)
    assert query.param == "forHandle"
    assert query.value == "@rickastley"
    assert query.source == "handle"


# -- format 2: raw channel ID -----------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        CHANNEL_ID,
        f"https://www.youtube.com/channel/{CHANNEL_ID}",
        f"youtube.com/channel/{CHANNEL_ID}/videos",
    ],
)
def test_channel_id_uses_id(raw):
    query = parse_channel_identifier(raw)
    assert query.param == "id"
    assert query.value == CHANNEL_ID
    assert query.source == "channel_id"


def test_malformed_channel_id_is_rejected():
    with pytest.raises(ChannelResolutionFailed, match="channel ID"):
        parse_channel_identifier("https://www.youtube.com/channel/not-an-id")


# -- format 3: legacy /user/ ------------------------------------------------


def test_legacy_user_uses_for_username():
    query = parse_channel_identifier("https://www.youtube.com/user/RickAstleyVEVO")
    assert query.param == "forUsername"
    assert query.value == "RickAstleyVEVO"
    assert query.source == "legacy_user"


def test_legacy_user_falls_back_to_the_page_when_the_username_is_gone():
    body = f'<link rel="canonical" href="https://www.youtube.com/channel/{CHANNEL_ID}">'
    query = parse_channel_identifier("https://www.youtube.com/user/RickAstleyVEVO")
    resolved = resolve_legacy_user_via_page(query, http=page_client(body=body))
    assert resolved.param == "id"
    assert resolved.value == CHANNEL_ID
    assert resolved.source == "legacy_user"


# -- format 4: legacy /c/ ---------------------------------------------------


def test_legacy_custom_has_no_api_parameter():
    query = parse_channel_identifier("https://www.youtube.com/c/RickAstleyOfficial")
    assert query.param == ""
    assert query.source == "legacy_custom"
    assert query.value == "RickAstleyOfficial"


def test_legacy_custom_resolves_through_the_page_fallback():
    body = (
        f'<link rel="canonical" href="https://www.youtube.com/channel/{CHANNEL_ID}">'
    )
    resolved = resolve_channel_query(
        "https://www.youtube.com/c/RickAstleyOfficial", http=page_client(body=body)
    )
    assert resolved.param == "id"
    assert resolved.value == CHANNEL_ID
    assert resolved.source == "legacy_custom"


def test_unrelated_channel_ids_in_the_page_are_ignored():
    # - A real channel page carries a dozen "channelId" values for recommended
    #   channels and sidebar videos, none of them the channel you asked about.
    #   Reading those returned a confidently wrong channel; only tags where the
    #   page declares its own identity count.
    other = "UCin0m13qWv3-051xlWlHamA"
    body = (
        f'window.ytInitialData = {{"channelId":"{other}","more":"{other}"}};'
        f'<a href="https://www.youtube.com/channel/{other}">recommended</a>'
        f'<link rel="canonical" href="https://www.youtube.com/channel/{CHANNEL_ID}">'
    )

    resolved = resolve_channel_query("youtube.com/c/Whoever", http=page_client(body=body))

    assert resolved.value == CHANNEL_ID
    assert resolved.value != other


def test_a_page_with_only_unrelated_ids_fails_rather_than_guessing():
    other = "UCin0m13qWv3-051xlWlHamA"
    body = f'window.ytInitialData = {{"channelId":"{other}"}};'

    with pytest.raises(ChannelResolutionFailed, match="couldn't find a channel ID"):
        resolve_channel_query("youtube.com/c/Whoever", http=page_client(body=body))


def test_og_url_is_accepted_as_an_identity_tag():
    body = f'<meta property="og:url" content="https://www.youtube.com/channel/{CHANNEL_ID}">'
    assert resolve_channel_query("youtube.com/c/Thing", http=page_client(body=body)).value == CHANNEL_ID


def test_legacy_custom_reads_the_itemprop_meta_tag_too():
    body = f'<meta itemprop="identifier" content="{CHANNEL_ID}">'
    resolved = resolve_channel_query("youtube.com/c/Something", http=page_client(body=body))
    assert resolved.value == CHANNEL_ID


# -- the fallback failing ---------------------------------------------------


def test_legacy_custom_failure_asks_for_a_handle_or_id_when_page_is_missing():
    with pytest.raises(ChannelResolutionFailed) as excinfo:
        resolve_channel_query("youtube.com/c/GoneForever", http=page_client(status=404))

    message = str(excinfo.value)
    assert "no longer exists" in message
    assert "@handle" in message and "channel ID" in message


def test_legacy_custom_failure_when_the_page_has_no_channel_id():
    with pytest.raises(ChannelResolutionFailed) as excinfo:
        resolve_channel_query(
            "youtube.com/c/Reshuffled", http=page_client(body="<html>nothing useful</html>")
        )

    message = str(excinfo.value)
    assert "changed its page structure" in message
    assert "@handle" in message


def test_legacy_custom_failure_on_a_network_error():
    with pytest.raises(ChannelResolutionFailed) as excinfo:
        resolve_channel_query("youtube.com/c/Offline", http=page_client(raise_error=True))
    assert "@handle" in str(excinfo.value)


def test_legacy_custom_failure_on_an_unexpected_status():
    with pytest.raises(ChannelResolutionFailed, match="HTTP 503"):
        scrape_channel_id("https://www.youtube.com/c/Wobbly", http=page_client(status=503))


# -- odds and ends ----------------------------------------------------------


def test_bare_name_is_treated_as_a_handle():
    query = parse_channel_identifier("rickastley")
    assert query.param == "forHandle"
    assert query.value == "@rickastley"


def test_empty_identifier_is_rejected():
    with pytest.raises(ChannelResolutionFailed, match="No channel URL"):
        parse_channel_identifier("   ")


def test_unrecognised_youtube_url_is_rejected():
    with pytest.raises(ChannelResolutionFailed, match="Couldn't tell which channel"):
        parse_channel_identifier("https://www.youtube.com/playlist?list=PL123")


def test_youtube_url_with_no_path_is_rejected():
    with pytest.raises(ChannelResolutionFailed, match="doesn't point at a channel"):
        parse_channel_identifier("https://www.youtube.com/")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("youtube.com/c/Name", "https://www.youtube.com/c/Name"),
        ("youtube.com/user/Name", "https://www.youtube.com/user/Name"),
        ("@handle", "https://www.youtube.com/@handle"),
        (CHANNEL_ID, f"https://www.youtube.com/channel/{CHANNEL_ID}"),
    ],
)
def test_canonical_page_url_per_format(raw, expected):
    assert canonical_page_url(parse_channel_identifier(raw)) == expected


def test_resolve_passes_through_without_a_page_fetch_for_official_formats():
    # - page_client would raise if touched; official formats must not touch it.
    exploding = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(AssertionError("should not fetch"))
        )
    )
    assert resolve_channel_query("@rickastley", http=exploding).param == "forHandle"
    assert resolve_channel_query(CHANNEL_ID, http=exploding).param == "id"
