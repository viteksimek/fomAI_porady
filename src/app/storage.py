from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from pathlib import Path
from google.cloud import storage

from app.settings import Settings, get_settings

_GS_URI_RE = re.compile(r"^gs://([^/]+)/(.+)$")


def parse_gs_uri(uri: str) -> tuple[str, str]:
    m = _GS_URI_RE.match(uri.strip())
    if not m:
        raise ValueError(f"Invalid GCS URI: {uri}")
    return m.group(1), m.group(2)


class GcsStorage:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client: storage.Client | None = None
        if self._settings.google_cloud_project:
            self._client = storage.Client(project=self._settings.google_cloud_project)

    def _require_client(self) -> storage.Client:
        if self._client is None:
            raise RuntimeError("GCS client not configured (set GOOGLE_CLOUD_PROJECT)")
        return self._client

    async def blob_exists(self, gcs_uri: str) -> bool:
        bucket_name, blob_name = parse_gs_uri(gcs_uri)

        def _exists() -> bool:
            b = self._require_client().bucket(bucket_name)
            return b.blob(blob_name).exists()

        return await asyncio.to_thread(_exists)

    async def upload_bytes(self, *, gcs_uri: str, data: bytes, content_type: str) -> None:
        bucket_name, blob_name = parse_gs_uri(gcs_uri)

        def _upload() -> None:
            b = self._require_client().bucket(bucket_name)
            blob = b.blob(blob_name)
            blob.upload_from_string(data, content_type=content_type)

        await asyncio.to_thread(_upload)

    async def upload_file(self, *, local_path: Path, gcs_uri: str, content_type: str) -> None:
        bucket_name, blob_name = parse_gs_uri(gcs_uri)

        def _upload() -> None:
            b = self._require_client().bucket(bucket_name)
            blob = b.blob(blob_name)
            blob.upload_from_filename(str(local_path), content_type=content_type)

        await asyncio.to_thread(_upload)

    async def download_to_path(self, *, gcs_uri: str, dest: Path) -> None:
        bucket_name, blob_name = parse_gs_uri(gcs_uri)

        def _download() -> None:
            b = self._require_client().bucket(bucket_name)
            blob = b.blob(blob_name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(dest))

        await asyncio.to_thread(_download)

    async def generate_upload_signed_url(
        self,
        *,
        gcs_uri: str,
        content_type: str,
        expiration_seconds: int,
    ) -> str:
        """Velké soubory: resumable session URL (server vytvoří session přes API — bez V4 signBlob).

        Klient pošle celý soubor jedním PUT na vrácenou URL (funguje i pro desítky MB).

        Parametr expiration_seconds zachovává kontrakt API; u resumable session platí TTL po straně GCS.
        """
        _ = expiration_seconds
        bucket_name, blob_name = parse_gs_uri(gcs_uri)

        def _session_url() -> str:
            b = self._require_client().bucket(bucket_name)
            blob = b.blob(blob_name)
            # Resumable upload session — spolehlivější na Cloud Run než generate_signed_url (IAM signBlob).
            return blob.create_resumable_upload_session(
                content_type=content_type,
                size=None,
            )

        return await asyncio.to_thread(_session_url)

    async def generate_download_signed_url(
        self,
        *,
        gcs_uri: str,
        expiration_seconds: int,
    ) -> str:
        bucket_name, blob_name = parse_gs_uri(gcs_uri)

        def _url() -> str:
            b = self._require_client().bucket(bucket_name)
            blob = b.blob(blob_name)
            return blob.generate_signed_url(
                version="v4",
                expiration=timedelta(seconds=expiration_seconds),
                method="GET",
            )

        return await asyncio.to_thread(_url)

    def job_input_uri(self, job_id: str, filename: str) -> str:
        safe = Path(filename).name.replace("..", "").strip() or "audio.bin"
        return f"gs://{self._settings.gcs_bucket}/{self._settings.gcs_jobs_prefix}/{job_id}/input/{safe}"

    def job_normalized_uri(self, job_id: str) -> str:
        return (
            f"gs://{self._settings.gcs_bucket}/"
            f"{self._settings.gcs_jobs_prefix}/{job_id}/working/normalized.mp3"
        )

    def job_chirp_chunk_uri(self, job_id: str, chunk_index: int) -> str:
        """Dočasné segmenty pro Chirp BatchRecognize (limit délky jednoho souboru)."""
        return (
            f"gs://{self._settings.gcs_bucket}/"
            f"{self._settings.gcs_jobs_prefix}/{job_id}/working/chunk_{chunk_index:03d}.mp3"
        )

    def job_transcript_uri(self, job_id: str) -> str:
        return (
            f"gs://{self._settings.gcs_bucket}/"
            f"{self._settings.gcs_jobs_prefix}/{job_id}/output/transcript.json"
        )

    def job_minutes_uri(self, job_id: str) -> str:
        return (
            f"gs://{self._settings.gcs_bucket}/"
            f"{self._settings.gcs_jobs_prefix}/{job_id}/output/minutes.json"
        )
