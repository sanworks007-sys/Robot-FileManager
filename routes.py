import asyncio
import json
import mimetypes
import os
import shutil
import time
import uuid
import zipfile
from collections import deque
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import psutil
from aiohttp import ClientError, web
from server import PromptServer

from . import backup, catalog, installer, providers, terminal, transfers
from .security import DATA, SETTINGS, child, public_source_url, resolve, roots, settings, write_json
from .tasks import TASKS, Stopped, error_text, extract_archive, file_operation, measure_size, queue_download, trash_action, trash_list


routes = PromptServer.instance.routes
EVENTS = deque(maxlen=500)
SERVER_STARTED = time.time()


def log_event(kind, message):
    EVENTS.append({"time": datetime.now(timezone.utc).isoformat(),
                   "kind": kind, "message": message})


def guarded(function):
    @wraps(function)
    async def wrapper(request):
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() != request.host.lower():
            return web.json_response({"error": "Cross-origin request rejected"}, status=403)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return web.json_response({"error": "Cross-site request rejected"}, status=403)
        try:
            return await function(request)
        except (ValueError, KeyError, FileNotFoundError, FileExistsError, NotADirectoryError, PermissionError, OSError,
                json.JSONDecodeError, ClientError, asyncio.TimeoutError, zipfile.BadZipFile) as error:
            message = error_text(error)
            log_event("error", f"{request.path}: {message}")
            return web.json_response({"error": message}, status=400)
    return wrapper


async def body(request):
    if request.content_length and request.content_length > 1024 * 1024:
        raise ValueError("Request is too large")
    data = await request.json()
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


@routes.get("/robot/files/roots")
@guarded
async def list_roots(request):
    available = []
    for name, path in roots().items():
        if path.is_dir():
            usage = shutil.disk_usage(path)
            available.append({"id": name, "path": str(path), "total": usage.total, "used": usage.used, "free": usage.free})
    return web.json_response({"roots": available, "settings": settings()})


@routes.get("/robot/system/stats")
@guarded
async def system_stats(request):
    root_name = request.query.get("root", "output")
    folder = resolve(root_name, request.query.get("path", ""), must_exist=True)
    def collect():
        disk = shutil.disk_usage(folder)
        memory = psutil.virtual_memory()
        return {"root": root_name, "disk": {"total": disk.total, "used": disk.used, "free": disk.free},
                "cpu": psutil.cpu_percent(), "ram": {"total": memory.total, "used": memory.total - memory.available, "percent": memory.percent},
                "models": catalog.model_totals(request.query.get("refresh_models") == "1")}
    return web.json_response(await asyncio.to_thread(collect))


@routes.get("/robot/transfers")
@guarded
async def transfer_history(request):
    return web.json_response({"transfers": await asyncio.to_thread(transfers.history)})


@routes.get("/robot/terminal")
@guarded
async def terminal_status(request):
    return web.json_response({"available": terminal.available(), "commands": terminal.snapshot()})


@routes.post("/robot/terminal/run")
@guarded
async def terminal_run(request):
    data = await body(request)
    job = await asyncio.to_thread(terminal.run, data["command"], data["root"], data.get("path", ""))
    return web.json_response({"command": job.public()})


@routes.post("/robot/terminal/stop")
@guarded
async def terminal_stop(request):
    data = await body(request)
    return web.json_response({"command": terminal.stop(data["id"]).public()})


@routes.get("/robot/files/list")
@guarded
async def list_files(request):
    root_name = request.query.get("root", "output")
    relative = request.query.get("path", "")
    folder = resolve(root_name, relative, must_exist=True)
    if not folder.is_dir():
        raise ValueError("Choose a folder")
    entries = []
    with os.scandir(folder) as iterator:
        for item in iterator:
            if item.is_symlink() or Path(item.path).resolve() == DATA.resolve():
                continue
            try:
                info = item.stat(follow_symlinks=False)
                entries.append({"name": item.name, "folder": item.is_dir(follow_symlinks=False),
                                "size": info.st_size if item.is_file(follow_symlinks=False) else None,
                                "modified": info.st_mtime, "extension": Path(item.name).suffix.lower()})
            except OSError:
                continue
    entries.sort(key=lambda item: (not item["folder"], item["name"].lower()))
    return web.json_response({"root": root_name, "path": relative, "entries": entries})


