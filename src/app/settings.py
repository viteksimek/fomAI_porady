from functools import lru_cache

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
    model_transcript: str = "gemini-2.0-flash-001"
    model_minutes: str = "gemini-2.0-flash-001"

    api_key: str = ""
    skip_internal_oidc: bool = False

    process_inline: bool = False

    signed_url_exp_seconds: int = 900


@lru_cache
def get_settings() -> Settings:
    return Settings()
