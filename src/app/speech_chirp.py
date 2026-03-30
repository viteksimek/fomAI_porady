"""Přepis přes Cloud Speech-to-Text v2 — model Chirp 3 (multiregion us | eu)."""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from pathlib import PurePosixPath
from urllib.parse import unquote

from google.api_core.client_options import ClientOptions
from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import cloud_speech

logger = logging.getLogger(__name__)

# Diarizace u Chirp 3 v BatchRecognize jen pro vybrané locale (dokumentace Google).
_CHIRP_DIARIZATION_LOCALES: frozenset[str] = frozenset(
    {
        "cmn-hans-cn",
        "de-de",
        "en-gb",
        "en-in",
        "en-us",
        "es-es",
        "es-us",
        "fr-ca",
        "fr-fr",
        "hi-in",
        "it-it",
        "ja-jp",
        "ko-kr",
        "pt-br",
    }
)

_ISO2_TO_BCP47: dict[str, str] = {
    "cs": "cs-CZ",
    "en": "en-US",
    "de": "de-DE",
    "sk": "sk-SK",
    "pl": "pl-PL",
    "fr": "fr-FR",
    "es": "es-ES",
    "it": "it-IT",
    "uk": "uk-UA",
}

# S enable_word_time_offsets u BatchRecognize + chirp platí cca 20 min limit — držíme rezervu.
_WORD_LEVEL_MAX_SECONDS: int = 19 * 60

# Hint z názvu souboru před příponou (priorita: první shoda).
_SPEAKER_FILENAME_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"_s(\d+)(?=\.[^.]+$)", re.IGNORECASE),
    re.compile(r"_(\d+)\s*mluvcich(?=\.[^.]+$)", re.IGNORECASE),
    re.compile(r"_(\d+)-mluvcich(?=\.[^.]+$)", re.IGNORECASE),
)

_SPEAKER_COUNT_MIN = 1
_SPEAKER_COUNT_MAX = 32


def parse_speaker_count_from_filename(filename: str) -> int | None:
    """Vytáhne počet mluvčích z názvu, např. ``porada_s4.m4a`` → 4, ``zapis_3mluvcich.m4a`` → 3."""
    base = PurePosixPath(unquote(filename)).name
    for pat in _SPEAKER_FILENAME_PATTERNS:
        m = pat.search(base)
        if m:
            n = int(m.group(1))
            if _SPEAKER_COUNT_MIN <= n <= _SPEAKER_COUNT_MAX:
                return n
    return None


def parse_speaker_count_from_gcs_uri(gcs_uri: str) -> int | None:
    """Poslední segment cesty gs://…/file.m4a."""
    part = unquote(gcs_uri.rstrip("/").rsplit("/", 1)[-1])
    return parse_speaker_count_from_filename(part)


def _norm_locale(code: str) -> str:
    return code.strip().replace("_", "-").lower()


def language_codes_for_chirp(language_hint: str) -> list[str]:
    h = (language_hint or "cs").strip().lower()
    if h in ("auto", "mul", "multi", "automatic"):
        return ["auto"]
    if len(h) == 2 and h.isalpha():
        return [_ISO2_TO_BCP47.get(h, "cs-CZ")]
    if re.match(r"^[a-z]{2}-[a-z0-9-]+$", h, re.I):
        parts = h.split("-", 1)
        return [f"{parts[0].lower()}-{parts[1].upper()}"] if len(parts[1]) == 2 else [h]
    return ["cs-CZ"]


def _format_ts(offset: timedelta | None) -> str:
    if offset is None:
        return ""
    total = max(0, int(offset.total_seconds()))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _segments_from_words(words: list, language_display: str) -> list[dict[str, str]]:
    segments: list[dict[str, str]] = []
    cur_spk: str | None = None
    cur_start: timedelta | None = None
    cur_tokens: list[str] = []

    for w in words:
        token = (w.word or "").strip()
        if not token:
            continue
        label = (w.speaker_label or "1").strip() or "1"
        spk = f"Mluvčí {label}"
        if cur_spk is None:
            cur_spk = spk
            cur_start = w.start_offset
        if spk != cur_spk and cur_tokens:
            text = " ".join(cur_tokens).strip()
            if text:
                segments.append(
                    {
                        "timestamp": _format_ts(cur_start),
                        "speaker": cur_spk,
                        "text": text,
                        "language": language_display,
                    }
                )
            cur_spk = spk
            cur_start = w.start_offset
            cur_tokens = []
        cur_tokens.append(token)

    if cur_tokens and cur_spk:
        text = " ".join(cur_tokens).strip()
        if text:
            segments.append(
                {
                    "timestamp": _format_ts(cur_start),
                    "speaker": cur_spk,
                    "text": text,
                    "language": language_display,
                }
            )
    return segments


