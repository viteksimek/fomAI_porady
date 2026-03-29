from __future__ import annotations

import json
import logging
from typing import Annotated, Any

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status

from app import __version__
from app.auth_deps import require_api_key, verify_cloud_tasks_oidc
from app.jobs_store import get_jobs_store_resolved
from app.models import (
    CreateJobBody,
    HealthResponse,
    JobAcceptedResponse,
    JobDetailResponse,
    JobOptions,
    JobStatus,
    SignedUploadBody,
    SignedUploadResponse,
    TaskProcessBody,
)
from app.pipeline import process_job
from app.settings import get_settings
from app.storage import GcsStorage
from app.tasks import enqueue_process_job

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title=get_settings().app_name, version=__version__)


def _kickoff_processing(background_tasks: BackgroundTasks, job_id: str) -> None:
    settings = get_settings()
    if settings.process_inline:
        background_tasks.add_task(process_job, job_id)
        return
    try:
        enqueue_process_job(job_id)
    except Exception:
        logger.warning(
            "Cloud Tasks unavailable; falling back to background processing on this instance",
            exc_info=True,
        )
        background_tasks.add_task(process_job, job_id)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@app.post(
    "/v1/jobs",
    response_model=JobAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
async def create_job_from_gcs(
    body: CreateJobBody,
    background_tasks: BackgroundTasks,
    request: Request,
) -> JobAcceptedResponse:
    settings = get_settings()
    store = get_jobs_store_resolved()
    gcs = GcsStorage()

    if settings.gcs_bucket and not await gcs.blob_exists(body.source.gcs_uri):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GCS object does not exist or is not accessible",
        )

    job_id = await store.create_job(
        status=JobStatus.queued,
        input_gcs_uri=body.source.gcs_uri,
        options=body.options.model_dump(),
    )
    _kickoff_processing(background_tasks, job_id)
    base = str(request.base_url).rstrip("/")
    return JobAcceptedResponse(
        job_id=job_id,
        status=JobStatus.queued.value,
        status_url=f"{base}/v1/jobs/{job_id}",
    )


@app.post(
    "/v1/jobs/upload",
    response_model=JobAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
async def create_job_from_upload(
    background_tasks: BackgroundTasks,
    request: Request,
    file: Annotated[UploadFile, File(...)],
    options_json: Annotated[str | None, Form(alias="options")] = None,
) -> JobAcceptedResponse:
    settings = get_settings()
    if not settings.gcs_bucket or not settings.google_cloud_project:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GCS not configured",
        )

    opts: dict[str, Any] = JobOptions().model_dump()
    if options_json:
        try:
            parsed = json.loads(options_json)
            opts = {**opts, **parsed}
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"Invalid options JSON: {e}") from e

    store = get_jobs_store_resolved()
    gcs = GcsStorage()

    job_id = await store.create_job(
        status=JobStatus.queued,
        input_gcs_uri=None,
        options=opts,
    )
    dest_uri = gcs.job_input_uri(job_id, file.filename or "upload.bin")
    data = await file.read()
    await gcs.upload_bytes(
        gcs_uri=dest_uri,
        data=data,
        content_type=file.content_type or "application/octet-stream",
    )
    await store.update_job(job_id, {"input_gcs_uri": dest_uri})

    _kickoff_processing(background_tasks, job_id)
    base = str(request.base_url).rstrip("/")
    return JobAcceptedResponse(
        job_id=job_id,
        status=JobStatus.queued.value,
        status_url=f"{base}/v1/jobs/{job_id}",
    )


