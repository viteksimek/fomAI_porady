from __future__ import annotations

from fastapi import Header, HTTPException, Request, status

from app.settings import get_settings


async def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.api_key:
        return
    if not x_api_key or x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key",
        )


def verify_cloud_tasks_oidc(request: Request) -> None:
    settings = get_settings()
    if settings.skip_internal_oidc:
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing bearer token",
        )
    token = auth.removeprefix("Bearer ").strip()
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as ga_requests

        aud = settings.cloud_run_service_url
        if not aud:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="CLOUD_RUN_SERVICE_URL not set for OIDC verification",
            )
        idinfo = id_token.verify_oauth2_token(
            token,
            ga_requests.Request(),
            audience=aud,
        )
        email = idinfo.get("email") or ""
        expected = settings.cloud_tasks_invoker_sa
        if expected and email != expected:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Token email does not match expected service account",
            )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Invalid OIDC token: {e}",
        ) from e
