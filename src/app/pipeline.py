from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import tempfile
from pathlib import Path

import vertexai
from vertexai.generative_models import GenerationConfig, GenerativeModel, HarmBlockThreshold, HarmCategory, Part

from app.jobs_store import get_jobs_store_resolved
from app.models import JobStatus
from app.settings import get_settings
from app.storage import GcsStorage

logger = logging.getLogger(__name__)

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

            transcript_prompt = f"""
Jsi profesionální přepisovatel porad v jazyce {lang}.
Úkol: přepiš audio přesně, včetně časových značek a rozlišení mluvčích (pokud lze odhadnout z audio).
Odpověz výhradně JSON podle schématu. Veškeré textové položky piš česky, pokud audio není v češtině — přesto přepis věrný originálu v poli text, summary může být česky.
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
