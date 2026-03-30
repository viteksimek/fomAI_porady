from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import vertexai
from vertexai.generative_models import GenerationConfig, GenerativeModel, HarmBlockThreshold, HarmCategory, Part

from app.jobs_store import get_jobs_store_resolved
from app.models import JobStatus
from app.settings import get_settings
from app.speech_chirp import (
    CHIRP_BATCH_CHUNK_SECONDS,
    CHIRP_BATCH_SINGLE_FILE_MAX_SECONDS,
    merge_chirp_transcript_partials,
    parse_speaker_count_from_gcs_uri,
    transcribe_gcs_chirp3,
)
from app.storage import GcsStorage

logger = logging.getLogger(__name__)


def _resolved_speaker_count(options: dict[str, Any], input_gcs_uri: str) -> int | None:
    """options.speaker_count má přednost; jinak hint z názvu objektu v gs:// (např. *_s3.m4a)."""
    raw = options.get("speaker_count")
    if raw is not None:
        try:
            n = int(raw)
        except (TypeError, ValueError):
            n = -1
        if 1 <= n <= 32:
            return n
    return parse_speaker_count_from_gcs_uri(input_gcs_uri)

TRANSCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": "Stručný souhrn obsahu nahrávky v češtině.",
        },
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "timestamp": {
                        "type": "string",
                        "description": "Čas ve formátu MM:SS nebo HH:MM:SS od začátku.",
                    },
                    "speaker": {
                        "type": "string",
                        "description": "Označení mluvčího (např. Mluvčí 1).",
                    },
                    "text": {"type": "string", "description": "Přepsaný text segmentu."},
                    "language": {"type": "string", "description": "ISO kód nebo název jazyka segmentu."},
                },
                "required": ["timestamp", "speaker", "text"],
            },
        },
    },
    "required": ["summary", "segments"],
}

MINUTES_SCHEMA = {
    "type": "object",
    "properties": {
        "executive_summary": {
            "type": "string",
            "description": "Stručné shrnutí pro vedení v češtině.",
        },
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "bullet_points": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["title", "bullet_points"],
            },
        },
        "decisions": {"type": "array", "items": {"type": "string"}},
        "action_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "owner": {"type": "string"},
                    "due_date": {"type": "string", "description": "Datum nebo prázdné pokud neznámé."},
                },
                "required": ["title"],
            },
        },
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "executive_summary",
        "topics",
        "decisions",
        "action_items",
        "open_questions",
    ],
}


