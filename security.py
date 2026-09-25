import csv
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import folder_paths


DATA = Path(folder_paths.get_user_directory()).resolve() / ".robot_file_manager"
DATA.mkdir(parents=True, exist_ok=True)
if os.name != "nt":
    os.chmod(DATA, 0o700)
PRIVATE = DATA.resolve()
SETTINGS = DATA / "settings.json"
ROOTS_FILE = DATA / "allowed_roots.json"


def read_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def write_json(path, value, private=False):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=path.name + ".tmp-", delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
    try:
        if private and os.name != "nt":
            os.chmod(temporary, 0o600)
        if private and os.name == "nt":
            icacls = shutil.which("icacls")
            if not icacls:
                raise OSError("Windows file permissions are unavailable")
            identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                      capture_output=True, text=True, check=True)
            sid = next(csv.reader(identity.stdout.splitlines()))[1]
            result = subprocess.run([icacls, str(temporary), "/inheritance:r", "/grant:r", f"*{sid}:F"],
                                    capture_output=True, text=True, check=False)
            if result.returncode:
                raise OSError("Could not protect private configuration file")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def settings():
    saved = read_json(SETTINGS, {})
    if not isinstance(saved, dict):
        saved = {}
    return {"trash": bool(saved.get("trash", True)), "export_ttl_hours": min(168, max(1, int(saved.get("export_ttl_hours", 24))))}


def roots():
    base = Path(folder_paths.base_path).resolve()
    result = {
        "comfy": base,
        "models": Path(folder_paths.models_dir).resolve(),
        "output": Path(folder_paths.get_output_directory()).resolve(),
        "input": Path(folder_paths.get_input_directory()).resolve(),
        "temp": Path(folder_paths.get_temp_directory()).resolve(),
        "user": Path(folder_paths.get_user_directory()).resolve(),
        "custom_nodes": base / "custom_nodes",
    }
    for category, (paths, _) in folder_paths.folder_names_and_paths.items():
        if category == "custom_nodes":
            continue
        for index, path in enumerate(paths):
            result[f"model:{category}:{index}"] = Path(path).resolve()
    configured = read_json(ROOTS_FILE, {})
    if not isinstance(configured, dict):
        configured = {}
    for name, path in configured.items():
        if isinstance(name, str) and isinstance(path, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name) and name not in result:
            resolved = Path(path).expanduser().resolve()
            if resolved.is_dir():
                result[f"extra:{name}"] = resolved
    return result


def relative_path(value):
    if not isinstance(value, str) or "\x00" in value or value.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", value):
        raise ValueError("Invalid relative path")
    parts = PurePosixPath(value.replace("\\", "/")).parts
    if any(part in ("..", "") or ":" in part or part.endswith((".", " ")) or
           re.match(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part, re.I) for part in parts):
        raise ValueError("Path traversal is not allowed")
    return Path(*parts)


def inside(path, root):
    return path == root or root in path.parents


def resolve(root_name, value="", *, must_exist=False):
    available = roots()
    if root_name not in available:
        raise ValueError("Unknown root")
    root = available[root_name].resolve()
    relative = relative_path(value)
    candidate = root
    for part in relative.parts:
        candidate /= part
        if candidate.is_symlink():
            raise ValueError("Symbolic links are not available through the file manager")
    target = candidate.resolve()
    if not inside(target, root) or inside(target, PRIVATE):
        raise ValueError("Path is outside the allowed root")
    if must_exist and not target.exists():
        raise FileNotFoundError(value)
    return target


def child(root_name, folder, name):
    if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\\" in name or "\x00" in name:
        raise ValueError("Invalid filename")
    parent = resolve(root_name, folder, must_exist=True)
    if not parent.is_dir():
        raise ValueError("Destination must be a folder")
    return resolve(root_name, str((Path(folder) / name).as_posix()))


def reject_symlinks(path):
    if path.is_symlink():
        raise ValueError("Symbolic links cannot be transferred")
    if path.is_dir():
        for parent, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                if (Path(parent) / name).is_symlink():
                    raise ValueError("Folder contains a symbolic link")


def safe_url(value, allowed_hosts=None):
    if not isinstance(value, str):
        raise ValueError("Expected a URL")
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Use a public HTTPS URL without embedded credentials")
    if allowed_hosts and host not in allowed_hosts:
        raise ValueError("Unexpected source host")
    if len(value) > 16384 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid URL")
    return value


def public_addresses(host):
    addresses = set()
    for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise ValueError("Download source resolves to a private address")
        addresses.add(str(address))
    if not addresses:
        raise ValueError("Download source has no public address")
    return addresses


def public_url(value):
    value = safe_url(value)
    public_addresses(urlsplit(value).hostname)
    return value


def download_url(value):
    parsed = urlsplit(value)
    query = ""
    if (parsed.hostname or "").lower() in ("civitai.com", "www.civitai.com"):
        query = urlencode([(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
                           if key in ("type", "format", "size", "fp", "modelVersionId")])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def public_source_url(value):
    safe_url(value)
    if value != download_url(value):
        raise ValueError("Source URL has an unsafe query or fragment")
    return value
