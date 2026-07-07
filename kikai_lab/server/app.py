"""FastAPI application factory for ``kikai server start``.

Every JSON endpoint answers with the CLI's envelope shape
(``{ok, schema_version, data, warnings, errors, next_actions}``) so agents parse one
format everywhere — including FastAPI's own request-validation failures and unknown
routes. ``OperationError`` codes map onto HTTP statuses by code convention; unexpected
exceptions become a 500 envelope that names the exception type but never echoes its
message (raw messages carry host paths / file excerpts; the traceback goes to the log).
"""
from __future__ import annotations

import logging
from importlib import metadata
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from kikai_lab.envelope import envelope, error
from kikai_lab.operation import OperationError
from kikai_lab.server.registry import ServerConfig

logger = logging.getLogger("kikai_lab.server")

# HTTP statuses by OperationError code convention. Tails are checked both bare
# ("data_source.exists") and suffixed ("operation.script_bundle_run_name_in_use").
_NOT_FOUND_TAILS = ("missing", "not_found")
_CONFLICT_TAILS = ("exists", "in_use", "archived", "not_local")
_FORBIDDEN_TAILS = ("forbidden",)
_UNPROCESSABLE_TAILS = ("invalid", "unknown", "unverified", "incompatible")
_TIMEOUT_TAILS = ("timeout",)


def _tail_matches(tail: str, names: tuple[str, ...]) -> bool:
    return any(tail == name or tail.endswith(f"_{name}") for name in names)


def http_status_for_code(code: str) -> int:
    tail = code.rsplit(".", 1)[-1]
    if _tail_matches(tail, _NOT_FOUND_TAILS):
        return 404
    if _tail_matches(tail, _CONFLICT_TAILS):
        return 409
    if _tail_matches(tail, _FORBIDDEN_TAILS):
        return 403
    if _tail_matches(tail, _UNPROCESSABLE_TAILS):
        return 422
    if _tail_matches(tail, _TIMEOUT_TAILS):
        # an infrastructure budget exceeded (docker_run_timeout / docker_exec_timeout)
        # is gateway-timeout semantics — 400 would tell clients "your request is
        # malformed, do not retry", the opposite of the truth.
        return 504
    return 400