@routes.post("/robot/files/mkdir")
@guarded
async def mkdir(request):
    data = await body(request)
    target = child(data["root"], data.get("path", ""), data["name"])
    target.mkdir()
    return web.json_response({"ok": True})


@routes.post("/robot/files/create")
@guarded
async def create_file(request):
    data = await body(request)
    target = child(data["root"], data.get("path", ""), data["name"])
    if target.suffix.lower() not in (".txt", ".md", ".json", ".csv", ".yaml", ".yml"):
        raise ValueError("Only simple text files can be created")
    target.touch(exist_ok=False)
    return web.json_response({"ok": True})


@routes.post("/robot/files/action")
@guarded
async def action(request):
    data = await body(request)
    action_name = data["action"]
    if action_name not in ("copy", "move", "rename", "duplicate", "delete"):
        raise ValueError("Unsupported file action")
    sources = data.get("sources", [])
    if not isinstance(sources, list) or not 1 <= len(sources) <= 1000:
        raise ValueError("Select 1 to 1000 files")
    permanent = bool(data["permanent"]) if "permanent" in data else None
    task = file_operation(action_name, sources, data.get("destination"), data.get("name"), permanent)
    return web.json_response({"task": task.id})


@routes.post("/robot/files/size")
@guarded
async def file_size(request):
    data = await body(request)
    return web.json_response({"task": measure_size(data["root"], data["path"]).id})


@routes.post("/robot/files/extract")
@guarded
async def extract_zip(request):
    data = await body(request)
    task = extract_archive(data["root"], data["path"], data["destination_root"], data.get("destination_folder", ""))
    return web.json_response({"task": task.id})


@routes.get("/robot/files/download")
@guarded
async def download_file(request):
    path = resolve(request.query["root"], request.query["path"], must_exist=True)
    if not path.is_file():
        raise ValueError("Choose a file")
    response = web.FileResponse(path)
    response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(path.name)}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    transfers.record("export", path.name, request.query["root"], request.query["path"], size=path.stat().st_size, status="requested")
    return response


@routes.get("/robot/files/preview")
@guarded
async def preview_file(request):
    path = resolve(request.query["root"], request.query["path"], must_exist=True)
    allowed = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".webm"}
    if not path.is_file() or path.suffix.lower() not in allowed:
        raise ValueError("Preview is unavailable for this file")
    response = web.FileResponse(path)
    response.headers["Content-Type"] = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "sandbox"
    return response


@routes.post("/robot/files/upload")
@guarded
async def upload_file(request):
    root_name = request.query["root"]
    folder = request.query.get("path", "")
    reader = await request.multipart()
    task = TASKS.external("upload", "Upload files")
    task.total = request.content_length
    names = []
    count = 0
    try:
        while part := await reader.next():
            if not part.filename:
                continue
            target = child(root_name, folder, part.filename)
            TASKS.reserve(target)
            temporary = target.with_name(target.name + ".robot-upload-" + uuid.uuid4().hex)
            transfer = transfers.record("upload", target.name, root_name, target.relative_to(roots()[root_name]).as_posix(), status="running")
            try:
                if target.exists():
                    raise FileExistsError(target.name)
                with temporary.open("xb") as stream:
                    while chunk := await part.read_chunk(4 * 1024 * 1024):
                        task.check()
                        stream.write(chunk)
                        count += len(chunk)
                        task.progress(count, request.content_length)
                if target.exists():
                    raise FileExistsError(target.name)
                os.replace(temporary, target)
                names.append(target.name)
                transfers.update(transfer, "completed", target.stat().st_size)
            except Stopped:
                transfers.update(transfer, "cancelled")
                raise
            except Exception:
                transfers.update(transfer, "failed")
                raise
            finally:
                temporary.unlink(missing_ok=True)
                TASKS.release(target)
        task.status = "completed"
        task.result = {"uploaded": names}
        TASKS.persist()
        return web.json_response({"uploaded": names, "task": task.id})
    except Stopped:
        task.status = "cancelled"
        TASKS.persist()
        raise ValueError("Upload cancelled")
    except Exception as error:
        task.status = "failed"
        task.error = error_text(error)
        TASKS.persist()
        raise


