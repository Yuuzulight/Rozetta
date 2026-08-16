"""The watch_video tool: transcript retrieval, optionally translated.

There is no official Google endpoint that hands a third party the captions of an
arbitrary public video — the Captions API needs OAuth as the video's own owner,
which rules it out for this use case entirely. So this leans on
youtube-transcript-api, which reads the same caption tracks the web player uses.

That library is unofficial, which means it can break when YouTube changes
something on their side. The failure handling below exists to keep that case
distinguishable from "this video has no captions", because the two look similar
from the outside and call for completely different reactions.

There is deliberately no fallback extraction path. Scraping the watch page by
hand would be the same category of unofficial and would break for much the same
reasons — more code, no more reliability.
"""

from __future__ import annotations

from youtube_transcript_api import (
    AgeRestricted,
    InvalidVideoId,
    NoTranscriptFound,
    NotTranslatable,
    PoTokenRequired,
    RequestBlocked,
    TranscriptsDisabled,
    TranslationLanguageNotAvailable,
    VideoUnavailable,
    VideoUnplayable,
    YouTubeDataUnparsable,
    YouTubeRequestFailed,
    YouTubeTranscriptApi,
)

from models import (
    InvalidVideoIdentifier,
    NoTranscriptAvailable,
    TranscriptExtractionFailed,
    TranscriptResult,
    TranslationUnavailable,
    VideoNotAccessible,
    extract_video_id,
)

_UPSTREAM_HINT = (
    "transcript extraction failed — may indicate an upstream YouTube change, "
    "not a problem with this specific video"
)


def _map_library_error(exc: Exception, video_id: str) -> Exception:
    """Translate youtube-transcript-api's exceptions into Rozetta's.

    The important line here is the one between NoTranscriptAvailable (a fact
    about the video) and TranscriptExtractionFailed (a fact about the world).
    """
    if isinstance(exc, (TranscriptsDisabled, NoTranscriptFound)):
        return NoTranscriptAvailable(
            f"No transcript available for video {video_id}. The uploader has not "
            "provided captions and YouTube has not auto-generated any."
        )

    if isinstance(exc, AgeRestricted):
        return VideoNotAccessible(
            f"Video {video_id} is age-restricted, so its captions can't be read "
            "without a signed-in account. Rozetta only handles public videos and "
            "does not authenticate."
        )

    if isinstance(exc, (VideoUnplayable, VideoUnavailable)):
        return VideoNotAccessible(
            f"Video {video_id} is unavailable — it may be private, deleted, or "
            "region-blocked. Rozetta only handles public videos and does not "
            "authenticate."
        )

    if isinstance(exc, InvalidVideoId):
        return InvalidVideoIdentifier(f"{video_id!r} is not a valid YouTube video ID.")

    if isinstance(
        exc,
        (RequestBlocked, PoTokenRequired, YouTubeRequestFailed, YouTubeDataUnparsable),
    ):
        return TranscriptExtractionFailed(
            f"{_UPSTREAM_HINT}. YouTube blocked or malformed the request for "
            f"video {video_id} ({type(exc).__name__})."
        )

    # - Any other CouldNotRetrieveTranscript, or anything unexpected at all, is
    #   an upstream problem rather than a statement about the video.
    return TranscriptExtractionFailed(
        f"{_UPSTREAM_HINT}. Underlying error for video {video_id}: "
        f"{type(exc).__name__}: {exc}"
    )


def _pick_default_transcript(available: list):
    """Choose the track to use when the caller didn't ask for a language.

    What we want is the language actually spoken in the video. That's harder
    than it sounds: a popular video can carry a dozen human-written caption
    tracks, and YouTube doesn't list them original-first — asking for "the
    transcript" of a well-subtitled English video can hand back Arabic.

    The tell is the auto-generated track. YouTube only ever generates captions
    by transcribing the audio, so its language is the spoken one. So: prefer a
    human-written track in that language, fall back to the auto-generated track
    itself, and only if nothing is auto-generated fall back to first-listed.
    """
    generated = [t for t in available if t.is_generated]
    manual = [t for t in available if not t.is_generated]

    if generated:
        spoken = generated[0].language_code
        for track in manual:
            if track.language_code.lower() == spoken.lower():
                return track
        return generated[0]

    return manual[0] if manual else available[0]


def _match_language(code: str, candidates: list[str]) -> str | None:
    """Case-insensitive language-code match, returning YouTube's own spelling."""
    wanted = code.strip().lower()
    for candidate in candidates:
        if candidate.lower() == wanted:
            return candidate
    return None