def sanitize_details(value: Any) -> Any:
    """Trim absolute host paths out of error details before they cross the network.

    Registry validation errors (validation.py) carry full host paths in ``path``-ish
    keys — fine on a local CLI, not on a possibly-exposed HTTP port. The basename is
    kept so the error stays actionable.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if (
                isinstance(item, str)
                and item.startswith("/")
                and (key == "path" or key.endswith("_path") or key.endswith("_root"))
            ):
                out[key] = f".../{item.rsplit('/', 1)[-1]}"
            else:
                out[key] = sanitize_details(item)
        return out
    if isinstance(value, list):
        return [sanitize_details(item) for item in value]
    return value


def sanitize_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {**item, "details": sanitize_details(item.get("details") or {})} for item in errors
    ]


class KikaiJSONResponse(JSONResponse):
    """JSONResponse whose serializer absorbs YAML-native values.

    Hand-edited registry YAML can carry datetime objects (unquoted ISO timestamps);
    a report over such a record must not 500. A ``default=`` hook keeps the fast
    zero-copy path for ordinary payloads (large columnar metrics responses are hot),
    only touching values json.dumps cannot handle.
    """

    def render(self, content: Any) -> bytes:
        import json as _json

        from kikai_lab.server.registry import jsonable

        kwargs = {"ensure_ascii": False, "allow_nan": False, "separators": (",", ":")}
        try:
            return _json.dumps(content, default=jsonable, **kwargs).encode("utf-8")
        except TypeError:
            # default= never sees mapping KEYS; a YAML-native non-string key (e.g. an
            # unquoted date used as a key) needs the full normalizing walk.
            return _json.dumps(jsonable(content), **kwargs).encode("utf-8")


def envelope_response(
    *,
    ok: bool,
    data: dict[str, Any] | None = None,
    warnings: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
    next_actions: list[dict[str, Any]] | None = None,
    status_code: int = 200,
) -> JSONResponse:
    return KikaiJSONResponse(
        envelope(ok=ok, data=data, warnings=warnings, errors=errors, next_actions=next_actions),
        status_code=status_code,
    )


def kikai_version() -> str:
    try:
        return metadata.version("kikai-lab")
    except metadata.PackageNotFoundError:  # editable/uninstalled checkout
        return "0.0.0+source"


def create_app(config: ServerConfig) -> FastAPI:
    import hmac
    from contextlib import asynccontextmanager

    reconciler = None
    if config.with_reconciler:
        from kikai_lab.server.reconciler import BackgroundReconciler

        reconciler = BackgroundReconciler(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if reconciler is not None:
            reconciler.start()
        try:
            yield
        finally:
            if reconciler is not None:
                reconciler.stop()

    app = FastAPI(
        title="kikai server", version=kikai_version(), docs_url="/docs", lifespan=lifespan
    )
    app.state.kikai_config = config
    app.state.kikai_reconciler = reconciler

    @app.exception_handler(OperationError)
    async def operation_error_handler(_: Request, exc: OperationError) -> JSONResponse:
        return envelope_response(
            ok=False,
            errors=[error(exc.code, exc.message, details=sanitize_details(exc.details))],
            status_code=http_status_for_code(exc.code),
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return envelope_response(
            ok=False,
            errors=[
                error(
                    "request.params_invalid",
                    "request parameters failed validation",
                    details={"validation_errors": exc.errors()},
                )
            ],
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "route.not_found" if exc.status_code == 404 else "request.http_error"
        if exc.status_code == 405:
            code = "route.method_not_allowed"
        return envelope_response(
            ok=False,
            errors=[error(code, str(exc.detail), details={"status_code": exc.status_code})],
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
        # Raw exception text can carry host paths or file excerpts; keep it in the log,
        # off the wire (the server may be exposed beyond localhost, with no auth).
        logger.exception("unhandled server error")
        return envelope_response(
            ok=False,
            errors=[
                error(
                    "server.internal_error",
                    "unexpected server error; see the server log",
                    details={"type": type(exc).__name__},
                )
            ],
            status_code=500,
        )

    if config.auth_token:
        # constant-time shared-secret gate on EVERYTHING except the liveness probe.
        # 401 (not the envelope 4xx family) so proxies and clients treat it as an
        # auth failure, not an application error.
        expected = config.auth_token

        @app.middleware("http")
        async def bearer_auth(request, call_next):  # type: ignore[no-untyped-def]
            if request.url.path == "/healthz":
                return await call_next(request)
            header = request.headers.get("authorization", "")
            token = header[7:] if header.lower().startswith("bearer ") else ""
            if not hmac.compare_digest(token, expected):
                return KikaiJSONResponse(
                    status_code=401,
                    content=envelope(
                        ok=False,
                        errors=[
                            error(
                                "server.unauthorized",
                                "missing or invalid bearer token",
                            )
                        ],
                    ),
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return await call_next(request)

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        reconciler_state: dict[str, Any] = {"enabled": config.with_reconciler}
        if reconciler is not None:
            reconciler_state["last_tick_at"] = reconciler.last_tick_at
            reconciler_state["last_errors"] = reconciler.last_errors
        return envelope_response(
            ok=True,
            data={
                "status": "ok",
                "version": kikai_version(),
                "projects_root": str(config.projects_root),
                "host_id": config.host_id,
                "reconciler": reconciler_state,
            },
        )

    @app.get("/v1/version")
    def version() -> JSONResponse:
        return envelope_response(ok=True, data={"version": kikai_version()})

    @app.get("/v1/skill.md")
    def skill_doc():
        from pathlib import Path as _Path

        from fastapi.responses import PlainTextResponse

        skill_path = _Path(__file__).parent / "SKILL.md"
        return PlainTextResponse(
            skill_path.read_text(encoding="utf-8"), media_type="text/markdown"
        )

    # Dashboard: a static no-build SPA served from the package itself. Imported
    # lazily like the routers so create_app stays cheap to import.
    from pathlib import Path

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard_index() -> FileResponse:
        return FileResponse(static_dir / "index.html", media_type="text/html")

    from kikai_lab.server.artifacts import build_artifacts_router
    from kikai_lab.server.bundles import build_bundles_router
    from kikai_lab.server.projects import build_projects_router
    from kikai_lab.server.resources import build_resources_router
    from kikai_lab.server.runs import build_runs_router
    from kikai_lab.server.submit import build_submit_router

    app.include_router(build_projects_router(config), prefix="/v1")
    app.include_router(build_resources_router(config), prefix="/v1")
    app.include_router(build_runs_router(config), prefix="/v1")
    app.include_router(build_artifacts_router(config), prefix="/v1")
    app.include_router(build_bundles_router(config), prefix="/v1")
    app.include_router(build_submit_router(config), prefix="/v1")
    return app
