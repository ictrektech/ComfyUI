"""Submit exact workflow weight files to the V-Scheduler Model Hub."""
import asyncio
import json
import re
import secrets
from collections import defaultdict
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

import aiohttp
from aiohttp import web

import models

REPOSITORY_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,95}")
MODEL_EXTENSIONS = {".safetensors", ".sft"}
ACTIVE_PHASES = {"PENDING", "DOWNLOADING", "VERIFYING"}


def segments(value):
    parts = value.split("/")
    if len(value.encode()) > 512 or any(
        part in ("", ".", "..") or len(part.encode()) > 255 or re.search(r"[\\:\x00-\x1f\x7f]", part)
        for part in parts
    ):
        raise ValueError("invalid model path")
    return parts


def model_request(value):
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) for k in ("name", "directory", "url")):
        raise ValueError("invalid model request")
    category = models.ALIASES.get(value["directory"], value["directory"])
    name = value["name"]
    # Comfy's Linux loader accepts a saved Windows path as a literal filename.
    # Keep the workflow value byte-for-byte, including the separator it used.
    name_parts = segments(name.replace("\\", "/"))
    if (category not in models.CATEGORIES or PurePosixPath(name_parts[-1]).suffix not in MODEL_EXTENSIONS
            or (len(name_parts) > 1 and name_parts[0] in {"hf", "ms"})
            or any(PurePosixPath(part).suffix in MODEL_EXTENSIONS for part in name_parts[:-1])):
        raise ValueError("unsupported model directory or format")
    url = urlsplit(value["url"])
    if url.scheme != "https" or url.fragment:
        raise ValueError("unsupported model URL")
    parts = unquote(url.path).removeprefix("/").split("/")
    if url.netloc == "huggingface.co" and len(parts) >= 5 and parts[2:4] == ["resolve", "main"]:
        source, owner, repo, file = "hf", parts[0], parts[1], "/".join(parts[4:])
    elif url.netloc in {"modelscope.cn", "www.modelscope.cn"} and len(parts) >= 6 and parts[0] == "models" and parts[3:5] == ["resolve", "master"]:
        source, owner, repo, file = "ms", parts[1], parts[2], "/".join(parts[5:])
    else:
        raise ValueError("expected a Hugging Face main or ModelScope master file URL")
    if (not REPOSITORY_SEGMENT.fullmatch(owner) or not REPOSITORY_SEGMENT.fullmatch(repo)
            or segments(file)[-1] != name_parts[-1]):
        raise ValueError("model filename does not match repository file")
    return category, name, f"{source}://{owner}/{repo}", file


