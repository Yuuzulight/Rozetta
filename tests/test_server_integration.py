"""End-to-end through the MCP layer: registration, schemas, and tool dispatch.

These go through server.call_tool rather than calling the functions directly, so
they cover the wiring an MCP client actually sees.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import stats as stats_module
import transcript as transcript_module
from conftest import FakeDataAPI, channel_item, configure_fake_api, playlist_items, video_item
from server import load_dotenv, server
from test_transcript import FakeApi, FakeLanguage, FakeTranscript

VIDEO_ID = "dQw4w9WgXcQ"

EXPECTED_TOOLS = {"get_video_info", "watch_video", "get_video_stats", "get_channel_stats"}


def run(coro):
    return asyncio.run(coro)


def tools():
    return {tool.name: tool for tool in run(server.list_tools())}


def call(name: str, arguments: dict):
    return run(server.call_tool(name, arguments))


def use_fake_transcript_api(monkeypatch, transcripts=None, list_error=None):
    monkeypatch.setattr(
        transcript_module,
        "YouTubeTranscriptApi",
        lambda *args, **kwargs: FakeApi(transcripts=transcripts, list_error=list_error),
    )


# -- registration -----------------------------------------------------------


def test_all_four_tools_are_registered():
    assert set(tools()) == EXPECTED_TOOLS


def test_every_tool_has_a_description_and_an_output_schema():
    for name, tool in tools().items():
        assert tool.description, f"{name} has no description"
        assert tool.output_schema, f"{name} has no output schema"


def test_get_video_info_tells_the_model_to_call_it_first():
    description = tools()["get_video_info"].description
    assert "BEFORE watch_video" in description
    assert "confirm" in description.lower()


def test_the_ordering_convention_is_advice_not_enforcement(monkeypatch, tracker):
    # - watch_video must work on its own; nothing tracks whether get_video_info ran.
    use_fake_transcript_api(monkeypatch)

    result = call("watch_video", {"url_or_id": VIDEO_ID})

    assert result.is_error is False
    assert result.structured_content["transcript_text"] == "Never gonna give you up"


def test_watch_video_advertises_the_two_distinct_failure_modes():
    description = tools()["watch_video"].description
    assert "no transcript available" in description
    assert "transcript extraction failed" in description


def test_server_instructions_carry_the_two_step_convention():
    assert "get_video_info first" in server.instructions


# -- credentials stay out of the schemas ------------------------------------


def test_no_tool_accepts_an_api_key_argument():
    for name, tool in tools().items():
        properties = set(tool.input_schema.get("properties", {}))
        assert not any("key" in p.lower() for p in properties), f"{name} exposes {properties}"
        assert not any("token" in p.lower() or "secret" in p.lower() for p in properties)


def test_the_api_key_appears_nowhere_in_any_serialised_schema(monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "super-secret-value")

    blob = json.dumps([t.model_dump(mode="json") for t in run(server.list_tools())])

    assert "super-secret-value" not in blob
    assert "YOUTUBE_API_KEY" not in json.dumps(
        [t.input_schema for t in run(server.list_tools())]
    )


def test_tool_inputs_are_exactly_the_documented_arguments():
    schemas = {name: set(t.input_schema.get("properties", {})) for name, t in tools().items()}

    assert schemas["get_video_info"] == {"url_or_id"}
    assert schemas["watch_video"] == {"url_or_id", "target_language"}
    assert schemas["get_video_stats"] == {"url_or_id"}
    assert schemas["get_channel_stats"] == {"channel_url_or_id"}


# -- dispatch ---------------------------------------------------------------


def test_get_video_info_end_to_end(tracker):
    configure_fake_api(FakeDataAPI({"videos": {"items": [video_item()]}}), tracker)

    result = call("get_video_info", {"url_or_id": f"https://youtu.be/{VIDEO_ID}"})

    assert result.is_error is False
    assert result.structured_content["title"] == "Test Video"
    assert result.structured_content["duration_seconds"] == 253


def test_get_video_stats_end_to_end(tracker):
    configure_fake_api(FakeDataAPI({"videos": {"items": [video_item()]}}), tracker)

    result = call("get_video_stats", {"url_or_id": VIDEO_ID})

    assert result.structured_content["view_count"] == 1_500_000_000
    assert result.structured_content["tags"] == ["music", "test"]


def test_get_channel_stats_end_to_end(tracker):
    configure_fake_api(
        FakeDataAPI(
            {
                "channels": {"items": [channel_item()]},
                "playlistItems": playlist_items([]),
            }
        ),
        tracker,
    )

    result = call("get_channel_stats", {"channel_url_or_id": "@testchannel"})

    assert result.structured_content["subscriber_count"] == 12_300_000


def test_watch_video_translation_end_to_end(monkeypatch):
    use_fake_transcript_api(
        monkeypatch,
        transcripts=[
            FakeTranscript(
                language_code="en", translation_languages=[FakeLanguage("Spanish", "es")]
            )
        ],
    )

    result = call("watch_video", {"url_or_id": VIDEO_ID, "target_language": "es"})

    assert result.structured_content["was_translated"] is True
    assert result.structured_content["language"] == "es"


def test_watch_video_needs_no_api_key(monkeypatch, tracker):
    # - No key configured at all; transcripts don't go through the Data API.
    stats_module.configure(None, tracker=tracker)
    use_fake_transcript_api(monkeypatch)

    assert call("watch_video", {"url_or_id": VIDEO_ID}).is_error is False
    assert tracker.used_today() == 0


# -- errors reach the client with their message intact ----------------------


def test_a_bad_url_surfaces_as_a_tool_error():
    with pytest.raises(ToolError, match="Could not find a YouTube video ID"):
        call("get_video_info", {"url_or_id": "https://vimeo.com/12345"})


def test_a_missing_key_surfaces_as_a_tool_error(tracker):
    stats_module.configure(None, tracker=tracker)

    with pytest.raises(ToolError, match="YOUTUBE_API_KEY is not set"):
        call("get_video_stats", {"url_or_id": VIDEO_ID})


def test_no_captions_surfaces_with_its_own_wording(monkeypatch):
    from youtube_transcript_api import TranscriptsDisabled

    use_fake_transcript_api(monkeypatch, list_error=TranscriptsDisabled(VIDEO_ID))

    with pytest.raises(ToolError, match="No transcript available"):
        call("watch_video", {"url_or_id": VIDEO_ID})


def test_extraction_failure_surfaces_with_different_wording(monkeypatch):
    from youtube_transcript_api import RequestBlocked

    use_fake_transcript_api(monkeypatch, list_error=RequestBlocked(VIDEO_ID))

    with pytest.raises(ToolError, match="upstream YouTube change"):
        call("watch_video", {"url_or_id": VIDEO_ID})


# -- .env handling ----------------------------------------------------------


def test_dotenv_fills_in_a_missing_variable(tmp_path, monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text('# comment\nYOUTUBE_API_KEY="from-file"\n\n', encoding="utf-8")

    load_dotenv(env_file)

    import os

    assert os.environ["YOUTUBE_API_KEY"] == "from-file"


def test_dotenv_never_overrides_what_the_client_passed_in(tmp_path, monkeypatch):
    monkeypatch.setenv("YOUTUBE_API_KEY", "from-client")
    env_file = tmp_path / ".env"
    env_file.write_text("YOUTUBE_API_KEY=from-file\n", encoding="utf-8")

    load_dotenv(env_file)

    import os

    assert os.environ["YOUTUBE_API_KEY"] == "from-client"


def test_a_missing_dotenv_file_is_not_an_error(tmp_path):
    load_dotenv(tmp_path / "nope.env")
