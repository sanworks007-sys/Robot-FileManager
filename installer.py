import ast
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path

import nodes

from .security import DATA, child, read_json, relative_path, resolve, write_json
from .tasks import TASKS, error_text


STAGING = DATA / "node_uploads"
STAGING.mkdir(exist_ok=True)
HISTORY = DATA / "node_installs.json"
HISTORY_LOCK = threading.Lock()
SKIP_FOLDERS = {".git", "__pycache__", ".venv", "venv", "node_modules", "__MACOSX"}


def upload_path(folder, filename):
    if "\\" in filename:
        raise ValueError("Use forward slashes in uploaded paths")
    relative = relative_path(filename)
    if not relative.parts:
        raise ValueError("Invalid upload filename")
    if any(part in SKIP_FOLDERS for part in relative.parts) or relative.name.lower().startswith(".env"):
        return None
    return folder / relative


def stage(identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{32}", identifier):
        raise ValueError("Invalid node upload ID")
    folder = STAGING / identifier
    if not folder.is_dir():
        raise FileNotFoundError("Upload the node package again")
    return folder


def prepare(identifier):
    folder = stage(identifier)
    uploaded = folder / "uploaded"
    entries = list(uploaded.iterdir())
    source_name = entries[0].stem if len(entries) == 1 else "custom-node"
    if len(entries) == 1 and entries[0].suffix.lower() == ".zip":
        content = folder / "content"
        content.mkdir()
        with zipfile.ZipFile(entries[0]) as archive:
            items = archive.infolist()
            if len(items) > 100000:
                raise ValueError("ZIP has too many files")
            total = sum(item.file_size for item in items)
            if total > shutil.disk_usage(folder).free:
                raise OSError("Insufficient free space to extract this node")
            seen = set()
            for item in items:
                target = upload_path(content, item.filename)
                if target is None:
                    continue
                canonical = item.filename.rstrip("/").casefold()
                if canonical in seen or stat.S_IFMT(item.external_attr >> 16) == stat.S_IFLNK or item.flag_bits & 1:
                    raise ValueError("ZIP contains duplicate, linked, or encrypted files")
                seen.add(canonical)
                if item.file_size > 100 * 1024 * 1024 and item.file_size > max(1, item.compress_size) * 1000:
                    raise ValueError("ZIP entry expands unexpectedly")
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 4 * 1024 * 1024)
    else:
        content = uploaded
    entries = list(content.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        content = entries[0]
        source_name = content.name
        entries = list(content.iterdir())
    kind = "file" if len(entries) == 1 and entries[0].is_file() and entries[0].suffix == ".py" and entries[0].name != "__init__.py" else "folder"
    if kind == "folder" and not (content / "__init__.py").is_file():
        raise ValueError("Choose a node folder containing __init__.py, a ZIP of that folder, or a single .py node")
    files = [path for path in content.rglob("*") if path.is_file()]
    for path in files:
        if path.suffix == ".py":
            try:
                ast.parse(path.read_bytes(), filename=path.name)
            except SyntaxError as error:
                raise ValueError(f"Python syntax error in {path.relative_to(content)}: {error.msg}") from error
    requirements = content / "requirements.txt"
    if requirements.is_file() and requirements.stat().st_size > 64 * 1024:
        raise ValueError("Requirements file is too large")
    name = entries[0].name if kind == "file" else re.sub(r"-(?:main|master)$", "", source_name)
    summary = {"id": identifier, "name": name, "kind": kind, "content": content.relative_to(folder).as_posix(),
               "files": len(files), "size": sum(path.stat().st_size for path in files),
               "requirements": requirements.read_text(encoding="utf-8-sig") if requirements.is_file() else None}
    write_json(folder / "package.json", summary, private=True)
    return describe(summary)


def loaded_nodes(name):
    module = "custom_nodes." + (name[:-3] if name.endswith(".py") else name)
    return sorted(key for key, node in nodes.NODE_CLASS_MAPPINGS.items() if getattr(node, "RELATIVE_PYTHON_MODULE", "") == module)


def describe(summary):
    result = {key: value for key, value in summary.items() if key != "content"}
    target = child("custom_nodes", "", summary["name"])
    result.update(exists=target.exists(), loaded_nodes=loaded_nodes(summary["name"]), destination="custom_nodes/" + summary["name"])
    return result


def run_pip(arguments, task, label, cwd):
    task.result = {"stage": label, "output": ""}
    process = subprocess.Popen([sys.executable, "-m", "pip", "--disable-pip-version-check", *arguments],
                               cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    def read_output():
        for line in process.stdout:
            task.result["output"] = (task.result["output"] + error_text(line) + "\n")[-16000:]
    reader = threading.Thread(target=read_output, name="robot-node-pip", daemon=True)
    reader.start()
    deadline = time.monotonic() + 1800
    try:
        while process.poll() is None:
            task.check()
            if time.monotonic() > deadline:
                raise TimeoutError("Installing node requirements timed out")
            time.sleep(0.2)
        reader.join(timeout=5)
        return process.returncode, task.result["output"]
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        reader.join(timeout=5)
        process.stdout.close()


def install(identifier, name, requirements=True):
    folder = stage(identifier)
    summary = read_json(folder / "package.json", None)
    if not summary:
        raise ValueError("Inspect the uploaded package first")
    if not isinstance(name, str) or name.startswith(".") or name == "__init__.py":
        raise ValueError("Choose a custom node name")
    target = child("custom_nodes", "", name)
    if summary["kind"] == "file" and not name.endswith(".py"):
        raise ValueError("Single-file custom nodes must use a .py filename")
    if summary["kind"] == "folder" and name.endswith(".py"):
        raise ValueError("Choose a folder name for this package")
    if target.exists():
        raise FileExistsError("This custom node already exists; choose another name or install its existing requirements")
    content = folder / relative_path(summary["content"])
    def run(task):
        TASKS.reserve(target)
        log = ""
        installed = False
        try:
            if target.exists():
                raise FileExistsError(name)
            task.check()
            temporary = target.parent / (".robot-install-" + uuid.uuid4().hex)
            try:
                if summary["kind"] == "file":
                    source = next(path for path in content.iterdir() if path.suffix == ".py")
                    shutil.copyfile(source, temporary)
                else:
                    shutil.copytree(content, temporary)
                task.check()
                if target.exists():
                    raise FileExistsError(name)
                temporary.rename(target)
                installed = True
            finally:
                if temporary.is_dir():
                    shutil.rmtree(temporary)
                else:
                    temporary.unlink(missing_ok=True)
            if requirements and summary["requirements"] is not None:
                code, log = run_pip(["install", "--no-input", "-r", str(target / "requirements.txt")], task, "Installing requirements", target)
                if code:
                    raise OSError("Requirements installation failed. See the installer output.")
                check_code, check_output = run_pip(["check"], task, "Checking Python dependencies", target)
            else:
                check_code, check_output = 0, "Requirements installation was skipped." if summary["requirements"] is not None else "No requirements file to install."
            task.check()
            result = {"package": name, "installed": target.exists(), "requirements_installed": bool(requirements and summary["requirements"] is not None),
                      "dependency_check": check_code == 0 if requirements and summary["requirements"] is not None else None,
                      "output": (log + "\n" + check_output)[-16000:], "restart_required": True}
            with HISTORY_LOCK:
                history = read_json(HISTORY, [])
                history.append({"id": identifier, "name": name, "created": time.time(), **result})
                write_json(HISTORY, history, private=True)
            shutil.rmtree(folder, ignore_errors=True)
            return result
        except Exception:
            if installed:
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
            raise
        finally:
            TASKS.release(target)
    return TASKS.add("install-node", "Install custom node: " + name, run)


def history():
    return [describe(item) for item in reversed(read_json(HISTORY, []))]


def restart_process():
    if "__COMFY_CLI_SESSION__" in os.environ:
        Path(os.environ["__COMFY_CLI_SESSION__"] + ".reboot").touch()
        os._exit(0)
    arguments = [sys.executable, *sys.orig_argv[1:]]
    arguments = [value for value in arguments if value != "--windows-standalone-build"]
    if os.name == "nt":
        arguments = [subprocess.list2cmdline([value]) for value in arguments]
    os.execv(sys.executable, arguments)