def _ffprobe_duration_seconds(path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    r = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return float((r.stdout or "0").strip() or 0.0)


def _run_ffmpeg_normalize(src: Path, dest: Path) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-ac",
        "1",
        "-ar",
        "44100",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(dest),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


def _ffmpeg_extract_mp3_segment(src: Path, dest: Path, start_sec: float, duration_sec: float) -> None:
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        str(start_sec),
        "-i",
        str(src),
        "-t",
        str(duration_sec),
        "-ac",
        "1",
        "-ar",
        "44100",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(dest),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


async def _transcribe_with_chirp_maybe_chunked(
    *,
    job_id: str,
    tmp_path: Path,
    norm_path: Path,
    normalized_uri: str,
    duration_sec: float,
    lang: str,
    speaker_hint: int | None,
    gcs: GcsStorage,
    google_cloud_project: str,
    speech_region: str,
) -> dict:
    """BatchRecognize má limit ~60 min na soubor — delší audio rozdělíme a přepisy spojíme."""
    if duration_sec <= CHIRP_BATCH_SINGLE_FILE_MAX_SECONDS:
        return await asyncio.to_thread(
            transcribe_gcs_chirp3,
            gcs_uri=normalized_uri,
            language_hint=lang,
            project_id=google_cloud_project,
            speech_region=speech_region,
            duration_seconds=duration_sec,
            speaker_count=speaker_hint,
        )

    logger.info(
        "Chirp: délka %.0f s přesahuje %.0f s — batch po %.0f s (job %s)",
        duration_sec,
        CHIRP_BATCH_SINGLE_FILE_MAX_SECONDS,
        CHIRP_BATCH_CHUNK_SECONDS,
        job_id,
    )
    partials: list[tuple[float, dict]] = []
    start = 0.0
    chunk_idx = 0
    while start < duration_sec:
        chunk_dur = min(CHIRP_BATCH_CHUNK_SECONDS, duration_sec - start)
        chunk_path = tmp_path / f"chirp_chunk_{chunk_idx:03d}.mp3"
        await asyncio.to_thread(_ffmpeg_extract_mp3_segment, norm_path, chunk_path, start, chunk_dur)
        chunk_uri = gcs.job_chirp_chunk_uri(job_id, chunk_idx)
        await gcs.upload_file(
            local_path=chunk_path,
            gcs_uri=chunk_uri,
            content_type="audio/mpeg",
        )
        partial = await asyncio.to_thread(
            transcribe_gcs_chirp3,
            gcs_uri=chunk_uri,
            language_hint=lang,
            project_id=google_cloud_project,
            speech_region=speech_region,
            duration_seconds=chunk_dur,
            speaker_count=speaker_hint,
        )
        partials.append((start, partial))
        start += chunk_dur
        chunk_idx += 1

    return merge_chirp_transcript_partials(partials)


def _vertex_generate(
    *,
    model_name: str,
    project: str,
    location: str,
    parts: list,
    system_instruction: str | None,
    response_schema: dict,
) -> str:
    vertexai.init(project=project, location=location)
    model = GenerativeModel(model_name, system_instruction=system_instruction)
    safety = {
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_ONLY_HIGH,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_ONLY_HIGH,
    }
    gen_cfg = GenerationConfig(
        response_mime_type="application/json",
        response_schema=response_schema,
    )
    resp = model.generate_content(
        parts,
        generation_config=gen_cfg,
        safety_settings=safety,
    )
    if not resp.candidates:
        raise RuntimeError("Vertex response has no candidates")
    text = resp.text
    if not text:
        raise RuntimeError("Empty model response text")
    return text


async def process_job(job_id: str) -> None:
    settings = get_settings()
    store = get_jobs_store_resolved()
    gcs = GcsStorage()

    job = await store.get_job(job_id)
    if not job:
        logger.error("Job not found: %s", job_id)
        return
    input_uri = job.get("input_gcs_uri")
    if not input_uri:
        await store.update_job(
            job_id,
            {"status": JobStatus.failed.value, "error": "Missing input_gcs_uri"},
        )
        return

    options = job.get("options") or {}
    lang = (options.get("language_hint") or "cs") or "cs"
    speaker_hint = _resolved_speaker_count(options, input_uri)

    await store.update_job(job_id, {"status": JobStatus.processing.value, "error": None})

    try:
        if not settings.google_cloud_project or not settings.gcs_bucket:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT and GCS_BUCKET are required for processing")

        transcript_uri = gcs.job_transcript_uri(job_id)
        minutes_uri = gcs.job_minutes_uri(job_id)
        normalized_uri = gcs.job_normalized_uri(job_id)

        with tempfile.TemporaryDirectory(prefix=f"job-{job_id}-") as tmp:
            tmp_path = Path(tmp)
            raw_path = tmp_path / "input.bin"
            norm_path = tmp_path / "normalized.mp3"

            await gcs.download_to_path(gcs_uri=input_uri, dest=raw_path)
            await asyncio.to_thread(_run_ffmpeg_normalize, raw_path, norm_path)
            await gcs.upload_file(
                local_path=norm_path,
                gcs_uri=normalized_uri,
                content_type="audio/mpeg",
            )

            duration_sec = await asyncio.to_thread(_ffprobe_duration_seconds, norm_path)

            if settings.transcription_provider == "chirp_3":
                transcript_data = await _transcribe_with_chirp_maybe_chunked(
                    job_id=job_id,
                    tmp_path=tmp_path,
                    norm_path=norm_path,
                    normalized_uri=normalized_uri,
                    duration_sec=duration_sec,
                    lang=lang,
                    speaker_hint=speaker_hint,
                    gcs=gcs,
                    google_cloud_project=settings.google_cloud_project,
                    speech_region=settings.speech_region,
                )
            else:
                spk_line = ""
                if speaker_hint is not None:
                    spk_line = (
                        f"\nOdhad počtu mluvčích: {speaker_hint}. "
                        "Sladit pole speaker s audio podle tohoto odhadu, pokud to dává smysl.\n"
                    )
                transcript_prompt = f"""
Jsi profesionální přepisovatel porad v jazyce {lang}.
Úkol: přepiš audio přesně, včetně časových značek a rozlišení mluvčích (pokud lze odhadnout z audio).
{spk_line}Odpověz výhradně JSON podle schématu. Veškeré textové položky piš česky, pokud audio není v češtině — přesto přepis věrný originálu v poli text, summary může být česky.
"""

                transcript_json = await asyncio.to_thread(
                    _vertex_generate,
                    model_name=settings.model_transcript,
                    project=settings.google_cloud_project,
                    location=settings.model_region,
                    parts=[
                        Part.from_uri(normalized_uri, mime_type="audio/mpeg"),
                        Part.from_text(transcript_prompt),
                    ],
                    system_instruction="Odpovídej jen platným JSON. Žádný markdown.",
                    response_schema=TRANSCRIPT_SCHEMA,
                )
                transcript_data = json.loads(transcript_json)

            segments = transcript_data.get("segments") or []
            lines = []
            for seg in segments:
                ts = seg.get("timestamp", "")
                sp = seg.get("speaker", "")
                tx = seg.get("text", "")
                lines.append(f"[{ts}] {sp}: {tx}")
            flat_transcript = "\n".join(lines) if lines else transcript_data.get("summary", "")

            minutes_prompt = f"""
Z následujícího přepisu porady vytvoř strukturovaný zápis.
Jazyk výstupu: čeština (lang hint: {lang}).
V akčních bodech uveď konkrétní úkoly; vlastníka a termín vyplň jen pokud jsou v textu naznačeny, jinak prázdný řetězec nebo prázdné pole.

--- Přepis ---
{flat_transcript}
"""

            minutes_json = await asyncio.to_thread(
                _vertex_generate,
                model_name=settings.model_minutes,
                project=settings.google_cloud_project,
                location=settings.model_region,
                parts=[Part.from_text(minutes_prompt)],
                system_instruction="Odpovídej jen platným JSON. Žádný markdown.",
                response_schema=MINUTES_SCHEMA,
            )
            minutes_data = json.loads(minutes_json)

            t_bytes = json.dumps(transcript_data, ensure_ascii=False, indent=2).encode("utf-8")
            m_bytes = json.dumps(minutes_data, ensure_ascii=False, indent=2).encode("utf-8")

            await gcs.upload_bytes(
                gcs_uri=transcript_uri,
                data=t_bytes,
                content_type="application/json; charset=utf-8",
            )
            await gcs.upload_bytes(
                gcs_uri=minutes_uri,
                data=m_bytes,
                content_type="application/json; charset=utf-8",
            )

        await store.update_job(
            job_id,
            {
                "status": JobStatus.completed.value,
                "transcript_gcs_uri": transcript_uri,
                "minutes_gcs_uri": minutes_uri,
                "error": None,
            },
        )
    except subprocess.CalledProcessError as e:
        logger.exception("ffmpeg failed for job %s", job_id)
        err = (e.stderr or e.stdout or str(e))[:4000]
        await store.update_job(
            job_id,
            {"status": JobStatus.failed.value, "error": f"ffmpeg error: {err}"},
        )
    except Exception as e:
        logger.exception("Pipeline failed for job %s", job_id)
        await store.update_job(
            job_id,
            {"status": JobStatus.failed.value, "error": str(e)[:4000]},
        )
