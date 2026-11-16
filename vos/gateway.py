"""VOS authentication and per-account ComfyUI workers."""
import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import signal
import sqlite3
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from aiohttp import web
from multidict import CIMultiDict
from yarl import URL

import models
from downloads import Downloads
from patch_frontend import DISPATCHER

APP = "com.ictrek.comfyui"
BASE = f"/app/{APP}/"
EDITOR = BASE + "editor/"
COOKIE = "__Secure-vos-comfyui"
VOS = Path(__file__).resolve().parent
ROOT = VOS.parent
DATA = Path(os.getenv("COMFYUI_DATA_DIR", "/data"))
SHARED = Path(os.getenv("COMFYUI_MODEL_HUB_MOUNT", "/modelhub"))
PROJECTION = Path(os.getenv("COMFYUI_MODEL_INDEX", "/run/comfyui/models"))
USERINFO = os.environ["VOS_USERINFO_URL"]
HUB = os.getenv("COMFYUI_MODEL_HUB_URL", "http://v-scheduler-model-hub:6005")
SLOTS = max(1, min(4, int(os.getenv("COMFYUI_MAX_WORKERS", "1"))))
IDLE = 600
STARTUP_TIMEOUT = 180
STATIC = frozenset({"assets", "locales", "scripts", "extensions", "templates", "docs", "favicon.ico"})
HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                 "te", "trailer", "transfer-encoding", "upgrade"})


def workspace_scope(subject):
    return hashlib.sha256(("comfyui-workspace:" + subject).encode()).hexdigest()[:32]


def cuda_device_groups():
    groups = set()
    for pattern in ("nvmap", "nvidia*", "dri/renderD*"):
        for path in Path("/dev").glob(pattern):
            try:
                node = path.stat()
            except FileNotFoundError:
                continue
            if stat.S_ISCHR(node.st_mode) and node.st_gid and node.st_mode & stat.S_IRGRP:
                groups.add(node.st_gid)
    return sorted(groups)


def shared_model_groups():
    groups = set()
    for path in (SHARED, SHARED / "export"):
        try:
            directory = path.stat()
        except OSError:
            continue
        if stat.S_ISDIR(directory.st_mode) and directory.st_gid and directory.st_mode & stat.S_IXGRP:
            groups.add(directory.st_gid)
    return groups


def token_payload(token):
    try:
        payload = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError):
        return {}


@dataclass
class Login:
    subject: str
    token: str
    recheck: float
    active: bool = True


@dataclass
class Worker:
    process: asyncio.subprocess.Process
    port: int
    touched: float
    inflight: int = 0
    sockets: int = 0
    ready: bool = False


