"""Submit exact workflow weight files to the V-Scheduler Model Hub."""
import asyncio
import json
import re
import secrets
import time
from collections import defaultdict
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlsplit

import aiohttp
from aiohttp import web

import models

REPOSITORY_SEGMENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,95}")
ACTIVE_PHASES = {"PENDING", "DOWNLOADING", "VERIFYING"}
SOURCES = json.loads(Path(__file__).with_name("model-sources.json").read_text())


def mirror_for(repo, file):
    mirror = SOURCES.get(file)
    source, repository = repo.split("://", 1)
    return mirror if mirror and mirror.get(source, "").lower() == repository.lower() else None


def segments(value):
    parts = value.split("/")
    if len(value.encode()) > 512 or any(
        part in ("", ".", "..") or len(part.encode()) > 255 or re.search(r"[\\:\x00-\x1f\x7f]", part)
        for part in parts
    ):
        raise ValueError("invalid model path")
    return parts


def model_request(value, alternate=None):
    if not isinstance(value, dict) or any(not isinstance(value.get(k), str) for k in ("name", "directory", "url")):
        raise ValueError("invalid model request")
    category = models.ALIASES.get(value["directory"], value["directory"])
    name = value["name"]
    # Comfy's Linux loader accepts a saved Windows path as a literal filename.
    # Keep the workflow value byte-for-byte, including the separator it used.
    name_parts = segments(name.replace("\\", "/"))
    category_ok = REPOSITORY_SEGMENT.fullmatch(category) and category not in {
        "custom_nodes", "configs", "input", "output", "temp", "user"
    }
    directory_model = category == "diffusers"
    if (not category_ok or (not directory_model and PurePosixPath(name_parts[-1]).suffix.lower() not in models.WEIGHT_EXTENSIONS)
            or (len(name_parts) > 1 and name_parts[0] in {"hf", "ms"})
            or any(PurePosixPath(part).suffix.lower() in models.WEIGHT_EXTENSIONS for part in name_parts[:-1])):
        raise ValueError("unsupported model directory or format")
    url = urlsplit(value["url"])
    if url.scheme != "https" or url.fragment:
        raise ValueError("unsupported model URL")
    parts = unquote(url.path).strip("/").split("/")
    if directory_model and url.netloc == "huggingface.co" and (
        len(parts) == 2 or parts[2:4] == ["tree", "main"]
    ):
        source, owner, repo, file = "hf", parts[0], parts[1], "/".join(parts[4:])
    elif directory_model and url.netloc in {"modelscope.cn", "www.modelscope.cn"} and (
        len(parts) == 3 and parts[0] == "models"
        or len(parts) >= 5 and parts[0] == "models" and parts[3:5] == ["tree", "master"]
    ):
        source, owner, repo, file = "ms", parts[1], parts[2], "/".join(parts[5:])
    elif url.netloc == "huggingface.co" and len(parts) >= 5 and parts[2:4] == ["resolve", "main"]:
        source, owner, repo, file = "hf", parts[0], parts[1], "/".join(parts[4:])
    elif url.netloc in {"modelscope.cn", "www.modelscope.cn"} and len(parts) >= 6 and parts[0] == "models" and parts[3:5] == ["resolve", "master"]:
        source, owner, repo, file = "ms", parts[1], parts[2], "/".join(parts[5:])
    elif url.netloc in {"modelscope.cn", "www.modelscope.cn"} and len(parts) == 6 and parts[:3] == ["api", "v1", "models"] and parts[-1] == "repo":
        fields = parse_qs(url.query)
        if fields.get("Revision") != ["master"] or len(fields.get("FilePath", [])) != 1:
            raise ValueError("expected a ModelScope master file URL")
        source, owner, repo, file = "ms", parts[3], parts[4], fields["FilePath"][0]
    else:
        raise ValueError("expected a Hugging Face main or ModelScope master file URL")
    if directory_model and file.endswith("model_index.json"):
        file = str(PurePosixPath(file).parent)
        if file == ".":
            file = ""
    if (not REPOSITORY_SEGMENT.fullmatch(owner) or not REPOSITORY_SEGMENT.fullmatch(repo)
            or (not directory_model and segments(file)[-1] != name_parts[-1])):
        raise ValueError("model filename does not match repository file")
    if directory_model and file:
        segments(file)
    repository = f"{source}://{owner}/{repo}"
    selected = value.get("source", source)
    if selected not in ("hf", "ms"):
        raise ValueError("unknown model source")
    if mirror := mirror_for(repository, file):
        repository = f"{selected}://{mirror[selected]}"
    elif alternate and alternate.startswith(f"{selected}://"):
        repository = alternate
    elif selected != source:
        raise ValueError("no verified mirror for this file")
    return category, name, repository, file


