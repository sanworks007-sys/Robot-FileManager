import ast
import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

from server import PromptServer
from aiohttp import ClientError

from . import installer, providers, terminal
from .backup import VERSION
from .security import DATA, inside, read_json, reject_symlinks, relative_path, settings, write_json
from .tasks import TASKS, _download


ROOT = Path(__file__).resolve().parent
SOURCE = "https://github.com/sanworks007-sys/Robot-FileManager"
STATE = DATA / "self_update.json"
RECOVERY = DATA / "self_update_recovery"
LOCK = threading.Lock()
SESSION = uuid.uuid4().hex
IGNORE = {".git", "__pycache__", ".test-temp", ".pytest_cache", "node_modules", ".venv", "venv"}


def files(folder):
    result = {}
    for parent, dirs, names in os.walk(folder, followlinks=False):
        dirs[:] = [name for name in dirs if name not in IGNORE and not (Path(parent) / name).is_symlink()]
        for name in names:
            path = Path(parent) / name
            if path.is_symlink() or name.endswith((".pyc", ".robot-part", ".robot-part.json")):
                continue
            relative = path.relative_to(folder).as_posix()
            relative_path(relative)
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def target_path(name):
    target = ROOT
    for part in relative_path(name).parts:
        target /= part
        if target.is_symlink():
            raise ValueError("Plugin update path contains a symbolic link")
    if not inside(target.resolve(), ROOT.resolve()):
        raise ValueError("Plugin update path is outside its installation")
    return target


def git(*arguments, check=True):
    executable = shutil.which("git")
    if not executable:
        raise ValueError("Install Git to update this Git checkout")
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_ALLOW_PROTOCOL": "https"}
    command = [executable, "-c", "safe.directory=" + str(ROOT.resolve()), "-c", "core.hooksPath=" + os.devnull, "-c", "credential.helper=", "-C", str(ROOT), *arguments]
    result = subprocess.run(command, capture_output=True, timeout=120, env=env)
    if check and result.returncode:
        raise ValueError("Git update operation failed: " + result.stderr.decode("utf-8", errors="replace")[-300:])
    return result


def initialize():
    state = read_json(STATE, {})
    if "baseline" not in state:
        state.update(baseline=files(ROOT), installed_version=VERSION, repository=SOURCE)
        write_json(STATE, state, private=True)
    if state.get("restart_required") and state.get("pending_session") != SESSION:
        state.update(restart_required=False, installed_version=VERSION)
        write_json(STATE, state, private=True)
    return state


def local_status():
    state = initialize()
    checkout = (ROOT / ".git").exists()
    current = git("rev-parse", "HEAD").stdout.decode().strip() if checkout else state.get("installed_commit")
    changes = git("status", "--porcelain").stdout.decode().strip() if checkout else files(ROOT) != state["baseline"]
    return {"version": VERSION, "installed_commit": current, "repository": SOURCE, "local_changes": bool(changes),
            "git_checkout": checkout, "latest": state.get("latest"), "checked_at": state.get("checked_at"),
            "last_error": state.get("last_error"), "restart_required": state.get("restart_required", False),
            "recovery_available": (RECOVERY / "manifest.json").is_file()}


async def check_updates():
    repository = SOURCE.split("github.com/", 1)[1]
    info = await providers.request_json("https://api.github.com/repos/" + repository)
    branch = info["default_branch"]
    if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,200}", branch) or ".." in branch:
        raise ValueError("Invalid update branch")
    latest = await providers.request_json("https://api.github.com/repos/" + repository + "/commits/" + quote(branch, safe=""))
    commit = latest.get("sha", "")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Invalid GitHub update revision")
    version = None
    try:
        source = await providers.request_json("https://api.github.com/repos/" + repository + "/contents/backup.py?ref=" + commit)
        if source.get("encoding") == "base64":
            tree = ast.parse(base64.b64decode(source["content"]))
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "VERSION" for target in node.targets) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                    version = node.value.value[:50]
    except (ValueError, KeyError, SyntaxError, ClientError):
        version = None
    local = local_status()
    available = commit != local["installed_commit"]
    if local["git_checkout"] and available:
        comparison = await providers.request_json("https://api.github.com/repos/" + repository + "/compare/" + local["installed_commit"] + "..." + commit)
        available = comparison.get("status") == "ahead"
        blocked = comparison.get("status") == "diverged"
    else:
        blocked = False
    with LOCK:
        state = initialize()
        if local_status()["installed_commit"] == commit:
            available = False
        state.update(latest={"commit": commit, "version": version, "branch": branch, "available": available, "diverged": blocked,
            "message": str(latest.get("commit", {}).get("message", ""))[:1000]}, checked_at=time.time(), last_error=None)
        write_json(STATE, state, private=True)
    return local_status()


def idle(current=None):
    return not PromptServer.instance.prompt_queue.get_tasks_remaining() and not any(
        item["id"] != current and item["status"] in ("waiting", "running", "pausing", "cancelling", "paused") for item in TASKS.snapshot()) and not any(
        item["status"] in ("running", "stopping") for item in terminal.snapshot())


def validate(folder):
    for name in ("__init__.py", "routes.py", "web/manager.js", "web/manager.css"):
        if not (folder / name).is_file():
            raise ValueError("Update is missing " + name)
    reject_symlinks(folder)
    for path in folder.rglob("*.py"):
        if not any(part in IGNORE for part in path.relative_to(folder).parts):
            ast.parse(path.read_bytes(), filename=path.name)
    if (folder / "requirements.txt").exists():
        raise ValueError("This update adds Python dependencies. Install it manually after reviewing requirements.txt")


