"""Veřejná base URL z proxy hlaviček — Cloud Run posílá X-Forwarded-Proto / -Host."""

from starlette.requests import Request

from app.main import _public_base_url


def _request(
    *,
    scheme: str = "http",
    server_host: str = "127.0.0.1",
    server_port: int = 8080,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    h = headers or []
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": scheme,
            "root_path": "",
            "path": "/v1/jobs/prepare-upload",
            "raw_path": b"/v1/jobs/prepare-upload",
            "query_string": b"",
            "headers": h,
            "client": ("127.0.0.1", 12345),
            "server": (server_host, server_port),
        }
    )


def test_cloud_run_https_from_forwarded_headers() -> None:
    req = _request(
        scheme="http",
        server_host="169.254.8.1",
        headers=[
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-host", b"fomai-porady-635664358681.europe-west1.run.app"),
            (b"host", b"169.254.8.1:8080"),
        ],
    )
    assert _public_base_url(req) == "https://fomai-porady-635664358681.europe-west1.run.app"


def test_no_forwarded_uses_direct_scheme_and_host_header() -> None:
    req = _request(
        scheme="http",
        server_host="127.0.0.1",
        server_port=8000,
        headers=[(b"host", b"localhost:8000")],
    )
    assert _public_base_url(req) == "http://localhost:8000"


def test_first_value_when_proto_is_comma_separated() -> None:
    req = _request(
        scheme="http",
        headers=[
            (b"x-forwarded-proto", b"https, http"),
            (b"host", b"edge.example"),
        ],
    )
    assert _public_base_url(req) == "https://edge.example"
