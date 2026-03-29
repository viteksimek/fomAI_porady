from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from typing import Any

from google.cloud import firestore

from app.models import JobStatus, utc_now_iso
from app.settings import Settings, get_settings


class JobsStore(ABC):
    @abstractmethod
    async def create_job(
        self,
        *,
        status: JobStatus,
        input_gcs_uri: str | None,
        options: dict[str, Any],
    ) -> str: ...

    @abstractmethod
    async def get_job(self, job_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    async def update_job(self, job_id: str, patch: dict[str, Any]) -> None: ...


class MemoryJobsStore(JobsStore):
    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def create_job(
        self,
        *,
        status: JobStatus,
        input_gcs_uri: str | None,
        options: dict[str, Any],
    ) -> str:
        job_id = str(uuid.uuid4())
        now = utc_now_iso()
        async with self._lock:
            self._jobs[job_id] = {
                "job_id": job_id,
                "status": status.value,
                "input_gcs_uri": input_gcs_uri,
                "options": options,
                "created_at": now,
                "updated_at": now,
                "transcript_gcs_uri": None,
                "minutes_gcs_uri": None,
                "error": None,
                "transcript": None,
                "minutes": None,
            }
        return job_id

    async def get_job(self, job_id: str) -> dict[str, Any] | None:
        async with self._lock:
            j = self._jobs.get(job_id)
            return dict(j) if j else None

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> None:
        async with self._lock:
            if job_id not in self._jobs:
                return
            self._jobs[job_id].update(patch)
            self._jobs[job_id]["updated_at"] = utc_now_iso()


class FirestoreJobsStore(JobsStore):
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = firestore.Client(
            project=settings.google_cloud_project,
            database=settings.firestore_database,
        )
        self._collection = "meeting_jobs"

    async def create_job(
        self,
        *,
        status: JobStatus,
        input_gcs_uri: str | None,
        options: dict[str, Any],
    ) -> str:
        job_id = str(uuid.uuid4())
        now = utc_now_iso()
        doc = {
            "job_id": job_id,
            "status": status.value,
            "input_gcs_uri": input_gcs_uri,
            "options": options,
            "created_at": now,
            "updated_at": now,
            "transcript_gcs_uri": None,
            "minutes_gcs_uri": None,
            "error": None,
        }

        def _write() -> None:
            self._client.collection(self._collection).document(job_id).set(doc)

        await asyncio.to_thread(_write)
        return job_id

    async def get_job(self, job_id: str) -> dict[str, Any] | None:

        def _read() -> dict[str, Any] | None:
            snap = self._client.collection(self._collection).document(job_id).get()
            if not snap.exists:
                return None
            data = snap.to_dict() or {}
            data["job_id"] = job_id
            return data

        return await asyncio.to_thread(_read)

    async def update_job(self, job_id: str, patch: dict[str, Any]) -> None:
        patch = {**patch, "updated_at": utc_now_iso()}

        def _merge() -> None:
            ref = self._client.collection(self._collection).document(job_id)
            ref.set(patch, merge=True)

        await asyncio.to_thread(_merge)


def get_jobs_store_resolved() -> JobsStore:
    s = get_settings()
    if s.use_memory_store:
        return MemoryJobsStore()
    if not s.google_cloud_project:
        return MemoryJobsStore()
    return FirestoreJobsStore(s)
