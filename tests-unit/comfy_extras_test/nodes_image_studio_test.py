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
    def __init__(self, timeout, poll_response=None, output_bodies=None, job_payload=None, job_outputs=None):
        self.timeout = timeout
        self.calls = []
        self.payloads = []
        self.poll_response = poll_response
        self.output_bodies = output_bodies or [_png_with_alpha()]
        self.job_payload = {"job_id": "job-1"} if job_payload is None else job_payload
        self.job_outputs = job_outputs or ["sub/result.png"]
        self.downloads = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def request(self, method, url, json=None, headers=None):
        self.calls.append((method, url, headers))
        if method == "POST":
            self.payloads.append(json)
            return _Response(payload=self.job_payload)
        if self.poll_response is not None:
            return self.poll_response
        return _Response(payload={"status": "succeeded", "progress": 1, "outputs": self.job_outputs})

    def get(self, url, headers=None):
        self.calls.append(("GET", url, headers))
        body = self.output_bodies[min(self.downloads, len(self.output_bodies) - 1)]
        self.downloads += 1
        return _Response(body=body)

    def delete(self, url, headers=None):
        self.calls.append(("DELETE", url, headers))
        return _Response()


def _install(monkeypatch, catalog=None, **session_kwargs):
    sessions = []

    def make_session(timeout):
        session = _Session(timeout, **session_kwargs)
        sessions.append(session)
        return session

    monkeypatch.setattr(nodes_image_studio.aiohttp, "ClientSession", make_session)
    monkeypatch.setattr(nodes_image_studio, "POLL_SECONDS", 0)
    monkeypatch.setattr(nodes_image_studio, "VOS_SUB", "")
    monkeypatch.setattr(nodes_image_studio, "model_catalog", lambda: catalog or {})
    return sessions


def _execute(**overrides):
    kwargs = dict(
        prompt="test", model_key="sd-turbo", width=512, height=512, steps=4, cfg_scale=1.0, seed=-1,
        batch_count=1, negative_prompt="", model_variant="", sampler="euler_a", scheduler="", clip_skip=-1,
        hires=False, hires_scale=2.0, hires_steps=0, hires_denoising_strength=0.7, priority="background",
        extra_params="{}",
    )
    kwargs.update(overrides)
    return nodes_image_studio.ImageStudioGenerate.execute(**kwargs)


def _sd15():
    return {"sd15": {"key": "sd15", "status": "ready", "capabilities": ["txt2img"],
                     "runtime": {"defaults": {"width": 512, "height": 512, "steps": 20, "cfg_scale": 7.0, "sampler": "euler_a"}}}}


def test_success_returns_three_channel_image_and_encodes_names(monkeypatch):
    sessions = _install(monkeypatch)
    monkeypatch.setattr(nodes_image_studio, "VOS_SUB", "用户@example.com")

    result = asyncio.run(_execute()).result

    assert result[0].shape == (1, 2, 2, 3)
    calls = sessions[0].calls
    post_headers = next(headers for method, _, headers in calls if method == "POST")
    assert post_headers["X-Vos-Sub"] == quote("用户@example.com", safe="")
    assert any("sub%2Fresult.png" in url for method, url, _ in calls if method == "GET")
    assert not any(method == "DELETE" for method, _, _ in calls)


def test_unset_size_steps_and_sampler_follow_model_family(monkeypatch):
    sessions = _install(monkeypatch, catalog=_sd15())

    asyncio.run(_execute(model_key="sd15"))

    payload = sessions[0].payloads[0]
    for field in ("width", "height", "steps", "cfg_scale", "sampler"):
        assert field not in payload


def test_explicit_values_are_sent(monkeypatch):
    sessions = _install(monkeypatch, catalog=_sd15())

    asyncio.run(_execute(model_key="sd15", steps=30, cfg_scale=7.5, sampler="euler"))

    payload = sessions[0].payloads[0]
    assert payload["steps"] == 30
    assert payload["cfg_scale"] == 7.5
    assert payload["sampler"] == "euler"


def test_random_seed_is_resolved_and_reported(monkeypatch):
    sessions = _install(monkeypatch)

    result = asyncio.run(_execute(seed=-1)).result

    seed = sessions[0].payloads[0]["seed"]
    assert 0 <= seed <= 0xFFFFFFFF
    assert result[1] == seed


def test_explicit_seed_is_forwarded_unchanged(monkeypatch):
    sessions = _install(monkeypatch)

    result = asyncio.run(_execute(seed=42)).result

    assert sessions[0].payloads[0]["seed"] == 42
    assert result[1] == 42


def test_scheduling_priority_is_forwarded(monkeypatch):
    sessions = _install(monkeypatch)

    asyncio.run(_execute(priority="high"))

    assert sessions[0].payloads[0]["scheduling"] == {"priority": "high"}


def test_batch_outputs_stack(monkeypatch):
    _install(monkeypatch, output_bodies=[_png_with_alpha(), _png_with_alpha()],
             job_outputs=["sub/a.png", "sub/b.png"])

    result = asyncio.run(_execute(batch_count=2)).result

    assert result[0].shape == (2, 2, 2, 3)


def test_extra_params_merge_into_request(monkeypatch):
    sessions = _install(monkeypatch)

    asyncio.run(_execute(extra_params=json.dumps({"width": 640, "vae_tiling": True})))

    payload = sessions[0].payloads[0]
    assert payload["width"] == 640
    assert payload["vae_tiling"] is True


def test_extra_params_must_be_json_object(monkeypatch):
    _install(monkeypatch)

    with pytest.raises(ValueError, match="extra_params must be a JSON object"):
        asyncio.run(_execute(extra_params="[1, 2]"))


def test_failed_job_raises_and_cancels(monkeypatch):
    sessions = _install(monkeypatch, poll_response=_Response(payload={"status": "failed", "error": "boom", "outputs": []}))

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(_execute())

    assert any(method == "DELETE" for method, _, _ in sessions[0].calls)


def test_missing_job_id_raises(monkeypatch):
    _install(monkeypatch, job_payload={})

    with pytest.raises(RuntimeError, match="did not return a job_id"):
        asyncio.run(_execute())


def test_timeout_cancels_job(monkeypatch):
    sessions = _install(monkeypatch, poll_response=_Response(payload={"status": "running", "progress": 0.5, "outputs": []}))
    monkeypatch.setattr(nodes_image_studio, "TIMEOUT_SECONDS", 0)

    with pytest.raises(TimeoutError):
        asyncio.run(_execute())

    assert any(method == "DELETE" for method, _, _ in sessions[0].calls)


def test_poll_error_cancels_job(monkeypatch):
    sessions = _install(monkeypatch, poll_response=_Response(status=500, body=b'{"detail":"boom"}'))

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(_execute())

    assert any(method == "DELETE" for method, _, _ in sessions[0].calls)
