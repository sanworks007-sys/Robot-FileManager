import asyncio
import hashlib
import os
import re
import shutil
import socket
import sqlite3
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import aiohttp

from .security import DATA, PRIVATE, child, download_url, inside, public_url, read_json, reject_symlinks, relative_path, resolve, roots, safe_url, settings, write_json


class Stopped(Exception):
    pass


def error_text(error):
    message = re.sub(r"https?://[^\s]+", lambda match: download_url(match.group(0)), str(error))
    return re.sub(r"Bearer\s+[^\s]+", "Bearer [redacted]", message, flags=re.I)[:400]


class PublicResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=0):
        from .security import public_addresses
        addresses = await asyncio.to_thread(public_addresses, host)
        return [{"hostname": host, "host": address, "port": port,
                 "family": socket.AF_INET6 if ":" in address else socket.AF_INET, "proto": 0, "flags": 0}
                for address in addresses]

    async def close(self):
        pass


class Task:
    def __init__(self, kind, name, runner=None):
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.name = name
        self.runner = runner
        self.status = "waiting"
        self.bytes = 0
        self.total = None
        self.speed = 0
        self.error = None
        self.created = time.time()
        self.updated = self.created
        self.cancel = threading.Event()
        self.result = None

    def check(self):
        if self.cancel.is_set():
            raise Stopped()

    def progress(self, amount, total=None, started=None, initial=0):
        self.bytes = amount
        if total is not None:
            self.total = total
        self.updated = time.time()
        if started and self.updated > started:
            self.speed = (amount - initial) / (self.updated - started)

    def public(self):
        return {"id": self.id, "kind": self.kind, "name": self.name, "status": self.status, "bytes": self.bytes,
                "total": self.total, "speed": self.speed, "error": self.error, "created": self.created,
                "updated": self.updated, "result": self.result,
                "retryable": self.runner is not None and self.kind in ("download", "hash", "size", "backup", "zip")}


class TaskManager:
    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="robot-files")
        self.tasks = {}
        self.lock = threading.Lock()
        self.reserved = set()
        self.history_file = DATA / "task_history.json"
        for item in read_json(self.history_file, []):
            if isinstance(item, dict) and item.get("status") in ("completed", "failed", "cancelled", "skipped"):
                task = Task(item.get("kind", "task"), item.get("name", "Previous task"))
                task.id = item.get("id", task.id)
                task.status = item["status"]
                task.bytes = item.get("bytes", 0)
                task.total = item.get("total")
                task.error = item.get("error")
                task.result = item.get("result")
                task.created = item.get("created", task.created)
                task.updated = item.get("updated", task.updated)
                self.tasks[task.id] = task

    def persist(self):
        with self.lock:
            completed = [task.public() for task in self.tasks.values() if task.status in ("completed", "failed", "cancelled", "skipped")]
            write_json(self.history_file, sorted(completed, key=lambda item: item["created"])[-200:], private=True)

    def snapshot(self):
        with self.lock:
            return [task.public() for task in sorted(self.tasks.values(), key=lambda item: item.created, reverse=True)]

    def add(self, kind, name, runner):
        task = Task(kind, name, runner)
        with self.lock:
            self.tasks[task.id] = task
            if len(self.tasks) > 500:
                for old in sorted(self.tasks.values(), key=lambda item: item.created):
                    if old.status in ("completed", "failed", "cancelled", "skipped"):
                        del self.tasks[old.id]
                        if len(self.tasks) <= 400:
                            break
        self.executor.submit(self._run, task)
        return task

    def external(self, kind, name):
        task = Task(kind, name)
        task.status = "running"
        with self.lock:
            self.tasks[task.id] = task
        return task

    def _run(self, task):
        if not task.cancel.is_set():
            task.status = "running"
        task.updated = time.time()
        try:
            task.result = task.runner(task)
            task.status = "completed"
        except Stopped:
            task.status = "paused" if task.status == "pausing" else "cancelled"
        except Exception as error:
            task.error = error_text(error)
            task.status = "failed"
        task.updated = time.time()
        if task.status != "paused":
            self.persist()

    def control(self, task_id, action):
        with self.lock:
            task = self.tasks[task_id]
        if action in ("resume", "retry") and not task.public()["retryable"]:
            raise ValueError("This task cannot be retried safely")
        if action in ("pause", "cancel") and task.status in ("waiting", "running"):
            task.status = "pausing" if action == "pause" and task.kind == "download" else "cancelling"
            task.cancel.set()
        elif action in ("resume", "retry") and task.status in ("paused", "failed", "cancelled") and task.runner:
            task.cancel.clear()
            task.error = None
            task.status = "waiting"
            self.executor.submit(self._run, task)
        elif action == "remove" and task.status in ("completed", "failed", "cancelled", "paused", "skipped"):
            with self.lock:
                del self.tasks[task_id]
            self.persist()
        return task

    def reserve(self, path):
        with self.lock:
            if path in self.reserved:
                raise ValueError("Another task is already writing this file")
            self.reserved.add(path)

    def release(self, path):
        with self.lock:
            self.reserved.discard(path)