def _stt_results_to_transcript_dict(
    stt_results: list,
    *,
    language_display: str,
) -> dict:
    segments: list[dict[str, str]] = []
    for res in stt_results:
        if not res.alternatives:
            continue
        alt = res.alternatives[0]
        text = (alt.transcript or "").strip()
        if not text:
            continue
        ts = _format_ts(res.result_end_offset) if res.result_end_offset else ""
        if alt.words:
            word_segs = _segments_from_words(alt.words, language_display)
            if word_segs:
                segments.extend(word_segs)
                continue
        segments.append(
            {
                "timestamp": ts,
                "speaker": "Mluvčí 1",
                "text": text,
                "language": language_display,
            }
        )

    flat = " ".join(s.get("text", "") for s in segments).strip()
    summary = (flat[:900] + "…") if len(flat) > 900 else flat
    if not summary and segments:
        summary = segments[0].get("text", "")
    return {"summary": summary or "(prázdný přepis)", "segments": segments}


def transcribe_gcs_chirp3(
    *,
    gcs_uri: str,
    language_hint: str,
    project_id: str,
    speech_region: str,
    duration_seconds: float,
    speaker_count: int | None = None,
) -> dict:
    """Spustí BatchRecognize na gs://… vrací dict ve tvaru TRANSCRIPT_SCHEMA."""
    codes = language_codes_for_chirp(language_hint)
    lang_disp = codes[0] if codes != ["auto"] else "auto"

    can_diarize = (
        codes != ["auto"]
        and _norm_locale(codes[0]) in _CHIRP_DIARIZATION_LOCALES
        and duration_seconds <= _WORD_LEVEL_MAX_SECONDS
    )

    features = cloud_speech.RecognitionFeatures()
    if can_diarize:
        if speaker_count is not None:
            c = min(max(speaker_count, _SPEAKER_COUNT_MIN), _SPEAKER_COUNT_MAX)
            features.diarization_config = cloud_speech.SpeakerDiarizationConfig(
                min_speaker_count=1,
                max_speaker_count=c,
            )
        else:
            features.diarization_config = cloud_speech.SpeakerDiarizationConfig(
                min_speaker_count=1,
                max_speaker_count=8,
            )
        features.enable_word_time_offsets = True

    config = cloud_speech.RecognitionConfig(
        auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
        language_codes=codes,
        model="chirp_3",
        features=features,
    )
    meta = cloud_speech.BatchRecognizeFileMetadata(uri=gcs_uri)
    request = cloud_speech.BatchRecognizeRequest(
        recognizer=f"projects/{project_id}/locations/{speech_region}/recognizers/_",
        config=config,
        files=[meta],
        recognition_output_config=cloud_speech.RecognitionOutputConfig(
            inline_response_config=cloud_speech.InlineOutputConfig(),
        ),
    )

    endpoint = f"{speech_region}-speech.googleapis.com"
    client = SpeechClient(client_options=ClientOptions(api_endpoint=endpoint))

    timeout = min(10_800, max(600, int(duration_seconds * 4) + 600))
    logger.info(
        "Chirp 3 batch: uri=%s region=%s timeout=%ss speaker_count=%s",
        gcs_uri,
        speech_region,
        timeout,
        speaker_count,
    )

    operation = client.batch_recognize(request=request)
    response = operation.result(timeout=timeout)

    file_res = response.results.get(gcs_uri)
    if file_res is None:
        raise RuntimeError(f"BatchRecognize: chybí výsledek pro {gcs_uri}")
    if file_res.error and file_res.error.code:
        raise RuntimeError(f"Speech API error: {file_res.error.code} {file_res.error.message}")

    transcript_pb = file_res.transcript
    if not transcript_pb or not transcript_pb.results:
        raise RuntimeError("Chirp 3: prázdný přepis")

    return _stt_results_to_transcript_dict(list(transcript_pb.results), language_display=lang_disp)