def repository_files(metadata, category, file):
    available = {row["path"]: row for row in metadata["files"]}
    if category != "diffusers":
        return {file: available[file]}
    prefix = file + "/" if file else ""
    if prefix + "model_index.json" not in available:
        raise ValueError("Diffusers model_index.json is missing")
    # The built-in loader consumes these components. Preserve config and shard
    # indexes, without fetching repository scripts, training data or media.
    components = {"unet", "vae", "text_encoder", "text_encoder_2", "scheduler", "tokenizer", "tokenizer_2"}
    selected = {prefix + "model_index.json": available[prefix + "model_index.json"]}
    for path, info in available.items():
        relative = path.removeprefix(prefix)
        if not path.startswith(prefix) or relative.split("/", 1)[0] not in components:
            continue
        segments(relative)
        if PurePosixPath(relative).suffix.lower() in {".json", ".txt", ".model", ".safetensors", ".bin"}:
            selected[path] = info
    if not all(any(path.startswith(prefix + component + "/")
                   and PurePosixPath(path).suffix in {".safetensors", ".bin"} for path in selected)
               for component in ("unet", "vae", "text_encoder")):
        raise ValueError("unsupported Diffusers component layout")
    # Prefer safetensors when a component also publishes the same PyTorch variant.
    for path in tuple(selected):
        if path.endswith((".bin", ".bin.index.json")):
            component = path.removeprefix(prefix).split("/", 1)[0]
            if any(p.startswith(prefix + component + "/") and p.endswith(".safetensors") for p in selected):
                del selected[path]
    return selected