@routes.get("/robot/files/trash")
@guarded
async def list_trash(request):
    return web.json_response({"entries": trash_list()})


@routes.post("/robot/files/trash")
@guarded
async def modify_trash(request):
    data = await body(request)
    if data["action"] == "empty":
        for item in trash_list():
            trash_action(item["id"], "delete")
    else:
        trash_action(data["id"], data["action"])
    return web.json_response({"ok": True})


@routes.post("/robot/source/info")
@guarded
async def source_info(request):
    data = await body(request)
    result = await providers.source_info(data["value"])
    log_event("source", f"Resolved {result['platform']} source with {len(result['files'])} file(s)")
    return web.json_response(result)


@routes.post("/robot/source/search")
@guarded
async def source_search(request):
    data = await body(request)
    return web.json_response({"results": await providers.find_sources(data["filename"], data.get("sha256"))})


@routes.post("/robot/download/queue")
@guarded
async def enqueue(request):
    data = await body(request)
    items = data.get("items", [])
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise ValueError("Select 1 to 100 files")
    results = []
    skipped = []
    errors = []
    for item in items:
        if not isinstance(item, dict):
            errors.append({"name": "unknown", "error": "Invalid download item"})
            continue
        try:
            platform = providers.detect(item["url"])
            token = providers.credential(platform) if platform in ("huggingface", "civitai") else None
            task = queue_download(item["url"], item["root"], item.get("folder", ""), item["filename"], platform, token,
                                  {"model_type": item.get("model_type"), "model_name": item.get("model_name"), "sha256": item.get("sha256")})
            if task.status == "skipped":
                skipped.append({"name": task.name, "reason": task.result["skipped"], "task": task.id})
            else:
                results.append(task.id)
        except (ValueError, KeyError, OSError) as error:
            errors.append({"name": str(item.get("filename", "unknown"))[:100], "error": error_text(error)})
    return web.json_response({"tasks": results, "skipped": skipped, "errors": errors})


@routes.get("/robot/tasks")
@guarded
async def list_tasks(request):
    return web.json_response({"tasks": TASKS.snapshot()})


@routes.post("/robot/tasks/control")
@guarded
async def control_task(request):
    data = await body(request)
    if data["action"] not in ("pause", "resume", "cancel", "retry", "remove"):
        raise ValueError("Unknown task action")
    return web.json_response({"task": TASKS.control(data["id"], data["action"]).public()})


@routes.post("/robot/tasks/bulk")
@guarded
async def bulk_control_tasks(request):
    data = await body(request)
    action = data.get("action")
    if action not in ("retry_failed_downloads", "clear_completed_downloads"):
        raise ValueError("Unknown bulk task action")
    count = 0
    for task in TASKS.snapshot():
        if task["kind"] != "download":
            continue
        if action == "retry_failed_downloads" and task["status"] == "failed" and task["retryable"]:
            TASKS.control(task["id"], "retry")
            count += 1
        elif action == "clear_completed_downloads" and task["status"] == "completed":
            TASKS.control(task["id"], "remove")
            count += 1
    return web.json_response({"count": count})


@routes.get("/robot/models")
@guarded
async def models(request):
    found = await asyncio.to_thread(catalog.scan_models)
    log_event("models", f"Scanned {len(found)} installed model(s)")
    return web.json_response({"models": found})


@routes.get("/robot/models/missing")
@guarded
async def missing_models(request):
    return web.json_response({"models": await asyncio.to_thread(catalog.missing_models)})


