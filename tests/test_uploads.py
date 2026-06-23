"""Regression tests for multipart file/image uploads.

These guard the bug where ``request.form()`` returns
``starlette.datastructures.UploadFile``, which is NOT an instance of
``fastapi.UploadFile`` (Starlette 1.0 split the classes into a parent and a
subclass). ``_form_uploads`` checked the fastapi type, so every real upload was
dropped as if it were a plain string field — it never reached
``_save_uploaded_files`` / ``_read_uploaded_images`` and ``file_count`` was
always 0.

We exercise the real helpers through real Starlette multipart parsing via a
throwaway app rather than POSTing to ``/api/chat``: that endpoint spawns the
bundled CLI subprocess (the driver), which can't run under test. Upload
handling is identical in ``/api/chat`` and ``/api/chat/send`` and runs before
the driver, so the helper layer is the faithful bug surface.
"""
from __future__ import annotations

import base64

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import app as app_module

# 1x1 transparent PNG — decodes to the `\x89PNG\r\n\x1a\n` signature that
# _validate_image checks against the declared image/png content type.
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4"
    "2mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _mini_app() -> FastAPI:
    """Replay the real endpoints' upload-handling lines, minus the driver."""
    mini = FastAPI()

    @mini.post("/files")
    async def _files(request: Request):
        form = await request.form()
        files = app_module._form_uploads(form, "files")
        metas = await app_module._save_uploaded_files(files, "test-run")
        return {"kept": [f.filename for f in files], "metas": metas}

    @mini.post("/images")
    async def _images(request: Request):
        form = await request.form()
        images = app_module._form_uploads(form, "images")
        blocks, _meta = await app_module._read_uploaded_images(images)
        return {"n": len(blocks), "type": blocks[0]["type"] if blocks else None}

    return mini


def test_form_uploads_keeps_real_multipart_file() -> None:
    """The core regression: a real file part survives _form_uploads."""
    c = TestClient(_mini_app())
    r = c.post(
        "/files",
        data={"message": "hi"},
        files={"files": ("AuthKey_test.p8", b"-----BEGIN KEY-----\n",
                         "application/octet-stream")},
    )
    assert r.status_code == 200
    assert r.json()["kept"] == ["AuthKey_test.p8"]


def test_save_uploaded_files_writes_to_disk() -> None:
    """End-to-end through multipart: parsed, kept, written under the run dir."""
    c = TestClient(_mini_app())
    payload = b"-----BEGIN PRIVATE KEY-----\nABC\n"
    r = c.post(
        "/files",
        data={"message": "hi"},
        files={"files": ("secret.p8", payload, "application/octet-stream")},
    )
    assert r.status_code == 200
    metas = r.json()["metas"]
    assert len(metas) == 1
    assert metas[0]["filename"] == "secret.p8"
    assert metas[0]["size"] == len(payload)
    saved = app_module.UPLOADS_ROOT / "test-run" / "secret.p8"
    assert saved.read_bytes() == payload


def test_read_uploaded_images_produces_block() -> None:
    """A real PNG upload becomes a base64 image block (image_count was 0)."""
    c = TestClient(_mini_app())
    r = c.post("/images", files={"images": ("dot.png", _PNG_1x1, "image/png")})
    assert r.status_code == 200
    body = r.json()
    assert body["n"] == 1
    assert body["type"] == "image"


def test_form_uploads_skips_nameless_string_field() -> None:
    """A plain form field (no filename) must not be mistaken for a file —
    the fix widens the type check but must not swallow string fields."""
    c = TestClient(_mini_app())
    r = c.post("/files", data={"files": "not-a-file", "message": "hi"})
    assert r.status_code == 200
    assert r.json()["kept"] == []
