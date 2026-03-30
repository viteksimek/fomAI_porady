from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "meeting-minutes-api"
    environment: str = "development"

    google_cloud_project: str = ""
    gcs_bucket: str = ""
    gcs_jobs_prefix: str = "jobs"

    firestore_database: str = "(default)"
    use_memory_store: bool = False

    cloud_tasks_location: str = "europe-west1"
    cloud_tasks_queue: str = "meeting-jobs"
    cloud_run_service_url: str = ""
    cloud_tasks_invoker_sa: str = ""

    model_region: str = "europe-west1"
    # Ve Vertex musí existovat v MODEL_REGION. Od března 2026 Google uvádí, že 2.0-flash-001
    # je jen pro stávající zákazníky; nové projekty mají směřovat na 2.5-flash / novější.
    model_transcript: str = "gemini-2.5-flash"
    model_minutes: str = "gemini-2.5-flash"

    # Přepis: chirp_3 = Speech-to-Text v2 (multiregion); vertex_gemini = audio do Gemini v MODEL_REGION.
    transcription_provider: Literal["chirp_3", "vertex_gemini"] = "chirp_3"
    # Pro Chirp 3 použij «us» nebo «eu» (multiregion), ne europe-west1.
    speech_region: str = "eu"

    api_key: str = ""
    skip_internal_oidc: bool = False

    process_inline: bool = False

    signed_url_exp_seconds: int = 900


@lru_cache
def get_settings() -> Settings:
    return Settings()