@routes.post("/robot/models/hash")
@guarded
async def hash_model(request):
    data = await body(request)
    return web.json_response({"task": catalog.hash_model(data["root"], data["path"]).id})


@routes.post("/robot/models/locate")
@guarded
async def locate_model(request):
    data = await body(request)
    return web.json_response(await asyncio.to_thread(catalog.locate_model, data["root"], data["path"]))


@routes.post("/robot/models/source")
@guarded
async def model_source(request):
    data = await body(request)
    path = resolve(data["root"], data["path"], must_exist=True)
    if not path.is_file():
        raise ValueError("Choose a model file")
    public_source_url(data["url"])
    catalog.save_model_source(path, data["platform"], data["url"], {"model_type": data.get("model_type")})
    return web.json_response({"ok": True})


@routes.get("/robot/workflows")
@guarded
async def workflows(request):
    return web.json_response({"workflows": await asyncio.to_thread(catalog.workflow_files)})


@routes.get("/robot/custom-nodes")
@guarded
async def installed_custom_nodes(request):
    return web.json_response({"packages": await asyncio.to_thread(catalog.installed_custom_nodes)})


@routes.post("/robot/custom-nodes/upload")
@guarded
async def upload_custom_node(request):
    identifier = uuid.uuid4().hex
    folder = installer.STAGING / identifier
    uploaded = folder / "uploaded"
    uploaded.mkdir(parents=True)
    seen = set()
    try:
        reader = await request.multipart()
        while part := await reader.next():
            if not part.filename:
                continue
            target = installer.upload_path(uploaded, unquote(part.filename))
            if target is None:
                continue
            key = target.relative_to(uploaded).as_posix().casefold()
            if key in seen or len(seen) >= 100000:
                raise ValueError("Duplicate upload path or too many node files")
            seen.add(key)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                while chunk := await part.read_chunk(4 * 1024 * 1024):
                    if shutil.disk_usage(folder).free < len(chunk):
                        raise OSError("Insufficient space for this upload")
                    output.write(chunk)
        if not seen:
            raise ValueError("Choose a ZIP, Python file, or custom node folder")
        summary = await asyncio.to_thread(installer.prepare, identifier)
        return web.json_response({"package": summary})
    except Exception:
        shutil.rmtree(folder, ignore_errors=True)
        raise


@routes.post("/robot/custom-nodes/install")
@guarded
async def install_uploaded_custom_node(request):
    data = await body(request)
    task = installer.install(data["id"], data["name"], bool(data.get("requirements", True)))
    return web.json_response({"task": task.id})


@routes.get("/robot/custom-nodes/install-history")
@guarded
async def custom_node_install_history(request):
    return web.json_response({"started": SERVER_STARTED, "installs": await asyncio.to_thread(installer.history)})


@routes.post("/robot/system/restart")
@guarded
async def restart_server(request):
    data = await body(request)
    if data.get("confirm_restart") is not True:
        raise ValueError("Confirm the ComfyUI restart")
    if PromptServer.instance.prompt_queue.get_tasks_remaining() or any(
            task["status"] in ("waiting", "running", "pausing", "cancelling") for task in TASKS.snapshot()) or any(
            job["status"] in ("running", "stopping") for job in terminal.snapshot()):
        raise ValueError("Finish running or queued workflows, file tasks, and commands before restarting ComfyUI")
    asyncio.get_running_loop().call_later(1, installer.restart_process)
    return web.json_response({"restarting": True, "started": SERVER_STARTED})


@routes.post("/robot/custom-nodes/install-requirements")
@guarded
async def install_custom_node_requirements(request):
    data = await body(request)
    task = backup.install_local_requirements(data["package"])
    log_event("restore", "Queued custom node requirements installation")
    return web.json_response({"task": task.id})


