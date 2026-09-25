import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import folder_paths

from .security import DATA, download_url, resolve, roots, safe_url
from .tasks import TASKS


DB = DATA / "models.sqlite3"
MODEL_EXTENSIONS = {".safetensors", ".ckpt", ".pt", ".pt2", ".pth", ".pkl", ".bin", ".gguf", ".onnx", ".sft"}
MODEL_PATTERN = re.compile(r"(?<!\w)([^\s\"'<>|]+\.(?:safetensors|ckpt|pt2?|pth|pkl|bin|gguf|onnx|sft))\b", re.I)
EMBEDDING_PATTERN = re.compile(r"\bembedding:([\w .-]+)", re.I)


@contextmanager
def database():
    connection = sqlite3.connect(DB, timeout=30)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS models (path TEXT PRIMARY KEY, sha256 TEXT, source_platform TEXT, source_url TEXT, model_type TEXT, model_name TEXT)")
        connection.execute("CREATE INDEX IF NOT EXISTS models_sha ON models (sha256)")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def save_model_source(path, platform, url, metadata):
    if platform not in ("huggingface", "civitai", "direct", "github", "other"):
        raise ValueError("Unknown source platform")
    safe_url(url)
    source_url = download_url(url)
    if platform == "direct" and source_url != url:
        platform, source_url = None, None
    with database() as connection:
        connection.execute("INSERT INTO models(path, source_platform, source_url, model_type, model_name, sha256) VALUES (?, ?, ?, ?, ?, ?) "
                           "ON CONFLICT(path) DO UPDATE SET source_platform=COALESCE(excluded.source_platform, models.source_platform), "
                           "source_url=COALESCE(excluded.source_url, models.source_url), "
                           "model_type=excluded.model_type, model_name=excluded.model_name, sha256=COALESCE(excluded.sha256, models.sha256)",
                           (str(path.resolve()), platform, source_url, metadata.get("model_type"), metadata.get("model_name"), metadata.get("sha256")))


def model_type(path, category=None):
    category_map = {"checkpoints": "checkpoint", "loras": "lora", "vae": "vae", "controlnet": "controlnet",
                    "diffusion_models": "diffusion_model", "unet": "unet", "clip": "clip", "text_encoders": "text_encoder",
                    "embeddings": "embedding", "upscale_models": "upscaler", "ipadapter": "ipadapter"}
    if category in category_map:
        return category_map[category]
    text = str(path).lower()
    for key, value in category_map.items():
        if key in text:
            return value
    return "other"


def scan_models():
    found = {}
    root_map = roots()
    candidates = [(name, path) for name, path in root_map.items() if name.startswith("model:")]
    registered = {path for _, path in candidates}
    if root_map["models"] not in registered:
        candidates.append(("models", root_map["models"]))
    with database() as connection:
        known = {row[0]: row[1:] for row in connection.execute("SELECT path, sha256, source_platform, source_url, model_type, model_name FROM models")}
    for root_name, root in candidates:
        if not root.is_dir():
            continue
        category = root_name.split(":")[1] if root_name.startswith("model:") else None
        for parent, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink() and
                       (root_name != "models" or (Path(parent) / name) not in registered)]
            for filename in files:
                path = Path(parent) / filename
                if path.is_symlink() or path.suffix.lower() not in MODEL_EXTENSIONS:
                    continue
                key = str(path.resolve())
                if key in found:
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                sha, platform, url, stored_type, model_name = known.get(key, (None, None, None, None, None))
                found[key] = {"name": filename, "root": root_name, "path": path.relative_to(root).as_posix(),
                              "type": stored_type or model_type(path, category), "size": stat.st_size, "modified": stat.st_mtime,
                              "sha256": sha, "source_platform": platform, "source_url": url, "model_name": model_name}
    return sorted(found.values(), key=lambda item: (item["type"], item["name"].lower()))


