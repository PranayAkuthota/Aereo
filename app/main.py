import asyncio
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

from fastapi import FastAPI, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings
from app.errors import APIError
from app.repository import Repository
from app.schemas import ErrorResponse, FileInfo, Health, MeasurementPage
from app.service import process_upload
from app.uploads import uploaded_file

logger = logging.getLogger("aereo")


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        item = {"level": record.levelname, "event": record.getMessage(), "logger": record.name}
        for key in ("file_id", "feature_count", "failed_count", "method", "path", "status"):
            if hasattr(record, key):
                item[key] = getattr(record, key)
        if record.exc_info:
            item["exception"] = self.formatException(record.exc_info)
        return json.dumps(item)


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    application_logger = logging.getLogger("app")
    for target in (logger, application_logger):
        target.handlers = [handler]
        target.setLevel(logging.INFO)
        target.propagate = False


def error_response(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


class UploadLimitMiddleware:
    """Bound the entire multipart request, including requests without Content-Length."""

    def __init__(self, app: ASGIApp, max_body_bytes: int):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["method"] == "POST":
            headers = dict(scope["headers"])
            try:
                length = int(headers.get(b"content-length", b"0"))
                if length < 0:
                    raise ValueError
            except ValueError:
                await error_response("INVALID_REQUEST", "Invalid Content-Length.", 400)(
                    scope, receive, send
                )
                return
            if length > self.max_body_bytes:
                await error_response(
                    "UPLOAD_TOO_LARGE", "Request body exceeds the size limit.", 413
                )(scope, receive, send)
                return
        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if scope["method"] == "POST" and message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    # Raised during body parsing inside ExceptionMiddleware; handled consistently.
                    raise HTTPException(413, "Request body exceeds the size limit.")
            return message

        async def logged_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                logger.info(
                    "http_request",
                    extra={
                        "method": scope["method"],
                        "path": scope["path"],
                        "status": message["status"],
                    },
                )
            await send(message)

        await self.app(scope, bounded_receive, logged_send)


async def wait_for_processing(job: asyncio.Task[FileInfo]) -> FileInfo:
    """Keep the upload slot until a running thread finishes, even if its caller cancels."""
    try:
        return await asyncio.shield(job)
    except asyncio.CancelledError:
        while not job.done():
            try:
                await asyncio.shield(job)
            except asyncio.CancelledError:
                continue
            except Exception:
                break  # Retrieve the worker error below; the request remains cancelled.
        if not job.cancelled():
            failure = job.exception()
            if failure is not None:
                logger.error("cancelled_upload_worker_failed", exc_info=failure)
        raise


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    repository = Repository(settings.database_path)
    processing_slots = asyncio.Semaphore(settings.max_concurrent_uploads)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        await run_in_threadpool(repository.initialize)
        yield

    application = FastAPI(
        title="Geospatial File Measurement API",
        version="0.1.0",
        description="Upload KML or a zipped Shapefile; get explicit metric measurements and CRS.",
        lifespan=lifespan,
    )
    application.add_middleware(
        UploadLimitMiddleware, max_body_bytes=settings.max_upload_bytes + 64 * 1024
    )

    @application.exception_handler(APIError)
    async def api_error(request: Request, exc: APIError) -> JSONResponse:
        return error_response(exc.code, exc.message, exc.status_code)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Do not echo request bodies, properties, or parser internals into error responses.
        return error_response(
            "VALIDATION_ERROR", "Invalid path, query, or missing upload field.", 422
        )

    @application.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code = "UPLOAD_TOO_LARGE" if exc.status_code == 413 else "INVALID_REQUEST"
        return error_response(code, str(exc.detail), exc.status_code)

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_error")
        return error_response("INTERNAL_ERROR", "An unexpected server error occurred.", 500)

    errors: dict[int | str, dict[str, Any]] = {
        status: {"model": ErrorResponse} for status in (400, 404, 413, 415, 422, 500, 503)
    }

    @application.get("/health/", response_model=Health, summary="Check database readiness")
    def health() -> Health:
        repository.check()
        return Health()

    @application.post(
        "/api/files/",
        status_code=201,
        response_model=FileInfo,
        responses=errors,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["file"],
                            "properties": {"file": {"type": "string", "format": "binary"}},
                        }
                    }
                },
            }
        },
        summary="Upload and measure a geospatial file",
        description="Multipart field `file`: KML 2.2 or one zipped Shapefile with .prj. "
        "Processing finishes before responding; feature errors remain in results.",
    )
    async def upload(request: Request, response: Response) -> FileInfo:
        # Acquire before multipart parsing so waiting requests do not retain parsed uploads.
        if processing_slots.locked():
            raise APIError("PROCESSING_BUSY", "Upload capacity is busy; retry later.", 503)
        await processing_slots.acquire()
        try:
            async with uploaded_file(request) as file:
                data = bytearray()
                while chunk := await file.read(64 * 1024):
                    data.extend(chunk)
                    if len(data) > settings.max_upload_bytes:
                        raise APIError(
                            "UPLOAD_TOO_LARGE", "The upload exceeds the size limit.", 413
                        )
                job = asyncio.create_task(
                    run_in_threadpool(
                        process_upload, bytes(data), file.filename or "", settings, repository
                    )
                )
                info = await wait_for_processing(job)
        finally:
            processing_slots.release()
        response.headers["Location"] = f"/api/files/{info.id}/"
        return info

    @application.get(
        "/api/files/{file_id}/",
        response_model=FileInfo,
        responses=errors,
        summary="Get persisted file metadata",
    )
    def file_info(file_id: UUID) -> FileInfo:
        return repository.get(str(file_id))

    @application.get(
        "/api/files/{file_id}/measurements/",
        response_model=MeasurementPage,
        responses=errors,
        summary="Get feature geometry, attributes and measurements",
        description="Geometry is normalized to XY in the source CRS; Z/M are discarded. "
        "Results are ordered by original zero-based feature index. "
        "Offsets beyond the total return an empty page; limit is 1–1000.",
    )
    def measurements(
        file_id: UUID,
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    ) -> MeasurementPage:
        info = repository.get(str(file_id))
        return MeasurementPage(
            file_id=file_id,
            total=info.feature_count,
            offset=offset,
            limit=limit,
            features=(
                repository.measurements(str(file_id), offset, limit)
                if offset < info.feature_count
                else []
            ),
        )

    return application


app = create_app()
