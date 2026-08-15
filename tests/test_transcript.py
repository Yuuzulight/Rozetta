"""watch_video: happy path, translation, and every documented failure mode.

The transcript library is stubbed out here. These tests are about how Rozetta
reacts to what it returns, not about YouTube's live behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from youtube_transcript_api import (
    AgeRestricted,
    NotTranslatable,
    RequestBlocked,
    TranscriptsDisabled,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeDataUnparsable,
)

from models import (
    NoTranscriptAvailable,
    TranscriptExtractionFailed,
    TranslationUnavailable,
    VideoNotAccessible,
)
from transcript import fetch_transcript

VIDEO_ID = "dQw4w9WgXcQ"
WATCH_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


# -- stubs ------------------------------------------------------------------


@dataclass
class FakeLanguage:
    language: str
    language_code: str


class FakeFetched:
    def __init__(self, segments, language_code, is_generated):
        self._segments = segments
        self.language_code = language_code
        self.is_generated = is_generated

    def to_raw_data(self):
        return list(self._segments)


class FakeTranscript:
    def __init__(
        self,
        language_code="en",
        is_generated=True,
        segments=None,
        translation_languages=(),
        fetch_error=None,
    ):
        self.language = language_code.upper()
        self.language_code = language_code
        self.is_generated = is_generated
        self.translation_languages = list(translation_languages)
        self._segments = segments if segments is not None else [
            {"text": "Never gonna", "start": 0.0, "duration": 1.5},
            {"text": "give you\nup", "start": 1.5, "duration": 1.5},
        ]
        self._fetch_error = fetch_error
        self.translated_to = None

    def translate(self, language_code):
        if language_code not in [t.language_code for t in self.translation_languages]:
            raise NotTranslatable(VIDEO_ID)
        out = FakeTranscript(
            language_code=language_code,
            is_generated=self.is_generated,
            segments=[{"text": "Nunca voy a", "start": 0.0, "duration": 1.5}],
        )
        out.translated_to = language_code
        return out

    def fetch(self):
        if self._fetch_error is not None:
            raise self._fetch_error
        return FakeFetched(self._segments, self.language_code, self.is_generated)


class FakeApi:
    """Stands in for YouTubeTranscriptApi."""

    def __init__(self, transcripts=None, list_error=None):
        self._transcripts = transcripts if transcripts is not None else [FakeTranscript()]
        self._list_error = list_error

    def list(self, video_id):
        if self._list_error is not None:
            raise self._list_error
        return list(self._transcripts)


# -- happy path -------------------------------------------------------------


def test_returns_full_text_with_timestamps_stripped():
    result = fetch_transcript(WATCH_URL, api=FakeApi())

    assert result.video_id == VIDEO_ID
    assert result.transcript_text == "Never gonna give you up"
    assert result.language == "en"
    assert result.was_translated is False


def test_returns_raw_segments_alongside_the_text():
    result = fetch_transcript(VIDEO_ID, api=FakeApi())

    assert result.transcript_segments[0] == {
        "text": "Never gonna",
        "start": 0.0,
        "duration": 1.5,
    }
    assert all({"text", "start", "duration"} <= set(s) for s in result.transcript_segments)


def test_auto_generated_flag_is_reported():
    auto = fetch_transcript(VIDEO_ID, api=FakeApi([FakeTranscript(is_generated=True)]))
    manual = fetch_transcript(VIDEO_ID, api=FakeApi([FakeTranscript(is_generated=False)]))

    assert auto.is_auto_generated is True
    assert manual.is_auto_generated is False


def test_human_written_captions_win_over_auto_generated_in_the_same_language():
    api = FakeApi(
        [
            FakeTranscript(language_code="en", is_generated=True),
            FakeTranscript(language_code="en", is_generated=False),
        ]
    )
    assert fetch_transcript(VIDEO_ID, api=api).is_auto_generated is False


def test_the_spoken_language_wins_over_a_human_written_translation():
    # - A well-subtitled video carries many manual tracks and YouTube does not
    #   list them original-first. The auto-generated track marks what's spoken.
    api = FakeApi(
        [
            FakeTranscript(language_code="ar", is_generated=False),
            FakeTranscript(language_code="es", is_generated=False),
            FakeTranscript(language_code="en", is_generated=True),
            FakeTranscript(language_code="en", is_generated=False),
        ]
    )
    result = fetch_transcript(VIDEO_ID, api=api)

    assert result.language == "en"
    assert result.is_auto_generated is False


def test_auto_captions_are_used_when_the_spoken_language_has_no_manual_track():
    api = FakeApi(
        [
            FakeTranscript(language_code="fr", is_generated=False),
            FakeTranscript(language_code="de", is_generated=True),
        ]
    )
    result = fetch_transcript(VIDEO_ID, api=api)

    assert result.language == "de"
    assert result.is_auto_generated is True


def test_first_manual_track_is_used_when_nothing_is_auto_generated():
    api = FakeApi(
        [
            FakeTranscript(language_code="ja", is_generated=False),
            FakeTranscript(language_code="ko", is_generated=False),
        ]
    )
    assert fetch_transcript(VIDEO_ID, api=api).language == "ja"


def test_response_carries_no_title_or_duration():
    result = fetch_transcript(VIDEO_ID, api=FakeApi())
    assert not hasattr(result, "title")
    assert not hasattr(result, "duration_seconds")


def test_blank_cues_do_not_leave_double_spaces():
    api = FakeApi(
        [
            FakeTranscript(
                segments=[
                    {"text": "hello", "start": 0.0, "duration": 1.0},
                    {"text": "  ", "start": 1.0, "duration": 1.0},
                    {"text": "world", "start": 2.0, "duration": 1.0},
                ]
            )
        ]
    )
    assert fetch_transcript(VIDEO_ID, api=api).transcript_text == "hello world"


# -- no captions ------------------------------------------------------------


def test_captions_disabled_reports_no_transcript_available():
    api = FakeApi(list_error=TranscriptsDisabled(VIDEO_ID))
    with pytest.raises(NoTranscriptAvailable) as excinfo:
        fetch_transcript(VIDEO_ID, api=api)

    assert "No transcript available" in str(excinfo.value)


def test_empty_caption_list_reports_no_transcript_available():
    with pytest.raises(NoTranscriptAvailable):
        fetch_transcript(VIDEO_ID, api=FakeApi(transcripts=[]))


# -- private / age-restricted ----------------------------------------------


def test_age_restricted_video_is_explicit_and_does_not_authenticate():
    api = FakeApi(list_error=AgeRestricted(VIDEO_ID))
    with pytest.raises(VideoNotAccessible) as excinfo:
        fetch_transcript(VIDEO_ID, api=api)

    message = str(excinfo.value)
    assert "age-restricted" in message
    assert "does not authenticate" in message


def test_private_video_is_explicit():
    api = FakeApi(list_error=VideoUnplayable(VIDEO_ID, "This video is private", []))
    with pytest.raises(VideoNotAccessible) as excinfo:
        fetch_transcript(VIDEO_ID, api=api)
    assert "private" in str(excinfo.value)


def test_unavailable_video_is_explicit():
    with pytest.raises(VideoNotAccessible):
        fetch_transcript(VIDEO_ID, api=FakeApi(list_error=VideoUnavailable(VIDEO_ID)))


# -- library / upstream failure --------------------------------------------


def test_blocked_request_is_reported_as_extraction_failure_not_missing_captions():
    api = FakeApi(list_error=RequestBlocked(VIDEO_ID))
    with pytest.raises(TranscriptExtractionFailed) as excinfo:
        fetch_transcript(VIDEO_ID, api=api)

    message = str(excinfo.value)
    assert "transcript extraction failed" in message
    assert "upstream YouTube change" in message
    # - The distinction that matters: this must not read as "no captions".
    assert "No transcript available" not in message


def test_unparsable_youtube_data_is_an_extraction_failure():
    api = FakeApi(list_error=YouTubeDataUnparsable(VIDEO_ID))
    with pytest.raises(TranscriptExtractionFailed):
        fetch_transcript(VIDEO_ID, api=api)


def test_an_unexpected_library_exception_is_an_extraction_failure():
    api = FakeApi(list_error=AttributeError("library internals moved"))
    with pytest.raises(TranscriptExtractionFailed) as excinfo:
        fetch_transcript(VIDEO_ID, api=api)
    assert "AttributeError" in str(excinfo.value)


def test_failure_during_fetch_is_an_extraction_failure():
    api = FakeApi([FakeTranscript(fetch_error=YouTubeDataUnparsable(VIDEO_ID))])
    with pytest.raises(TranscriptExtractionFailed):
        fetch_transcript(VIDEO_ID, api=api)


def test_an_invalid_video_id_is_reported_as_such_not_as_an_upstream_break():
    from youtube_transcript_api import InvalidVideoId

    from models import InvalidVideoIdentifier

    api = FakeApi(list_error=InvalidVideoId(VIDEO_ID))
    with pytest.raises(InvalidVideoIdentifier):
        fetch_transcript(VIDEO_ID, api=api)


def test_a_transcript_list_that_breaks_while_iterating_is_an_extraction_failure():
    class ExplodingList:
        def __iter__(self):
            raise RuntimeError("caption list moved")

    class ExplodingApi:
        def list(self, video_id):
            return ExplodingList()

    with pytest.raises(TranscriptExtractionFailed):
        fetch_transcript(VIDEO_ID, api=ExplodingApi())


def test_no_captions_and_extraction_failure_are_different_types():
    assert not issubclass(TranscriptExtractionFailed, NoTranscriptAvailable)
    assert not issubclass(NoTranscriptAvailable, TranscriptExtractionFailed)


# -- translation ------------------------------------------------------------


def test_translation_uses_youtubes_own_mechanism():
    api = FakeApi(
        [
            FakeTranscript(
                language_code="en",
                translation_languages=[FakeLanguage("Spanish", "es")],
            )
        ]
    )
    result = fetch_transcript(VIDEO_ID, target_language="es", api=api)

    assert result.was_translated is True
    assert result.language == "es"
    assert result.transcript_text == "Nunca voy a"


def test_a_real_caption_track_beats_a_machine_translation():
    api = FakeApi(
        [
            FakeTranscript(language_code="en", translation_languages=[FakeLanguage("Spanish", "es")]),
            FakeTranscript(
                language_code="es",
                is_generated=False,
                segments=[{"text": "subtítulos reales", "start": 0.0, "duration": 1.0}],
            ),
        ]
    )
    result = fetch_transcript(VIDEO_ID, target_language="es", api=api)

    assert result.was_translated is False
    assert result.transcript_text == "subtítulos reales"


def test_target_matching_the_native_language_is_not_a_translation():
    api = FakeApi([FakeTranscript(language_code="en")])
    result = fetch_transcript(VIDEO_ID, target_language="EN", api=api)

    assert result.was_translated is False
    assert result.language == "en"


def test_unavailable_translation_lists_what_is_available_instead():
    api = FakeApi(
        [
            FakeTranscript(
                language_code="en",
                translation_languages=[
                    FakeLanguage("Spanish", "es"),
                    FakeLanguage("French", "fr"),
                ],
            )
        ]
    )
    with pytest.raises(TranslationUnavailable) as excinfo:
        fetch_transcript(VIDEO_ID, target_language="ja", api=api)

    message = str(excinfo.value)
    assert "'ja'" in message
    assert "es (Spanish)" in message
    assert "fr (French)" in message
    assert "natively in: en" in message


def test_untranslatable_transcript_says_so_plainly():
    api = FakeApi([FakeTranscript(language_code="en", translation_languages=[])])
    with pytest.raises(TranslationUnavailable) as excinfo:
        fetch_transcript(VIDEO_ID, target_language="es", api=api)
    assert "can't be translated into any language" in str(excinfo.value)


def test_unavailable_translation_never_returns_the_original_silently():
    api = FakeApi([FakeTranscript(language_code="en", translation_languages=[])])
    with pytest.raises(TranslationUnavailable):
        # - The failure IS the assertion: a returned English transcript here
        #   would be the silent-wrong-language bug this guards against.
        fetch_transcript(VIDEO_ID, target_language="de", api=api)


def test_translate_rejecting_the_code_still_surfaces_as_translation_unavailable(monkeypatch):
    transcript = FakeTranscript(
        language_code="en", translation_languages=[FakeLanguage("German", "de")]
    )
    monkeypatch.setattr(
        transcript, "translate", lambda code: (_ for _ in ()).throw(NotTranslatable(VIDEO_ID))
    )
    with pytest.raises(TranslationUnavailable):
        fetch_transcript(VIDEO_ID, target_language="de", api=FakeApi([transcript]))


def test_translate_blowing_up_unexpectedly_is_an_extraction_failure(monkeypatch):
    transcript = FakeTranscript(
        language_code="en", translation_languages=[FakeLanguage("German", "de")]
    )
    monkeypatch.setattr(
        transcript, "translate", lambda code: (_ for _ in ()).throw(RequestBlocked(VIDEO_ID))
    )
    with pytest.raises(TranscriptExtractionFailed):
        fetch_transcript(VIDEO_ID, target_language="de", api=FakeApi([transcript]))


# -- no length cap ----------------------------------------------------------


def test_long_transcripts_are_returned_whole():
    segments = [{"text": f"word{i}", "start": float(i), "duration": 1.0} for i in range(20_000)]
    api = FakeApi([FakeTranscript(segments=segments)])

    result = fetch_transcript(VIDEO_ID, api=api)

    assert len(result.transcript_segments) == 20_000
    assert result.transcript_text.endswith("word19999")
    assert "…" not in result.transcript_text