class Gateway:
    def __init__(self):
        self.workers = {}
        self.sessions = {}
        self.lock = asyncio.Lock()
        DATA.mkdir(parents=True, exist_ok=True)
        os.chmod(DATA, 0o711)
        PROJECTION.mkdir(parents=True, exist_ok=True)
        for category in models.CATEGORIES:
            (PROJECTION / category).mkdir(exist_ok=True)
        self.db = sqlite3.connect(DATA / "accounts.sqlite")
        os.chmod(DATA / "accounts.sqlite", 0o600)
        self.db.execute("CREATE TABLE IF NOT EXISTS accounts (uid INTEGER PRIMARY KEY, subject TEXT UNIQUE)")
        self.downloads = Downloads(self.db, HUB, SHARED, PROJECTION)

    async def verify(self, token):
        if not re.fullmatch(r"[A-Za-z0-9_.~+/=-]{1,16384}", token):
            raise web.HTTPUnauthorized()
        jwt = token_payload(token)
        if jwt.get("client_id") != APP or jwt.get("token_type") != "Bearer" or jwt.get("application") == "app":
            raise web.HTTPUnauthorized()
        try:
            async with self.http.get(USERINFO, headers={"Authorization": "Bearer " + token},
                                     allow_redirects=False, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status in (400, 401, 403):
                    raise web.HTTPUnauthorized()
                response.raise_for_status()
                claims = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as error:
            raise web.HTTPServiceUnavailable(reason="VOS account service unavailable") from error
        subject = claims.get("sub") if isinstance(claims, dict) else None
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 512 or claims.get("enable") != 1:
            raise web.HTTPUnauthorized()
        return subject

    async def revalidate(self, login):
        if not login.active:
            raise web.HTTPUnauthorized()
        if login.recheck <= time.monotonic():
            try:
                subject = await self.verify(login.token)
            except web.HTTPUnauthorized:
                login.active = False
                raise
            if subject != login.subject:
                login.active = False
                raise web.HTTPUnauthorized()
            login.recheck = time.monotonic() + 30

    async def authenticate(self, request):
        authorization = request.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            token = authorization[7:]
            return Login(await self.verify(token), token, time.monotonic() + 30)
        login = self.sessions.get(request.cookies.get(COOKIE))
        if login is None:
            raise web.HTTPUnauthorized()
        await self.revalidate(login)
        return login

    async def session(self, request):
        if request.headers.get("Sec-Fetch-Site") != "same-origin":
            raise web.HTTPForbidden()
        key = request.cookies.get(COOKIE)
        previous = self.sessions.get(key)
        if request.method == "DELETE":
            if previous:
                previous.active = False
                self.sessions.pop(key)
            response = web.Response(status=204)
            response.del_cookie(COOKIE, path=BASE)
            return response
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            raise web.HTTPUnauthorized()
        token = authorization[7:]
        subject = await self.verify(token)
        changed = previous is not None and previous.subject != subject
        if previous and previous.active and not changed:
            previous.token, previous.recheck = token, time.monotonic() + 30
        else:
            if previous:
                previous.active = False
                self.sessions.pop(key, None)
            for stale_key, login in list(self.sessions.items()):
                if not login.active or login.recheck + 3600 < time.monotonic():
                    login.active = False
                    self.sessions.pop(stale_key)
            key = secrets.token_urlsafe(32)
            self.sessions[key] = Login(subject, token, time.monotonic() + 30)
        response = web.json_response({"account_changed": changed}, headers={"Cache-Control": "no-store"})
        response.set_cookie(COOKIE, key, secure=True, httponly=True, samesite="Strict", path=BASE)
        return response

    def workspace(self, subject):
        name = hashlib.sha256(subject.encode()).hexdigest()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO accounts(subject) VALUES (?)", (name,))
            uid = self.db.execute("SELECT uid FROM accounts WHERE subject = ?", (name,)).fetchone()[0] + 20000
        workspace = DATA / name
        if not workspace.exists():
            workspace.mkdir(mode=0o700)
            os.chown(workspace, uid, uid)
        if workspace.is_symlink() or workspace.stat().st_uid != uid:
            raise web.HTTPServiceUnavailable(reason="Workspace ownership mismatch")
        tmp = workspace / "tmp"
        if not tmp.exists():
            tmp.mkdir(mode=0o700)
            os.chown(tmp, uid, uid)
        return workspace, uid

    async def stop(self, worker):
        if worker.process.returncode is not None:
            return
        try:
            os.killpg(worker.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(worker.process.wait(), timeout=8)
        except TimeoutError:
            os.killpg(worker.process.pid, signal.SIGKILL)
            await worker.process.wait()

    async def idle(self, worker):
        if worker.inflight or worker.sockets or not worker.ready:
            return False
        try:
            async with self.http.get(f"http://127.0.0.1:{worker.port}/queue",
                                     timeout=aiohttp.ClientTimeout(total=3)) as response:
                response.raise_for_status()
                queue = await response.json()
                return not queue["queue_running"] and not queue["queue_pending"]
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError):
            return False

    async def worker(self, login):
        async with self.lock:
            for subject, worker in list(self.workers.items()):
                if worker.process.returncode is not None:
                    self.workers.pop(subject)
            worker = self.workers.get(login.subject)
            if worker is not None:
                worker.touched = time.monotonic()
                worker.inflight += 1
                return worker
            if len(self.workers) >= SLOTS:
                for subject, candidate in sorted(self.workers.items(), key=lambda item: item[1].touched):
                    if await self.idle(candidate):
                        await self.stop(candidate)
                        self.workers.pop(subject)
                        break
                else:
                    raise web.HTTPServiceUnavailable(reason="All ComfyUI workers are busy", headers={"Retry-After": "10"})
            port = next(port for port in range(18300, 18300 + SLOTS)
                        if all(worker.port != port for worker in self.workers.values()))
            workspace, uid = self.workspace(login.subject)
            env = {k: v for k, v in os.environ.items() if not k.startswith(("VOS_", "COMFYUI_"))}
            env.update(HOME=str(workspace), USER=f"comfyui-{uid}", LOGNAME=f"comfyui-{uid}",
                       XDG_CACHE_HOME=str(workspace / ".cache"), TMPDIR=str(workspace / "tmp"),
                       TORCHINDUCTOR_CACHE_DIR=str(workspace / ".cache" / "torchinductor"),
                       PYTHONDONTWRITEBYTECODE="1")
            device = os.getenv("COMFYUI_DEVICE", "cpu")
            groups = shared_model_groups()
            if device in {"cuda", "l4t"}:
                groups.update(cuda_device_groups())
            flags = ["--cpu"] if device == "cpu" else []
            if device == "l4t":
                flags += ["--disable-dynamic-vram", "--disable-pinned-memory"]
            worker = Worker(await asyncio.create_subprocess_exec(
                sys.executable, str(ROOT / "main.py"), "--listen", "127.0.0.1", "--port", str(port),
                "--base-directory", str(workspace), "--models-directory", str(PROJECTION),
                "--disable-all-custom-nodes", "--disable-auto-launch", *flags,
                cwd=workspace, env=env, user=uid, group=uid, extra_groups=sorted(groups), umask=0o077,
                start_new_session=True), port, time.monotonic(), inflight=1)
            self.workers[login.subject] = worker
            return worker

    async def ready(self, worker):
        if worker.ready:
            return
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while worker.process.returncode is None and time.monotonic() < deadline:
            try:
                async with self.http.get(f"http://127.0.0.1:{worker.port}/system_stats",
                                         timeout=aiohttp.ClientTimeout(total=2)) as response:
                    if response.status == 200:
                        worker.ready = True
                        return
            except (aiohttp.ClientError, TimeoutError):
                pass
            await asyncio.sleep(0.5)
        await self.stop(worker)
        raise web.HTTPServiceUnavailable(reason="ComfyUI worker did not start", headers={"Retry-After": "10"})

    async def open_editor(self, request):
        if request.headers.get("Sec-Fetch-Site") != "same-origin":
            raise web.HTTPForbidden()
        login = await self.authenticate(request)
        await self.wait_worker(login)
        return web.json_response({"editor_path": f"{EDITOR}workspace/{workspace_scope(login.subject)}/"},
                                 headers={"Cache-Control": "no-store"})

    async def model_downloads(self, request):
        site = request.headers.get("Sec-Fetch-Site")
        if site not in (None, "same-origin") or (
            site is None and not request.headers.get("Authorization", "").startswith("Bearer ")
        ):
            raise web.HTTPForbidden()
        owner = workspace_scope((await self.authenticate(request)).subject)
        if request.method == "POST":
            return await self.downloads.start(request, owner, self.http)
        return await self.downloads.status(request, owner, self.http)

    async def wait_worker(self, login):
        worker = await self.worker(login)
        try:
            await self.ready(worker)
        finally:
            worker.inflight -= 1

    async def proxy(self, request):
        path = request.match_info["path"]
        if not path:
            if request.headers.get("Sec-Fetch-Dest") == "iframe" and COOKIE not in request.cookies:
                raise web.HTTPFound(BASE)
            path = "index.html"
        if request.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            raise web.HTTPForbidden()
        if request.method not in {"GET", "HEAD"} and COOKIE in request.cookies and "Authorization" not in request.headers:
            if request.headers.get("Sec-Fetch-Site") != "same-origin":
                raise web.HTTPForbidden()
        try:
            login = await self.authenticate(request)
        except web.HTTPUnauthorized:
            if path == "index.html":
                raise web.HTTPFound(BASE)
            raise
        scope = request.match_info.get("scope")
        if ((scope is not None and scope != workspace_scope(login.subject))
                or (scope is None and "Authorization" not in request.headers)):
            if path == "index.html":
                raise web.HTTPFound(BASE)
            raise web.HTTPUnauthorized()
        worker = await self.worker(login)
        try:
            await self.ready(worker)
            prefix = EDITOR if scope is None else f"{EDITOR}workspace/{scope}/"
            raw = request.rel_url.raw_path.removeprefix(prefix)
            if path == "index.html":
                raw = ""
            url = URL(f"http://127.0.0.1:{worker.port}/" + raw, encoded=True).with_query(request.query)
            if request.headers.get("Upgrade", "").lower() == "websocket":
                return await self.websocket(request, url, worker, login)
            return await self.http_proxy(request, url, path, workspace_scope(login.subject))
        finally:
            worker.inflight -= 1
            worker.touched = time.monotonic()

    async def http_proxy(self, request, url, path, scope):
        excluded = HOP | {"host", "authorization", "cookie", "origin", "referer", "accept-encoding",
                          "comfy-user", "forwarded", "content-length"}
        headers = filtered(request.headers, excluded)
        headers["Accept-Encoding"] = "identity"
        body = request.content.iter_chunked(256 * 1024) if request.can_read_body else None
        try:
            async with self.http.request(request.method, url, headers=headers, data=body,
                                         allow_redirects=False) as upstream:
                headers = filtered(upstream.headers, HOP | {"set-cookie"})
                if path == "index.html" and upstream.status == 200 and request.method == "GET":
                    asset = f"./assets/{DISPATCHER}"
                    import_map = json.dumps({"imports": {asset: asset + "?vos-model-downloads"}})
                    page = (await upstream.text()).replace(
                        "<head>", f'<head><script type="importmap">{import_map}</script>'
                        f'<script src="{BASE}storage.js" data-workspace="{scope}"></script>', 1
                    ).replace("</head>", f'<script src="{BASE}bridge.js" defer></script>'
                              f'<script src="{BASE}model-downloads.js"></script></head>', 1)
                    headers.popall("Content-Length", None)
                    headers["Cache-Control"] = "no-store"
                    headers["Content-Type"] = "text/html; charset=utf-8"
                    return web.Response(text=page, status=200, headers=headers)
                if path.split("/", 1)[0] not in STATIC or path == f"assets/{DISPATCHER}":
                    headers["Cache-Control"] = "private, no-store"
                response = web.StreamResponse(status=upstream.status, headers=headers)
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(256 * 1024):
                    await response.write(chunk)
                return response
        except aiohttp.ClientError as error:
            raise web.HTTPBadGateway(reason="ComfyUI worker disconnected") from error

    async def websocket(self, request, url, worker, login):
        if request.match_info["path"] not in {"ws", "api/ws"}:
            raise web.HTTPNotFound()
        async with self.http.ws_connect(url, max_msg_size=64 * 1024**2, heartbeat=30) as upstream:
            ws = web.WebSocketResponse(heartbeat=30, max_msg_size=64 * 1024**2)
            await ws.prepare(request)
            worker.sockets += 1

            async def relay(source, target):
                async for message in source:
                    if message.type == aiohttp.WSMsgType.TEXT:
                        await target.send_str(message.data)
                    if message.type == aiohttp.WSMsgType.BINARY:
                        await target.send_bytes(message.data)

            async def check_login():
                while True:
                    await asyncio.sleep(15)
                    await self.revalidate(login)

            tasks = [asyncio.create_task(coro) for coro in (relay(ws, upstream), relay(upstream, ws), check_login())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                worker.sockets -= 1
                await ws.close()
            return ws

    async def background(self, app):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=900), auto_decompress=False,
                                         cookie_jar=aiohttp.DummyCookieJar()) as self.http:
            async def maintain():
                while True:
                    await self.downloads.refresh(self.http)
                    async with self.lock:
                        for subject, worker in list(self.workers.items()):
                            if worker.process.returncode is not None or (
                                time.monotonic() - worker.touched > IDLE and await self.idle(worker)
                            ):
                                await self.stop(worker)
                                self.workers.pop(subject)
                    await asyncio.sleep(30)

            task = asyncio.create_task(maintain())
            yield
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await asyncio.gather(*(self.stop(worker) for worker in self.workers.values()))
            self.db.close()


