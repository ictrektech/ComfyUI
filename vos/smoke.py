"""Exercise the real worker and the VOS gateway in a disposable image."""
import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from aiohttp import ClientSession, FormData, web
from aiohttp.test_utils import TestServer


def token(subject):
    payload = base64.urlsafe_b64encode(json.dumps({
        "client_id": "com.ictrek.comfyui",
        "token_type": "Bearer",
        "application": "web",
        "sub": subject,
    }).encode()).rstrip(b"=").decode()
    return f"fixture.{payload}.signature"


async def main():
    async def userinfo(request):
        subject = subjects.get(request.headers.get("Authorization"))
        if not subject:
            raise web.HTTPUnauthorized()
        return web.json_response({"sub": subject, "enable": 1})

    alice, bob = ({"Authorization": "Bearer " + token(subject)} for subject in ("alice", "bob"))
    subjects = {alice["Authorization"]: "alice", bob["Authorization"]: "bob"}
    fake_vos = web.Application()
    fake_vos.router.add_get("/userinfo", userinfo)

    selected_files = None

    async def catalog(request):
        records = [{"status": "ready", "source": "hf",
                    "access_path": "/data/export/hf/fixtures/models/current"}]
        if selected_files:
            records.append({"status": "ready", "source": "hf",
                            "access_path": "/data/export/hf/fixtures/downloads/current"})
        return web.json_response(records)

    async def local_files(request):
        raise web.HTTPNotFound()

    async def pull(request):
        nonlocal selected_files
        body = await request.json()
        assert body == {"source": "hf", "model_id": "hf://fixtures/downloads",
                        "files": ["remote/a.safetensors", "remote/b.safetensors"], "append": True}
        selected_files = body["files"]
        for file in selected_files:
            destination = root / "modelhub/export/hf/fixtures/downloads/current" / file
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("fixture used only for the download and path contract")
        return web.json_response({"task_id": "task-download"})

    async def task(request):
        return web.json_response({"phase": "READY"})

    fake_vos.router.add_get("/api/v1/models", catalog)
    fake_vos.router.add_get("/api/v1/models/files", local_files)
    fake_vos.router.add_post("/api/v1/models/pull", pull)
    fake_vos.router.add_get("/api/v1/tasks/detail/task-download", task)
    async with TestServer(fake_vos) as platform:
        os.environ["VOS_USERINFO_URL"] = str(platform.make_url("/userinfo"))
        os.environ["COMFYUI_MODEL_HUB_URL"] = str(platform.make_url(""))
        root = Path(os.environ["COMFYUI_DATA_DIR"])
        model = root / "modelhub/export/hf/fixtures/models/current/checkpoints/smoke.safetensors"
        model.parent.mkdir(parents=True)
        model.write_text("catalog fixture; not used for inference")
        os.environ["COMFYUI_MODEL_HUB_MOUNT"] = str(root / "modelhub")
        os.environ["COMFYUI_MODEL_INDEX"] = str(root / "catalog")
        # Workers can traverse the VOS storage mount to the read-only model index.
        os.chmod(root, 0o755)
        import gateway
        async with TestServer(gateway.app()) as application, ClientSession() as client:
            def url(path):
                return application.make_url(gateway.EDITOR + path)

            async with client.get(url("api/queue")) as response:
                assert response.status == 401
            async with client.get(url("api/queue"), headers={"Authorization": "Bearer invalid"}) as response:
                assert response.status == 401
            browser = {"Sec-Fetch-Site": "same-origin"}
            async with client.post(application.make_url(gateway.BASE + "session"), headers=alice | browser) as response:
                assert response.status == 200 and response.cookies[gateway.COOKIE]["httponly"]
                browser["Cookie"] = f"{gateway.COOKIE}={response.cookies[gateway.COOKIE].value}"
            async with client.post(application.make_url(gateway.BASE + "prepare"), headers=browser) as response:
                assert response.status == 200
                editor = (await response.json())["editor_path"]
            async with client.get(application.make_url(editor), headers=browser) as response:
                assert response.status == 200 and "bridge.js" in await response.text()
            async with client.get(url("scripts/app.js"), headers=alice) as response:
                assert response.status == 200 and "comfyAPI" in await response.text()
            async with client.get(url("api/settings"), headers=alice) as response:
                assert response.status == 200 and isinstance(await response.json(), dict)
            async with client.get(url("api/models/checkpoints"), headers=alice) as response:
                assert response.status == 200 and await response.json() == ["hf/fixtures/models/checkpoints/smoke.safetensors"]
            download = application.make_url(gateway.BASE + "model-downloads")
            model_requests = [
                {"directory": directory, "name": name,
                 "url": f"https://huggingface.co/fixtures/downloads/resolve/main/remote/{filename}"}
                for directory, name, filename in (
                    ("vae", "nested/a.safetensors", "a.safetensors"),
                    ("unet", "b.safetensors", "b.safetensors"),
                )
            ]
            async with client.post(download, headers=alice, json={"models": [
                {**model_requests[0], "name": "../a.safetensors"},
            ]}) as response:
                assert response.status == 400
            async with client.post(download, headers=alice, json={"models": model_requests}) as response:
                assert response.status == 202, await response.text()
                batch = (await response.json())["id"]
            async with client.get(str(download) + "/" + batch, headers=bob) as response:
                assert response.status == 404
            async with client.get(str(download) + "/" + batch, headers=alice) as response:
                assert response.status == 200, await response.text()
                entries = (await response.json())["models"]
                assert len(entries) == 2 and all(entry["status"] == "ready" for entry in entries), entries
            for category, name in (("vae", "nested/a.safetensors"), ("diffusion_models", "b.safetensors")):
                async with client.get(url("api/models/" + category), headers=alice) as response:
                    assert response.status == 200 and name in await response.json()
            async with client.get(url("api/object_info/IdeogramV4"), headers=alice) as response:
                assert response.status == 200 and (await response.json())["IdeogramV4"]["api_node"] is True
            async with client.ws_connect(url("ws"), headers=alice) as socket:
                message = await socket.receive_json(timeout=10)
                assert message["type"] == "status" and message["data"]["sid"]
            async with client.post(url("api/settings"), headers=alice, json={"Comfy.Locale": "zh"}) as response:
                assert response.status == 200
            async with client.get(url("api/settings/Comfy.Locale"), headers=alice) as response:
                assert response.status == 200 and await response.json() == "zh"
            upload = FormData()
            upload.add_field("image", b"test input", filename="private.bin", content_type="application/octet-stream")
            async with client.post(url("api/upload/image"), headers=alice, data=upload) as response:
                assert response.status == 200 and (await response.json())["name"] == "private.bin"
            async with client.post(url("api/prompt"), headers=alice, json={"prompt": {
                "1": {"class_type": "EmptyImage", "inputs": {"width": 64, "height": 64, "batch_size": 1, "color": 4283263}},
                "2": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "vos_smoke"}},
            }}) as response:
                job = await response.json()
                assert response.status == 200 and job["prompt_id"], job
            prompt_id = job["prompt_id"]
            for _ in range(120):
                async with client.get(url("api/history/" + prompt_id), headers=alice) as response:
                    history = await response.json()
                if prompt_id in history:
                    break
                await asyncio.sleep(0.5)
            assert history[prompt_id]["status"]["status_str"] == "success", history
            image = {k: v for k, v in history[prompt_id]["outputs"]["2"]["images"][0].items()
                     if k in {"filename", "subfolder", "type"}}
            async with client.get(url("api/view"), params=image, headers=alice) as response:
                assert response.status == 200 and (await response.read()).startswith(b"\x89PNG")
            async with client.get(application.make_url(editor + "api/settings"), headers=bob) as response:
                assert response.status == 401
            async with client.get(url("api/settings/Comfy.Locale"), headers=bob) as response:
                assert response.status == 200 and await response.json() is None
            async with client.get(url("api/view"), params=image, headers=bob) as response:
                assert response.status == 404
            sys.stdout.write("VOS smoke passed: authentication, editor, settings, WebSocket, "
                             "partner node discovery, shared downloads with exact workflow paths, "
                             "local execution without Comfy credentials, and account isolation\n")


if __name__ == "__main__":
    with TemporaryDirectory(dir="/tmp", prefix="comfyui-smoke-") as temp:
        os.environ["COMFYUI_DATA_DIR"] = temp
        asyncio.run(main())