@routes.post("/robot/workflows/analyze")
@guarded
async def analyze_workflow(request):
    data = await body(request)
    path = resolve(data["root"], data["path"], must_exist=True)
    report = await asyncio.to_thread(catalog.analyze_workflow, path)
    log_event("workflow", f"Analyzed workflow: {len(report['models'])} model(s), {len(report['custom_nodes'])} custom node(s)")
    return web.json_response(report)


@routes.post("/robot/backup/preview")
@guarded
async def backup_preview(request):
    data = await body(request)
    return web.json_response(await asyncio.to_thread(backup.preview, data))


@routes.post("/robot/backup/create")
@guarded
async def backup_create(request):
    data = await body(request)
    task = backup.create_backup(data)
    log_event("backup", "Queued workspace export")
    return web.json_response({"task": task.id})


@routes.post("/robot/files/zip")
@guarded
async def files_zip(request):
    data = await body(request)
    return web.json_response({"task": backup.create_selection_zip(data["root"], data.get("paths", [])).id})


@routes.get("/robot/backup/download/{identifier}")
@guarded
async def backup_download(request):
    identifier = request.match_info["identifier"]
    if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
        raise ValueError("Invalid export ID")
    path = backup.EXPORTS / (identifier + ".zip")
    if not path.is_file():
        raise FileNotFoundError(identifier)
    response = web.FileResponse(path)
    response.headers["Content-Disposition"] = "attachment; filename=robot-workspace-backup.zip"
    return response


@routes.post("/robot/backup/import")
@guarded
async def backup_import(request):
    reader = await request.multipart()
    part = await reader.next()
    if part is None or not part.filename or not part.filename.lower().endswith(".zip"):
        raise ValueError("Choose a ZIP backup")
    identifier = uuid.uuid4().hex
    path = backup.IMPORTS / (identifier + ".zip")
    try:
        with path.open("xb") as stream:
            while chunk := await part.read_chunk(4 * 1024 * 1024):
                stream.write(chunk)
        summary = await asyncio.to_thread(backup.validate_archive, path)
        log_event("restore", "Validated workspace backup archive")
        return web.json_response({"import_id": identifier, "summary": summary})
    except Exception:
        path.unlink(missing_ok=True)
        raise


@routes.get("/robot/backup/import/{identifier}")
@guarded
async def backup_analyze(request):
    path = backup.import_archive(request.match_info["identifier"])
    return web.json_response(await asyncio.to_thread(backup.validate_archive, path))


@routes.post("/robot/backup/restore")
@guarded
async def backup_restore(request):
    data = await body(request)
    task = backup.restore_backup(data["import_id"], workflows=bool(data.get("workflows", True)),
                                 outputs=bool(data.get("outputs", False)), overwrite=bool(data.get("overwrite", False)),
                                 comfy_settings=bool(data.get("comfy_settings", False)),
                                 plugin_settings=bool(data.get("plugin_settings", False)),
                                 custom_nodes=data.get("custom_nodes", []),
                                 install_requirements=bool(data.get("install_requirements", False)))
    log_event("restore", "Queued workspace restore")
    return web.json_response({"task": task.id})


@routes.get("/robot/logs")
@guarded
async def logs(request):
    return web.json_response({"events": list(reversed(EVENTS))})


@routes.get("/robot/settings")
@guarded
async def get_settings(request):
    return web.json_response({"settings": settings(), "credentials": {name: bool(providers.credential(name)) for name in ("huggingface", "civitai")}})


@routes.post("/robot/settings")
@guarded
async def set_settings(request):
    data = await body(request)
    current = settings()
    if "trash" in data:
        current["trash"] = bool(data["trash"])
    if "export_ttl_hours" in data:
        current["export_ttl_hours"] = min(168, max(1, int(data["export_ttl_hours"])))
    write_json(SETTINGS, current, private=True)
    return web.json_response({"settings": current})


@routes.post("/robot/settings/credential")
@guarded
async def credential(request):
    data = await body(request)
    providers.set_credential(data["platform"], data.get("token", ""))
    return web.json_response({"configured": bool(providers.credential(data["platform"]))})


backup.schedule_cleanup()