def snapshot(current, names):
    RECOVERY.mkdir(exist_ok=True)
    with zipfile.ZipFile(RECOVERY / "files.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.write(target_path(name), name)
    write_json(RECOVERY / "manifest.json", {"files": names, "commit": current["installed_commit"], "git": current["git_checkout"]}, private=True)


def install_update():
    if not idle():
        raise ValueError("Finish workflows, downloads and file tasks before updating Robot File Manager")
    current = local_status()
    latest = current.get("latest")
    if current["local_changes"]:
        raise ValueError("Local plugin changes are protected. Commit or back them up and restore a clean installation before updating")
    if not latest or not latest["available"] or latest.get("diverged"):
        raise ValueError("Check for an available update first")

    def run(task):
        with LOCK:
            if not idle(task.id) or local_status()["local_changes"]:
                raise ValueError("The server became busy or plugin files changed; try updating again when idle")
            folder = DATA / ("self-update-" + uuid.uuid4().hex)
            uploaded = folder / "uploaded"
            uploaded.mkdir(parents=True)
            applied = False
            try:
                task.result = {"stage": "Downloading update"}
                archive_path = uploaded / "update.zip"
                asyncio.run(_download(task, "https://api.github.com/repos/" + SOURCE.split("github.com/", 1)[1] + "/zipball/" + latest["commit"], archive_path))
                with zipfile.ZipFile(archive_path) as archive:
                    seen = set()
                    total = 0
                    for entry in archive.infolist():
                        parts = relative_path(entry.filename).parts
                        if len(parts) < 2 or any(part in IGNORE for part in parts):
                            continue
                        name = Path(*parts[1:]).as_posix()
                        if name.casefold() in seen or (entry.external_attr >> 16) & 0o170000 == 0o120000 or entry.flag_bits & 1:
                            raise ValueError("Unsafe update archive")
                        seen.add(name.casefold())
                        total += entry.file_size
                        if len(seen) > 10000 or total > 256 * 1024 * 1024:
                            raise ValueError("Update archive is too large")
                        target = folder / "content" / relative_path(name)
                        if entry.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with archive.open(entry) as source, target.open("xb") as output:
                                shutil.copyfileobj(source, output, 1024 * 1024)
                content = folder / "content"
                validate(content)
                task.check()
                if not idle(task.id) or local_status()["local_changes"]:
                    raise ValueError("Server became busy or local files changed while preparing the update")
                old_files = files(ROOT)
                new_files = files(content)
                snapshot(current, old_files)
                task.result = {"stage": "Applying update"}
                if current["git_checkout"]:
                    origin = installer.github_repository(git("remote", "get-url", "origin").stdout.decode().strip())
                    if origin.removesuffix(".git").lower() != SOURCE.lower():
                        raise ValueError("Git origin differs from the configured update source")
                    git("fetch", "origin", latest["commit"])
                    git("merge-base", "--is-ancestor", current["installed_commit"], latest["commit"])
                    applied = True
                    git("merge", "--ff-only", latest["commit"])
                else:
                    applied = True
                    for name in new_files:
                        target = target_path(name)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        temporary = target.with_name(target.name + ".robot-update")
                        shutil.copyfile(content / name, temporary)
                        os.replace(temporary, target)
                    for name in set(old_files) - set(new_files):
                        target_path(name).unlink()
                state = initialize()
                state.update(baseline=files(ROOT), installed_commit=latest["commit"], restart_required=True, pending_session=SESSION)
                state["latest"]["available"] = False
                write_json(STATE, state, private=True)
                return {"stage": "Update installed. Restart ComfyUI to activate it.", "restart_required": True, "commit": latest["commit"]}
            except Exception:
                if applied:
                    restore_files()
                raise
            finally:
                shutil.rmtree(folder, onerror=installer.remove_readonly)
    return TASKS.add("self-update", "Update Robot File Manager", run)


def restore_files():
    manifest = read_json(RECOVERY / "manifest.json", None)
    if not manifest:
        raise ValueError("No recovery copy is available")
    if manifest["git"]:
        git("reset", "--hard", manifest["commit"])
    else:
        current = files(ROOT)
        with zipfile.ZipFile(RECOVERY / "files.zip") as archive:
            for name in manifest["files"]:
                target = target_path(name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
        for name in set(current) - set(manifest["files"]):
            target_path(name).unlink()
    state = initialize()
    state.update(baseline=files(ROOT), installed_commit=manifest["commit"], restart_required=True, pending_session=SESSION)
    write_json(STATE, state, private=True)


def rollback():
    if not idle() or local_status()["local_changes"]:
        raise ValueError("Finish active work and preserve local edits before rolling back")
    def run(task):
        with LOCK:
            if not idle(task.id) or local_status()["local_changes"]:
                raise ValueError("Server is busy or plugin files changed")
            restore_files()
            return {"restart_required": True, "stage": "Previous plugin version restored. Restart ComfyUI."}
    return TASKS.add("self-update", "Restore previous Robot File Manager", run)


def automatic():
    config = settings()
    if not config["check_updates"]:
        return
    try:
        result = local_status() if time.time() - read_json(STATE, {}).get("checked_at", 0) < 21600 else asyncio.run(check_updates())
        if config["auto_update"] and (result.get("latest") or {}).get("available") and not result["local_changes"] and idle():
            install_update()
    except Exception as error:
        state = initialize()
        state.update(last_error=str(error)[:300], checked_at=time.time())
        write_json(STATE, state, private=True)


def schedule_updates():
    def tick():
        automatic()
        timer = threading.Timer(3600, tick)
        timer.daemon = True
        timer.start()
    timer = threading.Timer(60, tick)
    timer.daemon = True
    timer.start()