def fetch_transcript(
    url_or_id: str,
    target_language: str | None = None,
    api: YouTubeTranscriptApi | None = None,
) -> TranscriptResult:
    video_id = extract_video_id(url_or_id)
    api = api or YouTubeTranscriptApi()

    try:
        transcript_list = api.list(video_id)
    except Exception as exc:
        raise _map_library_error(exc, video_id) from exc

    try:
        available = list(transcript_list)
    except Exception as exc:
        raise _map_library_error(exc, video_id) from exc

    if not available:
        raise NoTranscriptAvailable(
            f"No transcript available for video {video_id}. YouTube lists no "
            "caption tracks for it."
        )

    chosen = _pick_default_transcript(available)
    was_translated = False

    if target_language:
        chosen, was_translated = _apply_target_language(
            chosen, available, target_language, video_id
        )

    try:
        fetched = chosen.fetch()
    except Exception as exc:
        raise _map_library_error(exc, video_id) from exc

    segments = fetched.to_raw_data()
    # - Caption cues carry stray newlines; collapse them so the text reads as prose.
    text = " ".join(
        " ".join(str(segment.get("text", "")).split())
        for segment in segments
        if str(segment.get("text", "")).strip()
    )

    return TranscriptResult(
        video_id=video_id,
        transcript_text=text,
        transcript_segments=segments,
        language=fetched.language_code,
        is_auto_generated=bool(fetched.is_generated),
        was_translated=was_translated,
    )


def _apply_target_language(
    chosen,
    available: list,
    target_language: str,
    video_id: str,
):
    """Pick or produce a transcript in the requested language.

    A real caption track in the target language always wins over a machine
    translation of another one. Only when no such track exists do we ask YouTube
    to translate, using the library's own .translate() — that is YouTube's
    translation, not a second dependency and not an LLM step.
    """
    native_codes = [t.language_code for t in available]
    direct = _match_language(target_language, native_codes)
    if direct is not None:
        return next(t for t in available if t.language_code == direct), False

    translation_languages = list(getattr(chosen, "translation_languages", []) or [])
    translatable_codes = [t.language_code for t in translation_languages]
    matched = _match_language(target_language, translatable_codes)

    if matched is None:
        raise TranslationUnavailable(
            _unavailable_message(
                target_language, video_id, native_codes, translation_languages
            )
        )

    try:
        translated = chosen.translate(matched)
    except (NotTranslatable, TranslationLanguageNotAvailable) as exc:
        raise TranslationUnavailable(
            _unavailable_message(
                target_language, video_id, native_codes, translation_languages
            )
        ) from exc
    except Exception as exc:
        raise _translation_failure(exc, video_id, target_language, native_codes) from exc

    return translated, True


def _translation_failure(
    exc: Exception, video_id: str, target_language: str, native_codes: list[str]
) -> Exception:
    """Explain a blocked translation, and point at the tracks that would work.

    Measured: plain caption fetches succeed from the same address seconds
    before and after a translation request is refused, so YouTube gates the
    translated-caption endpoint far harder than the normal one. The generic
    upstream message is true but useless here — a caller told only "try later"
    will retry into the same wall, when the video usually has real caption
    tracks that fetch fine.
    """
    mapped = _map_library_error(exc, video_id)
    if not isinstance(mapped, TranscriptExtractionFailed):
        return mapped

    alternatives = ", ".join(native_codes) or "none"
    return TranscriptExtractionFailed(
        f"{_UPSTREAM_HINT}. YouTube refused to translate video {video_id} into "
        f"{target_language!r} ({type(exc).__name__}). Translated captions are rate-limited "
        "much more aggressively than ordinary ones, so this often fails while plain "
        f"transcript requests still work. Real caption tracks exist for: {alternatives}. "
        "Requesting one of those, or omitting target_language, will usually succeed."
    )


def _unavailable_message(
    target_language: str,
    video_id: str,
    native_codes: list[str],
    translation_languages: list,
) -> str:
    if translation_languages:
        listed = ", ".join(
            f"{t.language_code} ({t.language})" for t in translation_languages
        )
        offer = f"Available translation targets: {listed}."
    else:
        offer = "This transcript can't be translated into any language."

    return (
        f"Transcript for video {video_id} is not available in {target_language!r}. "
        f"Captions exist natively in: {', '.join(native_codes) or 'none'}. {offer} "
        "Call watch_video again without target_language to get the original."
    )