def filtered(headers, excluded):
    connection = {part.lower().strip() for value in headers.getall("Connection", []) for part in value.split(",")}
    return CIMultiDict((key, value) for key, value in headers.items() if key.lower() not in excluded | connection)


def static(filename):
    async def serve(request):
        return web.FileResponse(VOS / filename, headers={"Cache-Control": "no-cache"})
    return serve


def app():
    gateway = Gateway()
    app = web.Application(client_max_size=64 * 1024)
    app.cleanup_ctx.append(gateway.background)
    async def health(request):
        return web.json_response({"status": "ok", "version": os.getenv("COMFYUI_VERSION", "dev")})
    app.router.add_get("/health", health)
    app.router.add_get(BASE, static("index.html"))
    app.router.add_get(BASE + "bridge.js", static("bridge.js"))
    app.router.add_get(BASE + "storage.js", static("storage.js"))
    app.router.add_get(BASE + "model-downloads.js", static("model-downloads.js"))
    app.router.add_get(BASE + "guide", static("guide.html"))
    async def specification(request):
        return web.FileResponse(ROOT / "openapi.yaml", headers={"Content-Type": "text/yaml; charset=utf-8"})
    app.router.add_get(BASE + "openapi.yaml", specification)
    app.router.add_post(BASE + "session", gateway.session)
    app.router.add_delete(BASE + "session", gateway.session)
    app.router.add_post(BASE + "prepare", gateway.open_editor)
    app.router.add_post(BASE + "model-downloads", gateway.model_downloads)
    app.router.add_get(BASE + "model-downloads/{batch:[0-9a-f]{32}}", gateway.model_downloads)
    app.router.add_route("*", EDITOR + "workspace/{scope:[0-9a-f]{32}}/{path:.*}", gateway.proxy)
    app.router.add_route("*", EDITOR + "{path:.*}", gateway.proxy)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(app(), port=8188, access_log=None)
