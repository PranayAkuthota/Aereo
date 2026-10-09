from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Request
from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import parse_options_header
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.errors import APIError


class UploadParser(MultiPartParser):
    """Own every spool, including unfinished parts not returned in FormData.

    Starlette 0.52.1 tracks these handles in _files_to_close_on_error. Keep this
    small adapter covered by ASGI cleanup tests when upgrading Starlette.
    """

    finished = False

    def on_end(self) -> None:
        self.finished = True
        super().on_end()

    def close_files(self) -> None:
        for handle in self._files_to_close_on_error:
            handle.close()


@asynccontextmanager
async def uploaded_file(request: Request) -> AsyncIterator[UploadFile]:
    media_type, _ = parse_options_header(request.headers.get("content-type", ""))
    if not media_type:
        raise APIError("VALIDATION_ERROR", "A multipart field named file is required.", 422)
    if media_type.lower() != b"multipart/form-data":
        raise APIError("UNSUPPORTED_MEDIA_TYPE", "Use multipart/form-data.", 415)
    parser = UploadParser(request.headers, request.stream(), max_files=1, max_fields=0)
    try:
        form = await parser.parse()
        if not parser.finished:
            raise APIError("INVALID_MULTIPART", "The multipart body is incomplete.")
        items = form.multi_items()
        if len(items) != 1 or items[0][0] != "file" or not isinstance(items[0][1], UploadFile):
            raise APIError(
                "VALIDATION_ERROR", "Exactly one file field named file is required.", 422
            )
        yield items[0][1]
    except (MultiPartException, MultipartParseError) as exc:
        raise APIError("INVALID_MULTIPART", "The multipart body is malformed.") from exc
    finally:
        # Synchronous handle closure also runs when the request task is cancelled.
        # All handles belong to this parser; no reliance on garbage collection.
        parser.close_files()
