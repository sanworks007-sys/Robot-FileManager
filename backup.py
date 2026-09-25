import json
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
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import folder_paths

from .catalog import MODEL_EXTENSIONS, analyze_workflow, custom_node_manifest, database, scan_models, workflow_files
from .security import DATA, PRIVATE, SETTINGS, inside, public_source_url, read_json, relative_path, resolve, roots, settings, write_json
from .tasks import TASKS


FORMAT = "robot-file-manager-backup"
SCHEMA = 2
VERSION = "1.1.0"
COMFY_SETTINGS_ALLOWLIST = {"Comfy.Minimap.Visible", "Comfy.Queue.History.Expanded", "Comfy.RightSidePanel.IsOpen",
                            "Comfy.Templates.SelectedRunsOn", "Comfy.Templates.SortBy", "Comfy.TutorialCompleted"}
EXPORTS = DATA / "exports"
IMPORTS = DATA / "imports"
EXPORTS.mkdir(exist_ok=True)
IMPORTS.mkdir(exist_ok=True)


def bundled_custom_node_files(names):
    if not isinstance(names, list) or len(names) > 100 or any(not isinstance(name, str) for name in names) or len(names) != len({name.casefold() for name in names}):
        raise ValueError("Select up to 100 distinct custom node folders")
    base = Path(folder_paths.base_path).resolve() / "custom_nodes"
    files = []
    for name in names:
        if not isinstance(name, str) or len(name) > 200 or name.startswith(".") or len(relative_path(name).parts) != 1:
            raise ValueError("Invalid custom node folder")
        folder = resolve("custom_nodes", name, must_exist=True)
        if not folder.is_dir():
            raise ValueError("Select a custom node folder")
        for parent, dirs, filenames in os.walk(folder, followlinks=False):
            dirs[:] = [item for item in dirs if item not in (".git", "__pycache__", ".venv", "venv", "node_modules") and
                       not (Path(parent) / item).is_symlink()]
            for filename in filenames:
                path = Path(parent) / filename
                if (path.is_symlink() or path.suffix.lower() in MODEL_EXTENSIONS | {".pyc", ".pem", ".key"} or
                        filename.lower().startswith(".env") or filename.lower() in (".git", "credentials.json", "secrets.json")):
                    continue
                relative_path(path.relative_to(base).as_posix())
                files.append(("custom_nodes/" + path.relative_to(base).as_posix(), path))
                if len(files) > 100000:
                    raise ValueError("Too many custom node files")
    return names, files