TASKS = TaskManager()


def measure_size(root_name, relative):
    path = resolve(root_name, relative, must_exist=True)
    def run(task):
        count = 0
        size = 0
        if path.is_file():
            return {"files": 1, "size": path.stat().st_size}
        for parent, dirs, files in os.walk(path, followlinks=False):
            task.check()
            dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink() and
                       not inside((Path(parent) / name).resolve(), PRIVATE)]
            for name in files:
                child_path = Path(parent) / name
                if not child_path.is_symlink() and not inside(child_path.resolve(), PRIVATE):
                    try:
                        size += child_path.stat().st_size
                        count += 1
                    except OSError:
                        pass
            task.progress(count)
        return {"files": count, "size": size}
    return TASKS.add("size", f"Measure {path.name}", run)


def extract_archive(root_name, relative, destination_root, destination_folder):
    source = resolve(root_name, relative, must_exist=True)
    destination = resolve(destination_root, destination_folder, must_exist=True)
    if not source.is_file() or source.suffix.lower() != ".zip" or not destination.is_dir():
        raise ValueError("Choose a ZIP and a destination folder")
    def run(task):
        with zipfile.ZipFile(source) as archive:
            if len(archive.infolist()) > 100000:
                raise ValueError("ZIP has too many entries")
            planned = []
            total = 0
            seen = set()
            for item in archive.infolist():
                name = item.filename
                parts = PurePosixPath(name).parts
                mode = (item.external_attr >> 16) & 0o170000
                if (not parts or name.startswith(("/", "\\")) or "\\" in name or "\x00" in name or
                        any(part in ("..", "") for part in parts) or ":" in parts[0] or mode == 0o120000):
                    raise ValueError("ZIP contains an unsafe path or symbolic link")
                relative_path(name)
                canonical = PurePosixPath(name).as_posix().rstrip("/").casefold()
                if canonical in seen or item.flag_bits & 1 or item.compress_type not in (
                        zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
                    raise ValueError("ZIP contains a duplicate, encrypted, or unsupported entry")
                seen.add(canonical)
                target = resolve(destination_root, (PurePosixPath(destination_folder.replace("\\", "/")) / PurePosixPath(name)).as_posix())
                if not item.is_dir():
                    if target.exists():
                        raise FileExistsError(target.name)
                    if item.file_size > max(1, item.compress_size) * 1000 and item.file_size > 100 * 1024 * 1024:
                        raise ValueError("ZIP entry expands unexpectedly")
                    planned.append((item, target))
                    total += item.file_size
            if shutil.disk_usage(destination).free < total:
                raise OSError("Insufficient free space for extraction")
            copied = 0
            for item, target in planned:
                task.check()
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(target.name + ".robot-extract-" + uuid.uuid4().hex)
                try:
                    with archive.open(item) as reader, temporary.open("xb") as writer:
                        while chunk := reader.read(8 * 1024 * 1024):
                            task.check()
                            writer.write(chunk)
                            copied += len(chunk)
                            task.progress(copied, total)
                    if target.exists():
                        raise FileExistsError(target.name)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            return {"files": len(planned), "size": copied}
    return TASKS.add("extract", source.name, run)


def transfer_file(source, target, task, initial=0, total=None, started=None):
    size = source.stat().st_size
    if shutil.disk_usage(target.parent).free < size:
        raise OSError("Insufficient free space for copy")
    total = total if total is not None else initial + size
    task.total = total
    started = started or time.time()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with source.open("rb") as reader, target.open("xb") as writer:
            while True:
                task.check()
                data = reader.read(8 * 1024 * 1024)
                if not data:
                    break
                writer.write(data)
                task.progress(initial + writer.tell(), total, started)
        shutil.copystat(source, target)
    except Exception:
        target.unlink(missing_ok=True)
        raise


def transfer_directory(source, target, task):
    total = 0
    for parent, dirs, files in os.walk(source, followlinks=False):
        task.check()
        for name in dirs + files:
            if (Path(parent) / name).is_symlink():
                raise ValueError("Folder contains a symbolic link")
        total += sum((Path(parent) / name).stat().st_size for name in files)
    task.total = total
    if shutil.disk_usage(target.parent).free < total:
        raise OSError("Insufficient free space for copy")
    started = time.time()
    copied = 0
    directories = []
    for parent, dirs, files in os.walk(source, followlinks=False):
        task.check()
        target_parent = target / Path(parent).relative_to(source)
        target_parent.mkdir(parents=True, exist_ok=True)
        directories.append((Path(parent), target_parent))
        for name in files:
            path = Path(parent) / name
            transfer_file(path, target_parent / name, task, copied, total, started)
            copied += path.stat().st_size
    for original, copied_folder in reversed(directories):
        shutil.copystat(original, copied_folder)


def file_operation(action, sources, destination=None, name=None, permanent=False):
    def run(task):
        items = [(item["root"], item["path"], resolve(item["root"], item["path"], must_exist=True)) for item in sources]
        if action in ("copy", "move", "duplicate", "rename"):
            if not destination:
                raise ValueError("Choose a destination folder")
            folder = resolve(destination["root"], destination.get("path", ""), must_exist=True)
            if not folder.is_dir():
                raise ValueError("Destination is not a folder")
        count = 0
        for root_name, relative, source in items:
            task.check()
            reject_symlinks(source)
            if source in roots().values():
                raise ValueError("Cannot modify a root folder")
            if action in ("copy", "move", "duplicate", "rename"):
                new_name = name if action in ("rename", "duplicate") else source.name
                target = child(destination["root"], destination.get("path", ""), new_name)
                if target.exists() or source == target:
                    raise FileExistsError(new_name)
                if source.is_dir() and inside(target, source):
                    raise ValueError("Cannot move a folder into itself")
                if action in ("copy", "duplicate"):
                    if source.is_dir():
                        try:
                            transfer_directory(source, target, task)
                        except Exception:
                            shutil.rmtree(target, ignore_errors=True)
                            raise
                    else:
                        transfer_file(source, target, task)
                else:
                    shutil.move(str(source), str(target))
            elif action == "delete":
                if settings()["trash"] and not permanent:
                    trash_item(root_name, relative, source)
                elif source.is_dir():
                    shutil.rmtree(source)
                else:
                    source.unlink()
            else:
                raise ValueError("Unknown file operation")
            count += 1
            task.progress(count, len(items))
        return {"count": count}
    return TASKS.add(action, f"{action.title()} {len(sources)} item(s)", run)


TRASH = DATA / "trash"
TRASH.mkdir(exist_ok=True)


def trash_item(root_name, relative, source):
    item_id = uuid.uuid4().hex
    folder = TRASH / item_id
    folder.mkdir()
    try:
        write_json(folder / "item.json", {"id": item_id, "root": root_name, "path": relative, "name": source.name,
                                           "deleted": time.time()}, private=True)
        shutil.move(str(source), str(folder / "content"))
    except Exception:
        if not (folder / "content").exists():
            shutil.rmtree(folder, ignore_errors=True)
        raise


def trash_list():
    result = []
    for folder in TRASH.iterdir():
        if folder.is_dir() and (folder / "item.json").is_file():
            result.append(read_json(folder / "item.json", {}))
    return sorted(result, key=lambda item: item.get("deleted", 0), reverse=True)


def trash_action(item_id, action):
    if not isinstance(item_id, str) or len(item_id) != 32 or not all(c in "0123456789abcdef" for c in item_id):
        raise ValueError("Invalid trash entry")
    folder = TRASH / item_id
    metadata = read_json(folder / "item.json", None)
    if metadata is None:
        raise FileNotFoundError(item_id)
    if action == "restore":
        target = resolve(metadata["root"], metadata["path"])
        if target.exists():
            raise FileExistsError(target.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder / "content"), str(target))
        shutil.rmtree(folder)
    elif action == "delete":
        shutil.rmtree(folder)
    else:
        raise ValueError("Unknown trash action")


