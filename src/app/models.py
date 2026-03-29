from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    pending_upload = "pending_upload"
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class JobOptions(BaseModel):
    language_hint: str = Field(default="cs", description="Preferred language for prompts/output")


class GcsSource(BaseModel):
    gcs_uri: str = Field(..., description="gs://bucket/path to audio file")


class CreateJobBody(BaseModel):
    source: GcsSource
    options: JobOptions = Field(default_factory=JobOptions)


class JobAcceptedResponse(BaseModel):
    job_id: str
    status: str
    status_url: str


class SignedUploadBody(BaseModel):
    filename: str = "recording.m4a"
    content_type: str = "application/octet-stream"
    options: JobOptions | None = Field(
        default=None,
        description="Stejné jako u POST /v1/jobs — language_hint atd.",
    )


class PrepareUploadResponse(BaseModel):
    """Výsledek přípravy nahrání — vždy použijte tento tok pro spolehlivé zpracování libovolné velikosti."""

    job_id: str
    gcs_uri: str
    upload_url: str
    expires_in_seconds: int
    finalize_url: str
    status_url: str
    flow: str = "put_then_finalize"
    steps: list[str] = Field(
        default_factory=lambda: [
            "PUT: celý soubor jako raw body na upload_url (hlavička Content-Type musí odpovídat přípravě).",
            "POST: finalize_url bez těla (stejná base URL / autentizace jako ostatní /v1).",
            "GET: status_url dokud status není completed nebo failed.",
        ],
    )


# Zpětná kompatibilita — stejné pole jako dřív; nové klienty používají PrepareUploadResponse.
class SignedUploadResponse(PrepareUploadResponse):
    pass


class JobDetailResponse(BaseModel):
    job_id: str
    status: str
    created_at: str | None = None
    updated_at: str | None = None
    input_gcs_uri: str | None = None
    transcript_gcs_uri: str | None = None
    minutes_gcs_uri: str | None = None
    transcript_download_url: str | None = None
    minutes_download_url: str | None = None
    transcript: dict[str, Any] | None = None
    minutes: dict[str, Any] | None = None
    error: str | None = None


class TaskProcessBody(BaseModel):
    job_id: str


class HealthResponse(BaseModel):
    status: str
    version: str


class MetaResponse(BaseModel):
    """Jednotné informace pro klienty — nemusíte řešit limity ručně."""

    version: str
    upload_max_direct_multipart_mb: int = 32
    upload_recommended_path: str = "/v1/jobs/prepare-upload"
    note: str = (
        "Pro libovolně velké soubory vždy: prepare-upload → PUT na upload_url → POST finalize. "
        "Přímý multipart POST /v1/jobs/upload je vhodný jen pro malé soubory (pod limitem)."
    )


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
