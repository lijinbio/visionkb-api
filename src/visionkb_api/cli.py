"""Command line: ``visionkb-api serve | openapi | check``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__


def _openapi() -> dict:
    from .app import create_app

    return create_app().openapi()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="visionkb-api", description="VisionKB read-only API")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", help="run the HTTP server (settings from VISIONKB_* environment variables)")
    sp.add_argument("--host", help="default VISIONKB_API_HOST or 127.0.0.1")
    sp.add_argument("--port", type=int, help="default VISIONKB_API_PORT or 8770")
    sp.add_argument("--workers", type=int, default=1)
    op = sub.add_parser("openapi", help="write the OpenAPI document")
    op.add_argument("--out", type=Path, help="file to write (default stdout)")
    sub.add_parser("check", help="load the release and ping Neo4j/PostgreSQL; print JSON")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if a.cmd == "openapi":
        text = json.dumps(_openapi(), indent=2, sort_keys=False) + "\n"
        if a.out:
            a.out.write_text(text)
        else:
            sys.stdout.write(text)
        return 0

    from .config import Settings

    settings = Settings.from_env()
    if a.cmd == "check":
        from fastapi.testclient import TestClient

        from .app import create_app

        with TestClient(create_app(settings)) as c:
            r = c.get("/v1/health")
        sys.stdout.write(json.dumps(r.json(), indent=2) + "\n")
        data = r.json().get("data") or {}
        ok = all((data.get(k) or {}).get("status") == "ok" for k in ("neo4j", "summaries"))
        return 0 if ok else 1

    import uvicorn

    uvicorn.run(
        "visionkb_api.app:create_app",
        factory=True,
        host=a.host or settings.host,
        port=a.port or settings.port,
        workers=a.workers,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