def hash_model(root_name, relative):
    path = resolve(root_name, relative, must_exist=True)
    if not path.is_file() or path.suffix.lower() not in MODEL_EXTENSIONS:
        raise ValueError("Select a model file")
    def run(task):
        digest = hashlib.sha256()
        total = path.stat().st_size
        count = 0
        with path.open("rb") as stream:
            while True:
                task.check()
                chunk = stream.read(8 * 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                count += len(chunk)
                task.progress(count, total)
        value = digest.hexdigest()
        with database() as connection:
            connection.execute("INSERT INTO models(path, sha256) VALUES (?, ?) ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256", (str(path), value))
        return {"sha256": value}
    return TASKS.add("hash", path.name, run)


def locate_model(root_name, relative):
    path = resolve(root_name, relative, must_exist=True)
    if not path.is_file() or path.suffix.lower() not in MODEL_EXTENSIONS:
        raise ValueError("Choose a model file")
    with database() as connection:
        row = connection.execute("SELECT sha256, source_platform, source_url FROM models WHERE path=?", (str(path),)).fetchone()
    return {"root": root_name, "path": relative, "name": path.name, "size": path.stat().st_size,
            "sha256": row[0] if row else None, "source_platform": row[1] if row else None, "source_url": row[2] if row else None}


def workflow_files():
    base = Path(folder_paths.base_path).resolve()
    user = Path(folder_paths.get_user_directory()).resolve()
    folders = [base / "workflows", user / "workflows"]
    if user.is_dir():
        folders += [entry / "workflows" for entry in user.iterdir() if entry.is_dir() and entry.name != ".robot_file_manager"]
    found = []
    for folder in folders:
        if not folder.is_dir():
            continue
        for parent, dirs, files in os.walk(folder, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink()]
            for name in files:
                path = Path(parent) / name
                if path.suffix.lower() == ".json" and not path.is_symlink():
                    root = "user" if user in path.parents else "comfy"
                    origin = user if root == "user" else base
                    found.append({"name": name, "root": root, "path": path.relative_to(origin).as_posix(), "size": path.stat().st_size})
    return sorted(found, key=lambda item: item["path"])


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def analyze_workflow(path, installed=None):
    import nodes
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError("Workflow JSON is too large")
    with path.open("r", encoding="utf-8") as stream:
        graph = json.load(stream)
    if not isinstance(graph, (dict, list)):
        raise ValueError("Invalid workflow JSON")
    installed = installed if installed is not None else scan_models()
    by_name = {}
    for model in installed:
        by_name.setdefault(model["name"].lower(), []).append(model)
    with database() as connection:
        saved_sources = {}
        for local_path, platform, url in connection.execute("SELECT path, source_platform, source_url FROM models WHERE source_url IS NOT NULL"):
            saved_sources.setdefault(local_path.replace("\\", "/").split("/")[-1].lower(), []).append({"platform": platform, "url": url})
    values = set()
    for string in _strings(graph):
        full = string.strip()
        if "\n" not in full and len(full) <= 500 and Path(full.replace("\\", "/")).suffix.lower() in MODEL_EXTENSIONS:
            values.add(full.replace("\\", "/").split("/")[-1])
        else:
            for match in MODEL_PATTERN.finditer(string):
                values.add(match.group(1).replace("\\", "/").split("/")[-1])
        for match in EMBEDDING_PATTERN.finditer(string):
            values.add(match.group(1).strip() + ".safetensors")
    models = []
    for filename in sorted(values, key=str.lower):
        matches = by_name.get(filename.lower(), [])
        sources = [{"platform": item["source_platform"], "url": item["source_url"]} for item in matches if item["source_url"]]
        if not matches:
            sources = saved_sources.get(filename.lower(), [])
        models.append({"name": filename, "type": matches[0]["type"] if matches else model_type(filename), "status": "installed" if matches else "missing",
                       "locations": [{"root": item["root"], "path": item["path"]} for item in matches],
                       "sources": sources})
    graph_nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
    if not graph_nodes and isinstance(graph, dict):
        graph_nodes = list(graph.values())
    types = {item.get("type") or item.get("class_type") for item in graph_nodes if isinstance(item, dict)}
    known = nodes.NODE_CLASS_MAPPINGS
    custom = []
    for node_type in sorted(t for t in types if isinstance(t, str)):
        node_class = known.get(node_type)
        if node_class is None:
            custom.append({"node": node_type, "status": "missing", "package": None})
            continue
        module = getattr(node_class, "RELATIVE_PYTHON_MODULE", "")
        package = module.split("custom_nodes.", 1)[-1].split(".", 1)[0] if module.startswith("custom_nodes.") else None
        if package:
            custom.append({"node": node_type, "status": "installed", "package": package})
    return {"models": models, "custom_nodes": custom}


def missing_models():
    installed = scan_models()
    missing = {}
    for workflow in workflow_files():
        try:
            path = resolve(workflow["root"], workflow["path"], must_exist=True)
            report = analyze_workflow(path, installed)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        for model in report["models"]:
            if model["status"] != "missing":
                continue
            key = model["name"].lower()
            entry = missing.setdefault(key, {"name": model["name"], "type": model["type"], "sources": model["sources"],
                                             "used_by_workflows": []})
            entry["used_by_workflows"].append(workflow["root"] + "/" + workflow["path"])
    return sorted(missing.values(), key=lambda item: item["name"].lower())


def custom_node_manifest(workflow_reports):
    base = Path(folder_paths.base_path) / "custom_nodes"
    used = {}
    for workflow, report in workflow_reports.items():
        for node in report["custom_nodes"]:
            package = node["package"] or node["node"]
            item = used.setdefault(package, {"package": package, "nodes": set(), "used_by_workflows": set(), "repository": None, "commit": None})
            item["nodes"].add(node["node"])
            item["used_by_workflows"].add(workflow)
    for folder in base.iterdir() if base.is_dir() else ():
        if folder.is_dir() and not folder.is_symlink() and not folder.name.startswith(".") and folder.name not in ("__pycache__", "node_modules", "venv"):
            used.setdefault(folder.name, {"package": folder.name, "nodes": set(), "used_by_workflows": set(), "repository": None, "commit": None})
    for package, item in used.items():
        item["nodes"] = sorted(item["nodes"])
        item["used_by_workflows"] = sorted(item["used_by_workflows"])
        folder = (base / package).resolve()
        if base.resolve() not in folder.parents:
            continue
        config = folder / ".git" / "config"
        head = folder / ".git" / "HEAD"
        if config.is_file():
            data = config.read_text(encoding="utf-8", errors="replace")
            match = re.search(r"\[remote \"origin\"\][^\[]*?url\s*=\s*(\S+)", data, re.S)
            if match:
                url = match.group(1)
                if url.startswith("https://"):
                    parsed = urlsplit(url)
                    if parsed.hostname and parsed.port in (None, 443):
                        item["repository"] = urlunsplit(("https", parsed.hostname, parsed.path, "", ""))
                elif url.startswith("git@github.com:"):
                    item["repository"] = "https://github.com/" + url.split(":", 1)[1]
        if head.is_file():
            content = head.read_text(encoding="utf-8", errors="replace").strip()
            if content.startswith("ref: "):
                ref = folder / ".git" / content[5:]
                content = ref.read_text(encoding="utf-8").strip() if ref.is_file() else ""
            if re.fullmatch(r"[0-9a-f]{40}", content):
                item["commit"] = content
    return sorted(used.values(), key=lambda item: item["package"].lower())


def installed_custom_nodes():
    base = Path(folder_paths.base_path) / "custom_nodes"
    if not base.is_dir():
        return []
    result = []
    for folder in sorted(base.iterdir(), key=lambda item: item.name.lower()):
        if folder.is_dir() and not folder.is_symlink() and not folder.name.startswith(".") and folder.name not in ("__pycache__", "node_modules", "venv"):
            result.append({"package": folder.name, "requirements": (folder / "requirements.txt").is_file()})
    return result
