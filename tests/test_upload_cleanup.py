import asyncio

import pytest
import starlette.formparsers
from starlette.requests import ClientDisconnect

from app.config import Settings
from app.main import create_app

OPENING = (
    b'--BOUNDARY\r\nContent-Disposition: form-data; name="file"; filename="test.kml"\r\n'
    b"Content-Type: application/octet-stream\r\n\r\n"
)
CLOSING = b"\r\n--BOUNDARY--\r\n"
POINT = b'<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><Point><coordinates>78,17</coordinates></Point></Placemark></kml>'


@pytest.mark.parametrize(
    "case,expected",
    [
        ("success", 201),
        ("validation", 400),
        ("file_limit", 413),
        ("body_limit", 413),
        ("incomplete", 400),
        ("malformed", 400),
        ("disconnect", None),
        ("cancel", None),
    ],
)
def test_multiframe_upload_spools_are_closed_on_every_exit(tmp_path, monkeypatch, case, expected):
    handles = []
    original = starlette.formparsers.SpooledTemporaryFile

    def tracked(*args, **kwargs):
        handle = original(*args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(starlette.formparsers, "SpooledTemporaryFile", tracked)
    # Force disk spooling with small safe bodies; identity and closure are asserted explicitly.
    monkeypatch.setattr(starlette.formparsers.MultiPartParser, "spool_max_size", 32)
    settings = Settings(database_path=tmp_path / "upload.db", max_upload_bytes=256)
    payload = POINT if case == "success" else b"x" * 200
    frames = [OPENING + payload]
    if case == "body_limit":
        frames.append(b"y" * 66000)
    elif case == "file_limit":
        frames.append(b"y" * 200 + CLOSING)
    elif case == "malformed":
        frames.append(b"\r\n--BOUNDARY\r\ninvalid-header\r\n\r\nx\r\n--BOUNDARY--\r\n")
    elif case not in {"incomplete", "disconnect", "cancel"}:
        frames.append(CLOSING)

    async def exercise():
        application = create_app(settings)
        sent = []

        async def receive():
            if frames:
                body = frames.pop(0)
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": bool(frames) or case in {"disconnect", "cancel"},
                }
            if case == "cancel":
                raise asyncio.CancelledError
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/files/",
            "raw_path": b"/api/files/",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"multipart/form-data; boundary=BOUNDARY")],
            "server": ("test", 80),
            "client": ("test", 1),
        }
        async with application.router.lifespan_context(application):
            if case in {"disconnect", "cancel"}:
                with pytest.raises((ClientDisconnect, asyncio.CancelledError)):
                    await application(scope, receive, send)
            else:
                await application(scope, receive, send)
                assert (
                    next(m["status"] for m in sent if m["type"] == "http.response.start")
                    == expected
                )
                if case in {"incomplete", "malformed"}:
                    assert b'"INVALID_MULTIPART"' in b"".join(m.get("body", b"") for m in sent)
        assert handles and all(h.closed for h in handles)
        assert all(h._rolled for h in handles)

    asyncio.run(exercise())


def test_processing_capacity_remains_held_until_cancelled_worker_finishes(tmp_path, monkeypatch):
    import threading

    from app import main

    entered, release = threading.Event(), threading.Event()
    original = main.process_upload

    def gated(*args):
        entered.set()
        assert release.wait(5), "audit worker was not released"
        return original(*args)

    monkeypatch.setattr(main, "process_upload", gated)
    settings = Settings(database_path=tmp_path / "capacity.db", max_concurrent_uploads=1)

    async def invoke(app):
        sent = []
        body = OPENING + POINT + CLOSING

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/files/",
            "raw_path": b"/api/files/",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"content-type", b"multipart/form-data; boundary=BOUNDARY")],
            "server": ("test", 80),
            "client": ("test", 1),
        }
        await app(scope, receive, send)
        return next(m["status"] for m in sent if m["type"] == "http.response.start")

    async def exercise():
        application = create_app(settings)
        async with application.router.lifespan_context(application):
            active = asyncio.create_task(invoke(application))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                assert await invoke(application) == 503
                active.cancel()
                await asyncio.sleep(0)
                assert not active.done()
                assert await invoke(application) == 503
                active.cancel()  # Repeated cancellation must not release a live worker's slot.
                await asyncio.sleep(0)
                assert not active.done()
                assert await invoke(application) == 503
                from app.repository import Repository

                with Repository(settings.database_path).connection() as conn:
                    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 0
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await active
            from app.repository import Repository
            from app.schemas import FileInfo

            with Repository(settings.database_path).connection() as conn:
                rows = conn.execute("SELECT id, metadata FROM files").fetchall()
                assert len(rows) == 1
                info = FileInfo.model_validate_json(rows[0][1])
                assert info.status == "COMPLETED" and info.feature_count == 1
                assert conn.execute("SELECT count(*) FROM features").fetchone()[0] == 1
            assert await invoke(application) == 201

    asyncio.run(exercise())
