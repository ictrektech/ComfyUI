import asyncio
import json
from io import BytesIO
from urllib.parse import quote

import pytest
from PIL import Image

from comfy_extras import nodes_image_studio


def _png_with_alpha():
    buffer = BytesIO()
    Image.new("RGBA", (2, 2), (255, 0, 0, 128)).save(buffer, format="PNG")
    return buffer.getvalue()


class _Response:
    def __init__(self, status=200, body=b"", payload=None):
        self.status = status
        self.body = body
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        return json.dumps(self.payload) if self.payload is not None else self.body.decode()

    async def read(self):
        return self.body


class _Session:
    def __init__(self, timeout, poll_response=None):
        self.calls = []
        self.poll_response = poll_response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def request(self, method, url, json=None, headers=None):
        self.calls.append((method, url, headers))
        if method == "POST":
            return _Response(payload={"job_id": "job-1"})
        if self.poll_response is not None:
            return self.poll_response
        return _Response(payload={"status": "succeeded", "progress": 1, "outputs": ["sub/result.png"]})

    def get(self, url, headers=None):
        self.calls.append(("GET", url, headers))
        return _Response(body=_png_with_alpha())

    def delete(self, url, headers=None):
        self.calls.append(("DELETE", url, headers))
        return _Response()


def _execute():
    return nodes_image_studio.ImageStudioGenerate.execute(
        prompt="test", model_key="sd-turbo", width=512, height=512, steps=4,
        cfg_scale=1.0, seed=-1, batch_count=1, negative_prompt="", model_variant="",
        sampler="euler_a", scheduler="", clip_skip=-1, hires=False, hires_scale=2.0,
        priority="background", extra_params="{}",
    )


def test_success_returns_three_channel_image_and_encodes_names(monkeypatch):
    sessions = []

    def make_session(timeout):
        session = _Session(timeout)
        sessions.append(session)
        return session

    monkeypatch.setattr(nodes_image_studio.aiohttp, "ClientSession", make_session)
    monkeypatch.setattr(nodes_image_studio, "POLL_SECONDS", 0)
    monkeypatch.setattr(nodes_image_studio, "VOS_SUB", "用户@example.com")

    image = asyncio.run(_execute()).result[0]

    assert image.shape == (1, 2, 2, 3)
    calls = sessions[0].calls
    post_headers = next(headers for method, _, headers in calls if method == "POST")
    assert post_headers["X-Vos-Sub"] == quote("用户@example.com", safe="")
    assert any("sub%2Fresult.png" in url for method, url, _ in calls if method == "GET")
    assert not any(method == "DELETE" for method, _, _ in calls)


def test_timeout_cancels_job(monkeypatch):
    sessions = []

    def make_session(timeout):
        session = _Session(timeout, poll_response=_Response(payload={"status": "running", "progress": 0.5, "outputs": []}))
        sessions.append(session)
        return session

    monkeypatch.setattr(nodes_image_studio.aiohttp, "ClientSession", make_session)
    monkeypatch.setattr(nodes_image_studio, "POLL_SECONDS", 0)
    monkeypatch.setattr(nodes_image_studio, "TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(nodes_image_studio, "VOS_SUB", "")

    with pytest.raises(TimeoutError):
        asyncio.run(_execute())

    assert any(method == "DELETE" for method, _, _ in sessions[0].calls)


def test_poll_error_cancels_job(monkeypatch):
    sessions = []

    def make_session(timeout):
        session = _Session(timeout, poll_response=_Response(status=500, body=b'{"detail":"boom"}'))
        sessions.append(session)
        return session

    monkeypatch.setattr(nodes_image_studio.aiohttp, "ClientSession", make_session)
    monkeypatch.setattr(nodes_image_studio, "POLL_SECONDS", 0)
    monkeypatch.setattr(nodes_image_studio, "VOS_SUB", "")

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(_execute())

    assert any(method == "DELETE" for method, _, _ in sessions[0].calls)