class Downloads:
    def __init__(self, db, hub, shared, projection):
        self.db = db
        self.hub = hub
        self.shared = shared
        self.projection = projection
        self.lock = asyncio.Lock()
        db.execute("""CREATE TABLE IF NOT EXISTS model_aliases (
            category TEXT NOT NULL, name TEXT NOT NULL, repository TEXT NOT NULL,
            file TEXT NOT NULL, PRIMARY KEY(category, name))""")
        db.execute("""CREATE TABLE IF NOT EXISTS model_downloads (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, entries TEXT NOT NULL)""")

    async def call(self, session, path, *, params=None, body=None):
        async with session.request("GET" if body is None else "POST", self.hub + "/api/v1/" + path,
                                   params=params, json=body, allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=12)) as response:
            return response.status, await response.json() if response.ok else {}

    async def refresh(self, session):
        async with self.lock:
            aliases = self.db.execute("SELECT * FROM model_aliases").fetchall()
            return await models.refresh(session, self.hub, self.shared, self.projection, aliases)

    async def submit_repo(self, session, repo, entries):
        files = {item[3] for item in entries}
        status, found = await self.call(session, "models/files", params={"model_id": repo})
        if status == 200:
            files.difference_update(item["path"] for item in found["files"] if item.get("downloaded"))
        elif status not in (404, 409):
            return None, "scheduler_unavailable"
        if not files:
            return None, None
        status, result = await self.call(session, "models/pull", body={
            "source": repo.split(":", 1)[0], "model_id": repo, "files": sorted(files), "append": True,
        })
        if status in (200, 201, 202):
            task = result.get("task_id")
            if isinstance(task, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task):
                return task, None
        if status == 409:
            # Reuse the in-flight task if the exact requested files are covered.
            status, tasks = await self.call(session, "tasks")
            if status == 200:
                for task in tasks:
                    if task.get("model_id") != repo or task.get("phase") not in ACTIVE_PHASES:
                        continue
                    existing = task.get("files")
                    if existing is None or files.issubset(set(existing)):
                        return task["task_id"], None
            return None, "repository_busy"
        if status in (400, 422, 502):
            return None, "download_rejected"
        return None, "scheduler_unavailable"

    async def submit_group(self, session, repo, entries):
        try:
            task, error = await self.submit_repo(session, repo, entries)
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError):
            task, error = None, "scheduler_unavailable"
        return [{"directory": category, "name": name, "task_id": task, "error": error}
                for category, name, _, _ in entries]

    async def start(self, request, owner, session):
        try:
            values = (await request.json()).get("models")
            if not isinstance(values, list) or not 1 <= len(values) <= 64:
                raise ValueError("expected 1 to 64 models")
            entries = list(dict.fromkeys(model_request(value) for value in values))
        except (AttributeError, TypeError, ValueError) as error:
            raise web.HTTPBadRequest(reason="Invalid workflow model download") from error
        groups = defaultdict(list)
        for entry in entries:
            groups[entry[2]].append(entry)
        async with self.lock:
            # Reserve exact names before downloading. Never replace a binding to
            # a different repository file, even when the old file is absent.
            with self.db:
                for category, name, repo, file in entries:
                    previous = self.db.execute(
                        "SELECT repository,file FROM model_aliases WHERE category=? AND name=?", (category, name)
                    ).fetchone()
                    if previous is not None and previous != (repo, file):
                        raise web.HTTPConflict(reason="Workflow model path is already bound")
                    self.db.execute("INSERT OR IGNORE INTO model_aliases VALUES (?,?,?,?)", (category, name, repo, file))
        limit = asyncio.Semaphore(4)

        async def submit(repo, items):
            async with limit:
                return await self.submit_group(session, repo, items)

        batches = await asyncio.gather(*(submit(repo, items) for repo, items in groups.items()))
        results = [entry for batch in batches for entry in batch]
        batch_id = secrets.token_hex(16)
        with self.db:
            self.db.execute("INSERT INTO model_downloads VALUES (?,?,?)", (batch_id, owner, json.dumps(results)))
        return web.json_response({"id": batch_id}, status=202, headers={"Cache-Control": "no-store"})

    async def status(self, request, owner, session):
        row = self.db.execute("SELECT entries FROM model_downloads WHERE id=? AND owner=?",
                              (request.match_info["batch"], owner)).fetchone()
        if row is None:
            raise web.HTTPNotFound(reason="Model download not found")
        entries = json.loads(row[0])
        tasks = {}
        for task in {entry["task_id"] for entry in entries} - {None}:
            try:
                code, state = await self.call(session, "tasks/detail/" + task)
            except (aiohttp.ClientError, TimeoutError, ValueError):
                code, state = 503, {}
            if code not in (200, 404):
                raise web.HTTPServiceUnavailable(reason="Model Hub task state unavailable")
            tasks[task] = (code, state)
        def shared(entry):
            return (self.projection / entry["directory"] / entry["name"]).is_file()

        should_refresh = any(not e["error"] and not shared(e)
                             and (e["task_id"] is None or tasks[e["task_id"]][1].get("phase") == "READY")
                             for e in entries)
        refreshed = not should_refresh or await self.refresh(session)
        result = []
        for entry in entries:
            code, state = tasks.get(entry["task_id"], (200, {"phase": "READY"}))
            error = entry["error"]
            phase = state.get("phase")
            if code == 404:
                error = "task_missing"
            elif code == 200 and phase not in ACTIVE_PHASES | {"READY"}:
                error = "download_failed"
            elif phase == "READY" and refreshed and not shared(entry):
                error = "file_not_shared"
            status = "failed" if error else "ready" if phase == "READY" and refreshed else "downloading"
            result.append({**entry, "status": status, "error": error})
        return web.json_response({"models": result}, headers={"Cache-Control": "no-store"})