class Downloads:
    def __init__(self, db, hub, shared, projection):
        self.db = db
        self.hub = hub
        self.shared = shared
        self.projection = projection
        self.lock = asyncio.Lock()
        self.catalog_cache = {}
        db.execute("""CREATE TABLE IF NOT EXISTS model_aliases (
            category TEXT NOT NULL, name TEXT NOT NULL, repository TEXT NOT NULL,
            file TEXT NOT NULL, PRIMARY KEY(category, name))""")
        db.execute("""CREATE TABLE IF NOT EXISTS model_downloads (
            id TEXT PRIMARY KEY, owner TEXT NOT NULL, entries TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS model_mirrors (
            repository TEXT NOT NULL, file TEXT NOT NULL, alternate TEXT NOT NULL,
            PRIMARY KEY(repository, file))""")

    async def call(self, session, path, *, params=None, body=None):
        async with session.request("GET" if body is None else "POST", self.hub + "/api/v1/" + path,
                                   params=params, json=body, allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=12)) as response:
            return response.status, await response.json() if response.ok else {}

    async def refresh(self, session):
        async with self.lock:
            aliases = self.db.execute("SELECT * FROM model_aliases").fetchall()
            discovered = {(repo, file): alternate for repo, file, alternate in
                          self.db.execute("SELECT repository,file,alternate FROM model_mirrors")}
            for category, name, repo, file in tuple(aliases):
                if mirror := mirror_for(repo, file):
                    other = "ms" if repo.startswith("hf://") else "hf"
                    aliases.append((category, name, f"{other}://{mirror[other]}", file))
                elif alt := discovered.get((repo, file)):
                    aliases.append((category, name, alt, file))
            return await models.refresh(session, self.hub, self.shared, self.projection, aliases)

    async def catalog(self, session, repo):
        now = time.monotonic()
        if (cached := self.catalog_cache.get(repo)) and cached[0] > now:
            return cached[1]
        source = repo.split("://", 1)[0]
        status, metadata = await self.call(session, "models/repository-files",
                                           params={"source": source, "model_id": repo})
        if status != 200 or not isinstance(metadata.get("files"), list):
            raise ValueError("model repository metadata unavailable")
        if len(self.catalog_cache) >= 128:
            self.catalog_cache.clear()
        self.catalog_cache[repo] = (now + 60, metadata)
        return metadata

    async def alternate(self, session, entry):
        category, _, repo, file = entry
        source, name = repo.split("://", 1)
        other = "ms" if source == "hf" else "hf"
        if mirror := mirror_for(repo, file):
            return f"{other}://{mirror[other]}"
        candidate = f"{other}://{name}"
        try:
            original = repository_files(await self.catalog(session, repo), category, file)
            mirrored = repository_files(await self.catalog(session, candidate), category, file)
        except (aiohttp.ClientError, TimeoutError, ValueError, KeyError):
            return None
        if original.keys() != mirrored.keys():
            return None
        for name, item in original.items():
            digest = item.get("sha256")
            match = mirrored[name]
            if (not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)
                    or digest.lower() != str(match.get("sha256")).lower()
                    or item.get("size") != match.get("size")):
                return None
        return candidate

    @staticmethod
    async def values(request):
        try:
            values = (await request.json()).get("models")
            if not isinstance(values, list) or not 1 <= len(values) <= 64:
                raise ValueError("expected 1 to 64 models")
            return values
        except (AttributeError, TypeError, ValueError) as error:
            raise web.HTTPBadRequest(reason="Invalid workflow model download") from error

    async def choices(self, request, session):
        values = await self.values(request)
        limit = asyncio.Semaphore(4)

        async def resolve(value):
            try:
                entry = model_request(value)
            except ValueError:
                return []
            async with limit:
                alternate = await self.alternate(session, entry)
            source = entry[2].split("://", 1)[0]
            return ["hf", "ms"] if alternate else [source]

        return web.json_response({"choices": await asyncio.gather(*(resolve(value) for value in values))})

    async def submit_repo(self, session, repo, entries):
        files = set()
        for category, _, _, file in entries:
            if category == "diffusers":
                files.update(repository_files(await self.catalog(session, repo), category, file))
            else:
                files.add(file)
        status, found = await self.call(session, "models/files", params={"model_id": repo})
        if status == 200:
            files.difference_update(item["path"] for item in found["files"] if item.get("downloaded"))
        elif status not in (404, 409):
            return None, "scheduler_unavailable"
        if not files:
            return None, None
        verified = {file: mirror["sha256"] for file in files if (mirror := mirror_for(repo, file))}
        if verified:
            source = repo.split(":", 1)[0]
            status, metadata = await self.call(session, "models/repository-files",
                                               params={"source": source, "model_id": repo})
            if status != 200:
                return None, "scheduler_unavailable"
            hashes = {file["path"]: file.get("sha256") for file in metadata["files"]}
            if any(hashes.get(file) != expected for file, expected in verified.items()):
                return None, "mirror_mismatch"
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
        return [{"directory": category, "name": name, "source": repo.split(":", 1)[0],
                 "task_id": task, "error": error}
                for category, name, _, _ in entries]

    async def start(self, request, owner, session):
        values = await self.values(request)
        alternatives = {}
        entries = []
        try:
            for value in values:
                if not isinstance(value, dict):
                    raise ValueError("invalid model entry")
                base = model_request({key: item for key, item in value.items() if key != "source"})
                chosen = value.get("source")
                alternate = None
                if chosen and chosen != base[2].split("://", 1)[0]:
                    alternate = await self.alternate(session, base)
                    if alternate:
                        alternatives[(base[2], base[3])] = alternate
                        alternatives[(alternate, base[3])] = base[2]
                entries.append(model_request(value, alternate=alternate))
            entries = list(dict.fromkeys(entries))
        except (AttributeError, TypeError, ValueError) as error:
            raise web.HTTPBadRequest(reason="Invalid workflow model download") from error
        groups = defaultdict(list)
        for entry in entries:
            groups[entry[2]].append(entry)
        async with self.lock:
            # Reserve exact names before downloading. Never replace a binding to
            # a different repository file, even when the old file is absent.
            with self.db:
                self.db.executemany("INSERT OR REPLACE INTO model_mirrors VALUES (?,?,?)",
                                    [(repo, file, alt) for (repo, file), alt in alternatives.items()])
                for category, name, repo, file in entries:
                    if category == "diffusers" and any(
                        name.startswith(other + "/") or other.startswith(name + "/")
                        for (other,) in self.db.execute("SELECT name FROM model_aliases WHERE category='diffusers'")
                    ):
                        raise web.HTTPConflict(reason="Diffusers paths cannot overlap")
                    previous = self.db.execute(
                        "SELECT repository,file FROM model_aliases WHERE category=? AND name=?", (category, name)
                    ).fetchone()
                    mirror = mirror_for(repo, file)
                    if previous is not None and previous != (repo, file) and (
                        (not mirror or mirror_for(*previous) != mirror)
                        and alternatives.get((repo, file)) != previous[0]
                    ):
                        raise web.HTTPConflict(reason="Workflow model path is already bound")
                    self.db.execute("""INSERT INTO model_aliases VALUES (?,?,?,?)
                        ON CONFLICT(category,name) DO UPDATE SET repository=excluded.repository,file=excluded.file""",
                                    (category, name, repo, file))
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
            path = self.projection / entry["directory"] / entry["name"]
            return path.is_symlink() and path.is_dir() if entry["directory"] == "diffusers" else path.is_file()

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