async def _download(task, url, target, token=None):
    partial = target.with_name(target.name + ".robot-part")
    metadata_path = target.with_name(target.name + ".robot-part.json")
    if partial.is_symlink():
        raise ValueError("Unsafe partial download path")
    identity = hashlib.sha256(download_url(url).encode("utf-8")).hexdigest()
    partial_info = read_json(metadata_path, {}) if partial.exists() else {}
    if partial.exists() and partial_info.get("source") != identity:
        raise ValueError("Existing partial download belongs to a different source")
    offset = partial.stat().st_size if partial.exists() else 0
    timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=120)
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), ttl_dns_cache=0, limit=4)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False, auto_decompress=False) as session:
        current = url
        for _ in range(8):
            task.check()
            public_url(current)
            headers = {"Accept-Encoding": "identity"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            if offset and partial_info.get("etag"):
                headers["If-Range"] = partial_info["etag"]
            if token and urlsplit(current).hostname == urlsplit(url).hostname:
                headers["Authorization"] = f"Bearer {token}"
            async with session.get(current, headers=headers, allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    from urllib.parse import urljoin
                    current = urljoin(current, response.headers.get("Location", ""))
                    continue
                if response.status == 416 and offset:
                    partial.unlink()
                    offset = 0
                    current = url
                    continue
                if response.status not in (200, 206):
                    raise ValueError(f"Download failed with HTTP {response.status}")
                if (target.suffix.lower() in (".safetensors", ".ckpt", ".pt", ".pt2", ".pth", ".pkl", ".bin", ".gguf", ".onnx", ".sft") and
                        response.headers.get("Content-Type", "").split(";", 1)[0].lower() in ("text/html", "application/json")):
                    raise ValueError("Source returned a web page instead of a model file")
                if offset and response.status == 200:
                    offset = 0
                if response.status == 206 and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                    raise ValueError("Server returned a mismatched download range")
                write_json(metadata_path, {"source": identity, "etag": response.headers.get("ETag")})
                length = response.content_length
                content_range = response.headers.get("Content-Range", "")
                total = int(content_range.rsplit("/", 1)[1]) if response.status == 206 and "/" in content_range and content_range.rsplit("/", 1)[1].isdigit() else (offset + length if length is not None else None)
                if total and shutil.disk_usage(target.parent).free < max(0, total - offset):
                    raise OSError("Insufficient free space for download")
                started = time.time()
                with partial.open("ab" if offset else "wb") as stream:
                    count = offset
                    async for chunk in response.content.iter_chunked(4 * 1024 * 1024):
                        task.check()
                        stream.write(chunk)
                        count += len(chunk)
                        task.progress(count, total, started, offset)
                if total is not None and count != total:
                    raise OSError("Download ended before the expected size")
                os.replace(partial, target)
                metadata_path.unlink(missing_ok=True)
                return
        raise ValueError("Too many download redirects")


def queue_download(url, root_name, folder, filename, platform="direct", token=None, metadata=None):
    safe_url(url)
    expected = (metadata or {}).get("sha256")
    if expected and (not isinstance(expected, str) or len(expected) != 64 or any(char not in "0123456789abcdefABCDEF" for char in expected)):
        raise ValueError("Invalid expected SHA256")
    target = child(root_name, folder, filename)
    if target.exists():
        raise FileExistsError(filename)
    def run(task):
        TASKS.reserve(target)
        try:
            if target.exists():
                raise FileExistsError(filename)
            asyncio.run(_download(task, url, target, token))
            from .catalog import save_model_source
            if expected:
                digest = hashlib.sha256()
                with target.open("rb") as stream:
                    while chunk := stream.read(8 * 1024 * 1024):
                        digest.update(chunk)
                if digest.hexdigest().lower() != expected.lower():
                    target.unlink(missing_ok=True)
                    raise ValueError("Downloaded file SHA256 does not match the source")
            if platform in ("huggingface", "civitai", "direct"):
                try:
                    save_model_source(target, platform, url, metadata or {})
                except (OSError, ValueError, sqlite3.Error) as error:
                    task.error = "Download completed, but source metadata was not saved: " + error_text(error)
            return {"root": root_name, "path": str((Path(folder) / filename).as_posix())}
        except Stopped:
            if task.status == "cancelling":
                target.with_name(target.name + ".robot-part").unlink(missing_ok=True)
                target.with_name(target.name + ".robot-part.json").unlink(missing_ok=True)
            raise
        finally:
            TASKS.release(target)
    return TASKS.add("download", filename, run)