def requirement_specs(raw):
    if len(raw) > 64 * 1024:
        raise ValueError("Custom node requirements are too large")
    packages = []
    for line in raw.decode("utf-8").splitlines():
        value = line.split("#", 1)[0].strip()
        if not value:
            continue
        if len(value) > 500 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?(?:[<>=!~; \t0-9A-Za-z._,'\"()-]+)?", value):
            raise ValueError("Requirements contain an unsupported package source or pip option")
        packages.append(value)
        if len(packages) > 200:
            raise ValueError("Too many custom node requirements")
    return packages


def install_specs(packages, task):
    if not packages:
        return 0
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", *dict.fromkeys(packages)]
    process = subprocess.Popen(command, cwd=folder_paths.base_path, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 1800
    try:
        while process.poll() is None:
            task.check()
            if time.monotonic() > deadline:
                raise TimeoutError("Installing custom node requirements timed out")
            time.sleep(0.5)
    except Exception:
        if process.poll() is None:
            process.terminate()
        process.wait()
        raise
    if process.returncode:
        raise OSError("Installing custom node requirements failed in the ComfyUI Python environment")
    return len(packages)


def install_package_requirements(archive, names, task):
    packages = []
    for name in names:
        entry = "custom_nodes/" + name + "/requirements.txt"
        if entry not in archive.namelist():
            continue
        packages.extend(requirement_specs(archive.read(entry)))
    if len(packages) > 200:
        raise ValueError("Too many custom node requirements")
    return install_specs(packages, task)


def install_local_requirements(name):
    if not isinstance(name, str) or name.startswith(".") or len(relative_path(name).parts) != 1:
        raise ValueError("Invalid custom node folder")
    folder = resolve("custom_nodes", name, must_exist=True)
    if not folder.is_dir():
        raise ValueError("Choose an installed custom node folder")
    path = resolve("custom_nodes", name + "/requirements.txt", must_exist=True)
    if path.stat().st_size > 64 * 1024:
        raise ValueError("Custom node requirements are too large")
    packages = requirement_specs(path.read_bytes())
    return TASKS.add("requirements", "Install requirements for " + name,
                     lambda task: {"requirements_installed": install_specs(packages, task)})


def output_files(options):
    mode = options.get("outputs", "skip")
    base = Path(folder_paths.get_output_directory()).resolve()
    if mode == "skip":
        return []
    if mode == "selected":
        paths = [resolve("output", value, must_exist=True) for value in options.get("selected_outputs", [])]
    elif mode == "folder":
        paths = [resolve("output", options.get("output_folder", ""), must_exist=True)]
    elif mode == "all":
        paths = [base]
    else:
        raise ValueError("Invalid output selection")
    extensions = {value.lower().lstrip(".") for value in options.get("extensions", []) if isinstance(value, str)}
    after = options.get("after")
    before = options.get("before")
    files = {}
    for selected in paths:
        if selected.is_file():
            candidates = [selected]
        else:
            candidates = (Path(parent) / name for parent, dirs, names in os.walk(selected, followlinks=False)
                          for name in names if not (Path(parent) / name).is_symlink())
        for path in candidates:
            if path.is_symlink() or not path.is_file() or path.suffix.lower() in MODEL_EXTENSIONS:
                continue
            modified = path.stat().st_mtime
            if extensions and path.suffix.lower().lstrip(".") not in extensions:
                continue
            if after and modified < datetime.fromisoformat(after).timestamp():
                continue
            if before and modified > datetime.fromisoformat(before).timestamp():
                continue
            files[path.relative_to(base).as_posix()] = path
    return sorted(files.items())


def backup_data(options):
    models = scan_models()
    workflow_entries = workflow_files() if options.get("workflows", True) else []
    reports = {}
    for entry in workflow_entries:
        path = resolve(entry["root"], entry["path"], must_exist=True)
        workflow_name = entry["root"] + "/" + entry["path"]
        try:
            reports[workflow_name] = analyze_workflow(path, models)
        except (ValueError, OSError, json.JSONDecodeError):
            reports[workflow_name] = {"models": [], "custom_nodes": []}
    usage = {}
    for workflow, report in reports.items():
        for model in report["models"]:
            usage.setdefault(model["name"].lower(), []).append(workflow)
    manifest_models = []
    if options.get("models", True):
        base = Path(folder_paths.base_path).resolve()
        root_map = roots()
        for item in models:
            category = item["root"].split(":")[1] if item["root"].startswith("model:") else item["type"]
            source = root_map[item["root"]] / item["path"]
            relative = source.relative_to(base).as_posix() if inside(source, base) else (PurePosixPath("models") / category / item["path"]).as_posix()
            record = {"name": item["name"], "model_type": item["type"], "relative_path": relative,
                      "expected_folder": str(PurePosixPath(relative).parent), "file_size": item["size"],
                      "used_by_workflows": usage.get(item["name"].lower(), [])}
            if item["sha256"]:
                record["sha256"] = item["sha256"]
            if item["source_url"] and options.get("sources", True):
                try:
                    public_source_url(item["source_url"])
                except ValueError:
                    pass
                else:
                    record["sources"] = {item["source_platform"] or "other": item["source_url"]}
            manifest_models.append(record)
        existing = {item["name"].lower() for item in manifest_models}
        saved_sources = {}
        if options.get("sources", True):
            with database() as connection:
                for path, platform, url in connection.execute("SELECT path, source_platform, source_url FROM models WHERE source_url IS NOT NULL"):
                    try:
                        public_source_url(url)
                    except ValueError:
                        continue
                    saved_sources.setdefault(path.replace("\\", "/").split("/")[-1].lower(), (platform, url))
        for name, workflows in usage.items():
            if name not in existing:
                record = {"name": name, "model_type": "other", "used_by_workflows": workflows}
                known = saved_sources.get(name)
                if known:
                    record["sources"] = {known[0] or "other": known[1]}
                manifest_models.append(record)
    outputs = output_files(options)
    custom = custom_node_manifest(reports) if options.get("custom_nodes", True) else []
    return workflow_entries, manifest_models, custom, outputs


def preview(options):
    workflows, models, custom, outputs = backup_data(options)
    output_size = sum(path.stat().st_size for _, path in outputs)
    packages, node_files = bundled_custom_node_files(options.get("bundled_custom_nodes", []))
    node_size = sum(path.stat().st_size for _, path in node_files)
    sources = {"huggingface": 0, "civitai": 0, "other": 0, "unknown": 0}
    for model in models:
        links = model.get("sources", {})
        if not links:
            sources["unknown"] += 1
        elif "huggingface" in links:
            sources["huggingface"] += 1
        elif "civitai" in links:
            sources["civitai"] += 1
        else:
            sources["other"] += 1
    return {"workflows": len(workflows), "models": len(models), "custom_nodes": len(custom),
            "bundled_custom_nodes": len(packages), "custom_node_size": node_size,
            "outputs": len(outputs), "output_size": output_size,
            "estimated_zip_size": output_size + node_size + sum(entry["size"] for entry in workflows) + 1024 * 1024 + 200 * (len(outputs) + len(node_files) + len(workflows)),
            "free_space": shutil.disk_usage(EXPORTS).free,
            "sources": sources}


def workflow_destination(entry):
    parts = PurePosixPath(entry["path"]).parts
    index = parts.index("workflows")
    relative = PurePosixPath(*parts[index + 1:]).as_posix()
    prefix = ("/".join(parts[:index]) or "_user") if entry["root"] == "user" else "_root"
    archive_name = "workflows/" + ("user/" if entry["root"] == "user" else "comfy/") + prefix + "/" + relative
    return {"archive": archive_name, "root": entry["root"], "path": entry["path"]}


def write_zip_file(archive, path, archive_name, task, completed, total, compression):
    info = zipfile.ZipInfo.from_file(path, archive_name)
    info.compress_type = compression
    written = 0
    with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as output:
        while chunk := source.read(8 * 1024 * 1024):
            task.check()
            output.write(chunk)
            written += len(chunk)
            task.progress(completed + written, total)
    return written


def create_backup(options):
    def run(task):
        workflows, models, custom, outputs = backup_data(options)
        packages, node_files = bundled_custom_node_files(options.get("bundled_custom_nodes", []))
        if packages and not options.get("custom_nodes", True):
            raise ValueError("Include the custom node manifest when bundling folders")
        for item in custom:
            item["bundled"] = item["package"] in packages
        output_size = sum(path.stat().st_size for _, path in outputs)
        node_size = sum(path.stat().st_size for _, path in node_files)
        if shutil.disk_usage(EXPORTS).free < output_size + node_size + 100 * 1024 * 1024:
            raise OSError("Not enough free space for the backup ZIP")
        identifier = uuid.uuid4().hex
        archive = EXPORTS / f"{identifier}.zip"
        destinations = [workflow_destination(entry) for entry in workflows]
        manifest = {"backup_format": FORMAT, "schema_version": SCHEMA, "plugin_version": VERSION,
                    "created_at": datetime.now(timezone.utc).isoformat(), "contains_outputs": bool(outputs),
                    "workflow_destinations": destinations, "bundled_custom_nodes": packages}
        plugin_settings = settings() if options.get("plugin_settings", True) else None
        comfy_settings = None
        if options.get("comfy_settings"):
            path = Path(folder_paths.get_user_directory()) / "default" / "comfy.settings.json"
            if path.is_file() and path.stat().st_size < 2 * 1024 * 1024:
                comfy_settings = safe_comfy_settings(read_json(path, {}))
        count = 0
        total = output_size + node_size + sum(entry["size"] for entry in workflows)
        task.total = total
        try:
            with zipfile.ZipFile(archive, "w", allowZip64=True) as output:
                for name, value in (("manifest.json", manifest), ("models_manifest.json", models),
                                    ("custom_nodes_manifest.json", custom),
                                    ("settings.json", {"plugin": plugin_settings, "comfy": comfy_settings})):
                    output.writestr(name, json.dumps(value, ensure_ascii=False, indent=2))
                for entry, destination in zip(workflows, destinations):
                    task.check()
                    path = resolve(entry["root"], entry["path"], must_exist=True)
                    count += write_zip_file(output, path, destination["archive"], task, count, total, zipfile.ZIP_DEFLATED)
                for relative, path in outputs:
                    task.check()
                    count += write_zip_file(output, path, "outputs/" + relative, task, count, total, zipfile.ZIP_STORED)
                for archive_name, path in node_files:
                    task.check()
                    count += write_zip_file(output, path, archive_name, task, count, total, zipfile.ZIP_DEFLATED)
        except Exception:
            archive.unlink(missing_ok=True)
            raise
        return {"export_id": identifier, "size": archive.stat().st_size}
    return TASKS.add("backup", "Export workspace", run)


def create_selection_zip(root_name, paths):
    if not isinstance(paths, list) or not 1 <= len(paths) <= 1000:
        raise ValueError("Select 1 to 1000 items")
    from .security import roots
    if root_name not in roots():
        raise ValueError("Unknown root")
    base = roots()[root_name]
    selected = [resolve(root_name, value, must_exist=True) for value in paths]
    def run(task):
        files = {}
        for source in selected:
            if source.is_file() and not source.is_symlink():
                files[source.relative_to(base).as_posix()] = source
            elif source.is_dir():
                for parent, dirs, names in os.walk(source, followlinks=False):
                    dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink() and
                               not inside((Path(parent) / name).resolve(), PRIVATE)]
                    for name in names:
                        path = Path(parent) / name
                        if not path.is_symlink() and not inside(path.resolve(), PRIVATE):
                            files[path.relative_to(base).as_posix()] = path
        total_size = sum(path.stat().st_size for path in files.values())
        if shutil.disk_usage(EXPORTS).free < total_size + 100 * 1024 * 1024:
            raise OSError("Not enough free space for the output ZIP")
        identifier = uuid.uuid4().hex
        archive = EXPORTS / (identifier + ".zip")
        task.total = total_size
        copied = 0
        try:
            with zipfile.ZipFile(archive, "w", allowZip64=True) as output:
                for relative, path in files.items():
                    task.check()
                    copied += write_zip_file(output, path, relative, task, copied, total_size, zipfile.ZIP_STORED)
        except Exception:
            archive.unlink(missing_ok=True)
            raise
        return {"export_id": identifier, "size": archive.stat().st_size, "files": len(files)}
    return TASKS.add("zip", "ZIP selected files", run)


def _safe_entries(archive):
    if len(archive.infolist()) > 100000:
        raise ValueError("Archive has too many entries")
    seen = set()
    total = 0
    for item in archive.infolist():
        name = item.filename
        parts = PurePosixPath(name).parts
        if not parts or name.startswith(("/", "\\")) or "\\" in name or any(part in ("..", "") for part in parts) or ":" in parts[0]:
            raise ValueError("Unsafe archive path")
        relative_path(name)
        canonical = PurePosixPath(name).as_posix().rstrip("/").casefold()
        if canonical in seen:
            raise ValueError("Archive has duplicate entries")
        seen.add(canonical)
        if item.flag_bits & 1 or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
            raise ValueError("Unsupported or encrypted archive entry")
        mode = (item.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise ValueError("Archive contains a symbolic link")
        if parts[0] not in ("manifest.json", "models_manifest.json", "custom_nodes_manifest.json", "settings.json", "workflows", "outputs", "custom_nodes"):
            raise ValueError("Archive contains an unexpected file")
        if parts[0] in ("manifest.json", "models_manifest.json", "custom_nodes_manifest.json", "settings.json") and len(parts) != 1:
            raise ValueError("Invalid metadata path")
        if parts[0] == "workflows" and not item.is_dir() and (len(parts) < 2 or not name.lower().endswith(".json")):
            raise ValueError("Workflow archive entry must be JSON")
        if parts[0] == "outputs" and Path(name).suffix.lower() in MODEL_EXTENSIONS:
            raise ValueError("Model binaries are not allowed in a workspace backup")
        if parts[0] == "custom_nodes" and (len(parts) < 3 or Path(name).suffix.lower() in MODEL_EXTENSIONS):
            raise ValueError("Invalid custom node archive entry")
        if parts[0] in ("manifest.json", "models_manifest.json", "custom_nodes_manifest.json", "settings.json") and item.file_size > 10 * 1024 * 1024:
            raise ValueError("Backup metadata is too large")
        if item.file_size > max(item.compress_size, 1) * 1000 and item.file_size > 100 * 1024 * 1024:
            raise ValueError("Archive entry expands unexpectedly")
        total += item.file_size
    return total


def validate_archive(path):
    with zipfile.ZipFile(path) as archive:
        total = _safe_entries(archive)
        if "manifest.json" not in archive.namelist():
            raise ValueError("Missing backup manifest")
        if archive.getinfo("manifest.json").file_size > 1024 * 1024:
            raise ValueError("Backup manifest is too large")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("backup_format") != FORMAT or manifest.get("schema_version") not in (1, SCHEMA):
            raise ValueError("Unsupported backup format or schema")
        packages = manifest.get("bundled_custom_nodes", [])
        if (not isinstance(packages, list) or len(packages) > 100 or any(not isinstance(name, str) or
                name.startswith(".") or len(name) > 200 or len(relative_path(name).parts) != 1 for name in packages) or
                len(packages) != len({name.casefold() for name in packages})):
            raise ValueError("Invalid bundled custom nodes")
        archived_packages = {PurePosixPath(item.filename).parts[1] for item in archive.infolist()
                             if item.filename.startswith("custom_nodes/")}
        if (archived_packages - set(packages) or
                (manifest["schema_version"] == 1 and archived_packages)):
            raise ValueError("Unexpected custom node files")
        workflow_destinations = manifest.get("workflow_destinations", [])
        if not isinstance(workflow_destinations, list):
            raise ValueError("Invalid workflow destinations")
        seen_destinations = set()
        for destination in workflow_destinations:
            if not isinstance(destination, dict) or destination.get("root") not in ("user", "comfy") or not isinstance(destination.get("path"), str):
                raise ValueError("Invalid workflow destination")
            archive_name = destination.get("archive")
            parts = PurePosixPath(destination["path"]).parts
            if (not isinstance(archive_name, str) or archive_name not in archive.namelist() or not archive_name.startswith("workflows/") or
                    not destination["path"].lower().endswith(".json") or
                    (destination["root"] == "user" and not ((len(parts) >= 2 and parts[0] == "workflows") or
                                                              (len(parts) >= 3 and parts[1] == "workflows"))) or
                    (destination["root"] == "comfy" and (len(parts) < 2 or parts[0] != "workflows")) or
                    archive_name in seen_destinations):
                raise ValueError("Invalid workflow destination")
            resolve(destination["root"], destination["path"])
            seen_destinations.add(archive_name)
        models = json.loads(archive.read("models_manifest.json")) if "models_manifest.json" in archive.namelist() else []
        custom = json.loads(archive.read("custom_nodes_manifest.json")) if "custom_nodes_manifest.json" in archive.namelist() else []
        if not isinstance(models, list) or not isinstance(custom, list):
            raise ValueError("Invalid dependency manifests")
        for model in models:
            if not isinstance(model, dict) or not isinstance(model.get("name"), str) or len(model["name"]) > 500:
                raise ValueError("Invalid model manifest entry")
            if model["name"] in ("", ".", "..") or "/" in model["name"] or "\\" in model["name"]:
                raise ValueError("Invalid model filename")
            relative_path(model["name"])
            for field in ("relative_path", "expected_folder"):
                if model.get(field) is not None:
                    if not isinstance(model[field], str):
                        raise ValueError("Invalid model path")
                    relative_path(model[field])
            if model.get("sha256") is not None and (not isinstance(model["sha256"], str) or not re.fullmatch(r"[0-9a-fA-F]{64}", model["sha256"])):
                raise ValueError("Invalid model SHA256")
            sources = model.get("sources", {})
            if not isinstance(sources, dict):
                raise ValueError("Invalid model source entry")
            for url in sources.values():
                public_source_url(url)
        for package in custom:
            if not isinstance(package, dict) or not isinstance(package.get("package"), str) or len(package["package"]) > 200:
                raise ValueError("Invalid custom node entry")
            if not isinstance(package.get("nodes", []), list) or len(package.get("nodes", [])) > 1000 or any(
                    not isinstance(name, str) or len(name) > 200 for name in package.get("nodes", [])):
                raise ValueError("Invalid custom node names")
            if package.get("repository"):
                public_source_url(package["repository"])
        if set(packages) - {package["package"] for package in custom}:
            raise ValueError("Bundled custom node is missing from the manifest")
        installed = scan_models()
        names = {item["name"].lower(): item for item in installed}
        hashes = {item["sha256"]: item for item in installed if item["sha256"]}
        model_status = []
        for model in models:
            matched = hashes.get(model.get("sha256")) if model.get("sha256") else None
            if matched:
                status = "Hash Match"
            else:
                matched = names.get(model["name"].lower())
                status = "Installed" if matched else "Missing"
                if matched and model.get("sha256") and matched.get("sha256") and matched["sha256"] != model["sha256"]:
                    status = "Hash Mismatch"
                elif matched and model.get("expected_folder"):
                    source = roots()[matched["root"]] / matched["path"]
                    base = Path(folder_paths.base_path).resolve()
                    current_folder = source.relative_to(base).parent.as_posix() if inside(source, base) else None
                    if current_folder != model["expected_folder"].replace("\\", "/"):
                        status = "Found Elsewhere"
            model_status.append({**model, "status": status,
                                 "current_location": {"root": matched["root"], "path": matched["path"]} if matched else None})
        import nodes
        node_status = []
        custom_root = Path(folder_paths.base_path).resolve() / "custom_nodes"
        for package in custom:
            if isinstance(package, dict):
                missing = [name for name in package.get("nodes", []) if name not in nodes.NODE_CLASS_MAPPINGS]
                name = package["package"]
                try:
                    present = len(relative_path(name).parts) == 1 and (custom_root / name).is_dir() and not (custom_root / name).is_symlink()
                except ValueError:
                    present = False
                status = "Installed" if present and not missing else "Files present" if present else "Missing"
                node_status.append({**package, "status": status, "missing_nodes": missing,
                                    "bundled": name in packages, "files_present": present,
                                    "requirements_present": present and (custom_root / name / "requirements.txt").is_file() and
                                    not (custom_root / name / "requirements.txt").is_symlink()})
        return {"manifest": manifest, "models": model_status, "custom_nodes": node_status,
                "workflows": len([name for name in archive.namelist() if name.startswith("workflows/") and not name.endswith("/")]),
                "outputs": len([name for name in archive.namelist() if name.startswith("outputs/") and not name.endswith("/")]),
                "uncompressed_size": total, "free_space": shutil.disk_usage(Path(folder_paths.get_user_directory())).free}


def restore_backup(import_id, *, workflows=True, outputs=False, overwrite=False, comfy_settings=False, plugin_settings=False,
                   custom_nodes=None, install_requirements=False):
    path = import_archive(import_id)
    summary = validate_archive(path)
    selected_nodes = [] if custom_nodes is None else custom_nodes
    bundled = set(summary["manifest"].get("bundled_custom_nodes", []))
    if (not isinstance(selected_nodes, list) or any(not isinstance(name, str) for name in selected_nodes) or
            len(selected_nodes) != len({name.casefold() for name in selected_nodes}) or not set(selected_nodes) <= bundled):
        raise ValueError("Select custom nodes bundled in this backup")
    missing_nodes = [name for name in selected_nodes if not resolve("custom_nodes", name).exists()]
    with zipfile.ZipFile(path) as archive:
        workflow_size = sum(item.file_size for item in archive.infolist() if workflows and item.filename.startswith("workflows/"))
        output_size = sum(item.file_size for item in archive.infolist() if outputs and item.filename.startswith("outputs/"))
        node_size = sum(item.file_size for item in archive.infolist() if item.filename.startswith("custom_nodes/") and
                        PurePosixPath(item.filename).parts[1] in missing_nodes)
    user_folder = Path(folder_paths.get_user_directory())
    output_folder = Path(folder_paths.get_output_directory())
    custom_folder = Path(folder_paths.base_path) / "custom_nodes"
    required = {}
    for folder, size in ((user_folder, workflow_size), (output_folder, output_size), (custom_folder, node_size)):
        device = folder.stat().st_dev
        used, free = required.get(device, (0, shutil.disk_usage(folder).free))
        required[device] = (used + size, free)
    if any(used > free for used, free in required.values()):
        raise OSError("Not enough free space to restore the selected content")
    def run(task):
        restored = 0
        skipped = 0
        with zipfile.ZipFile(path) as archive:
            _safe_entries(archive)
            installed_requirements = install_package_requirements(archive, missing_nodes, task) if install_requirements else 0
            manifest = json.loads(archive.read("manifest.json"))
            destinations = {item["archive"]: item for item in manifest.get("workflow_destinations", [])}
            entries = [item for item in archive.infolist() if not item.is_dir() and
                       ((workflows and item.filename.startswith("workflows/")) or
                        (outputs and item.filename.startswith("outputs/")))]
            node_entries = {name: [item for item in archive.infolist() if not item.is_dir() and
                                   item.filename.startswith("custom_nodes/" + name + "/")]
                            for name in selected_nodes}
            total_entries = len(entries) + sum(len(items) for items in node_entries.values())
            for index, item in enumerate(entries, 1):
                task.check()
                relative = item.filename.split("/", 1)[1]
                if item.filename.startswith("workflows/"):
                    destination = destinations.get(item.filename)
                    target = resolve(destination["root"], destination["path"]) if destination else resolve("user", "default/workflows/" + relative)
                else:
                    target = resolve("output", relative)
                if target.suffix.lower() in MODEL_EXTENSIONS:
                    raise ValueError("Model binaries cannot be restored from a workspace backup")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and not overwrite:
                    skipped += 1
                    continue
                temporary = target.with_name(target.name + ".robot-restore-" + uuid.uuid4().hex)
                try:
                    with archive.open(item) as source, temporary.open("xb") as destination:
                        while True:
                            task.check()
                            chunk = source.read(8 * 1024 * 1024)
                            if not chunk:
                                break
                            destination.write(chunk)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                restored += 1
                task.progress(index, total_entries)
            completed = len(entries)
            for name, files in node_entries.items():
                task.check()
                target = resolve("custom_nodes", name)
                if target.exists():
                    skipped += 1
                    completed += len(files)
                    task.progress(completed, total_entries)
                    continue
                temporary = custom_folder / (".robot-restore-" + uuid.uuid4().hex)
                temporary.mkdir()
                try:
                    for item in files:
                        task.check()
                        relative = item.filename.split("/", 2)[2]
                        destination = temporary / relative_path(relative)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(item) as source, destination.open("xb") as output:
                            while chunk := source.read(8 * 1024 * 1024):
                                task.check()
                                output.write(chunk)
                        completed += 1
                        task.progress(completed, total_entries)
                    if target.exists():
                        raise FileExistsError(name)
                    temporary.rename(target)
                    restored += 1
                finally:
                    if temporary.exists():
                        shutil.rmtree(temporary)
            if (comfy_settings or plugin_settings) and "settings.json" in archive.namelist():
                data = json.loads(archive.read("settings.json"))
                if not isinstance(data, dict):
                    raise ValueError("Invalid backup settings")
                if comfy_settings:
                    safe = safe_comfy_settings(data.get("comfy"))
                    target = resolve("user", "default/comfy.settings.json")
                    if safe:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        current = read_json(target, {})
                        if not isinstance(current, dict):
                            raise ValueError("Existing ComfyUI settings are invalid")
                        current.update(safe)
                        write_json(target, current)
                        restored += 1
                if plugin_settings and isinstance(data.get("plugin"), dict):
                    plugin = data["plugin"]
                    current = settings()
                    if isinstance(plugin.get("trash"), bool):
                        current["trash"] = plugin["trash"]
                    ttl = plugin.get("export_ttl_hours")
                    if type(ttl) is int and 1 <= ttl <= 168:
                        current["export_ttl_hours"] = ttl
                    write_json(SETTINGS, current, private=True)
                    restored += 1
        return {"restored": restored, "skipped": skipped, "requirements_installed": installed_requirements,
                "missing_models": sum(item["status"] == "Missing" for item in summary["models"]),
                "missing_custom_nodes": sum(item["status"] == "Missing" for item in summary["custom_nodes"])}
    return TASKS.add("restore", "Restore workspace", run)


def import_archive(identifier):
    if not isinstance(identifier, str) or len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
        raise ValueError("Invalid import ID")
    path = IMPORTS / (identifier + ".zip")
    if not path.is_file():
        raise FileNotFoundError(identifier)
    return path


def safe_comfy_settings(value):
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if key in COMFY_SETTINGS_ALLOWLIST and
            isinstance(item, (str, int, float, bool)) and len(str(item)) <= 200}


def cleanup_exports():
    cutoff = time.time() - settings()["export_ttl_hours"] * 3600
    for folder in (EXPORTS, IMPORTS):
        for path in folder.glob("*.zip"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue


def schedule_cleanup():
    cleanup_exports()
    timer = threading.Timer(3600, schedule_cleanup)
    timer.daemon = True
    timer.start()
