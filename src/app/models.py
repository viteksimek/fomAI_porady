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


class SignedUploadResponse(BaseModel):
    job_id: str
    gcs_uri: str
    upload_url: str
    expires_in_seconds: int
    finalize_url: str


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


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