@app.post(
    "/v1/uploads/signed-url",
    response_model=SignedUploadResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
)
async def create_signed_upload(
    body: SignedUploadBody,
    request: Request,
) -> SignedUploadResponse:
    settings = get_settings()
    if not settings.gcs_bucket or not settings.google_cloud_project:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GCS not configured",
        )
    store = get_jobs_store_resolved()
    gcs = GcsStorage()

    job_id = await store.create_job(
        status=JobStatus.pending_upload,
        input_gcs_uri=None,
        options=JobOptions().model_dump(),
    )
    dest_uri = gcs.job_input_uri(job_id, body.filename)
    upload_url = await gcs.generate_upload_signed_url(
        gcs_uri=dest_uri,
        content_type=body.content_type,
        expiration_seconds=settings.signed_url_exp_seconds,
    )
    await store.update_job(job_id, {"input_gcs_uri": dest_uri})
    base = str(request.base_url).rstrip("/")
    return SignedUploadResponse(
        job_id=job_id,
        gcs_uri=dest_uri,
        upload_url=upload_url,
        expires_in_seconds=settings.signed_url_exp_seconds,
        finalize_url=f"{base}/v1/jobs/{job_id}/finalize-upload",
    )


@app.post(
    "/v1/jobs/{job_id}/finalize-upload",
    response_model=JobAcceptedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
async def finalize_upload(
    job_id: str,
    background_tasks: BackgroundTasks,
    request: Request,
) -> JobAcceptedResponse:
    store = get_jobs_store_resolved()
    gcs = GcsStorage()
    job = await store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.get("status") != JobStatus.pending_upload.value:
        raise HTTPException(status_code=400, detail="Job is not awaiting upload")
    input_uri = job.get("input_gcs_uri")
    if not input_uri:
        raise HTTPException(status_code=400, detail="Missing input_gcs_uri")

    if not await gcs.blob_exists(input_uri):
        raise HTTPException(
            status_code=400,
            detail="Object not found in GCS; upload before finalize",
        )

    await store.update_job(job_id, {"status": JobStatus.queued.value, "error": None})
    _kickoff_processing(background_tasks, job_id)
    base = str(request.base_url).rstrip("/")
    return JobAcceptedResponse(
        job_id=job_id,
        status=JobStatus.queued.value,
        status_url=f"{base}/v1/jobs/{job_id}",
    )


@app.get(
    "/v1/jobs/{job_id}",
    response_model=JobDetailResponse,
    dependencies=[Depends(require_api_key)],
)
async def get_job(
    job_id: str,
    include_signed_urls: bool = False,
) -> JobDetailResponse:
    store = get_jobs_store_resolved()
    job = await store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    settings = get_settings()
    gcs = GcsStorage()

    transcript_url = job.get("transcript_gcs_uri")
    minutes_url = job.get("minutes_gcs_uri")

    transcript_signed: str | None = None
    minutes_signed: str | None = None
    if include_signed_urls and settings.google_cloud_project and settings.gcs_bucket:
        if transcript_url:
            transcript_signed = await gcs.generate_download_signed_url(
                gcs_uri=transcript_url,
                expiration_seconds=settings.signed_url_exp_seconds,
            )
        if minutes_url:
            minutes_signed = await gcs.generate_download_signed_url(
                gcs_uri=minutes_url,
                expiration_seconds=settings.signed_url_exp_seconds,
            )

    # Optional: embed small JSON in memory store for local demos
    transcript_inline = job.get("transcript") if isinstance(job.get("transcript"), dict) else None
    minutes_inline = job.get("minutes") if isinstance(job.get("minutes"), dict) else None

    return JobDetailResponse(
        job_id=job_id,
        status=job.get("status", ""),
        created_at=job.get("created_at"),
        updated_at=job.get("updated_at"),
        input_gcs_uri=job.get("input_gcs_uri"),
        transcript_gcs_uri=transcript_url,
        minutes_gcs_uri=minutes_url,
        transcript_download_url=transcript_signed,
        minutes_download_url=minutes_signed,
        transcript=transcript_inline,
        minutes=minutes_inline,
        error=job.get("error"),
    )


@app.post("/internal/tasks/process")
async def internal_process_task(
    request: Request,
    background_tasks: BackgroundTasks,
    body: TaskProcessBody,
) -> dict[str, str]:
    verify_cloud_tasks_oidc(request)

    background_tasks.add_task(process_job, body.job_id)
    return {"status": "accepted", "job_id": body.job_id}
