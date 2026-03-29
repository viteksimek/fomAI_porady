from __future__ import annotations

import json
import logging

from google.cloud import tasks_v2

from app.settings import Settings, get_settings

logger = logging.getLogger(__name__)


def enqueue_process_job(job_id: str, settings: Settings | None = None) -> None:
    s = settings or get_settings()
    if s.process_inline:
        raise RuntimeError("PROCESS_INLINE is enabled; use BackgroundTasks instead")
    if not s.google_cloud_project or not s.cloud_run_service_url:
        raise RuntimeError(
            "Cloud Tasks target not configured "
            "(GOOGLE_CLOUD_PROJECT, CLOUD_RUN_SERVICE_URL; "
            "or set PROCESS_INLINE=true)"
        )
    if not s.cloud_tasks_invoker_sa:
        logger.warning(
            "CLOUD_TASKS_INVOKER_SA not set; OIDC token will be missing "
            "(internal endpoint will reject requests unless SKIP_INTERNAL_OIDC=true)"
        )

    client = tasks_v2.CloudTasksClient()
    parent = client.queue_path(s.google_cloud_project, s.cloud_tasks_location, s.cloud_tasks_queue)

    payload = json.dumps({"job_id": job_id}).encode("utf-8")
    service_url = s.cloud_run_service_url.rstrip("/")
    target = f"{service_url}/internal/tasks/process"

    headers = {"Content-Type": "application/json"}

    task: dict = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": target,
            "headers": headers,
            "body": payload,
        }
    }
    if s.cloud_tasks_invoker_sa:
        task["http_request"]["oidc_token"] = {
            "service_account_email": s.cloud_tasks_invoker_sa,
            "audience": service_url,
        }

    client.create_task(parent=parent, task=task)
