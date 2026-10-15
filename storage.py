import ctypes
import os
import shutil
import time
from pathlib import Path

from .security import DATA, inside, reject_symlinks, roots
from .tasks import TASKS


def file_size(path):
    reject_symlinks(path)
    if path.is_file():
        return path.stat().st_size
    return sum((Path(parent) / name).stat().st_size for parent, _, names in os.walk(path) for name in names)


def recycle_bin(empty=False):
    if os.name != "nt":
        return {"supported": False, "size": 0, "items": 0}
    class Query(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint32), ("i64Size", ctypes.c_int64), ("i64NumItems", ctypes.c_int64)]
    shell = ctypes.WinDLL("shell32")
    query = Query()
    query.cbSize = ctypes.sizeof(Query)
    shell.SHQueryRecycleBinW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(Query)]
    shell.SHQueryRecycleBinW.restype = ctypes.c_long
    if shell.SHQueryRecycleBinW(None, ctypes.byref(query)) != 0:
        raise OSError("Windows could not read this user's Recycle Bin")
    before = {"supported": True, "size": query.i64Size, "items": query.i64NumItems}
    if empty and query.i64NumItems:
        shell.SHEmptyRecycleBinW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32]
        shell.SHEmptyRecycleBinW.restype = ctypes.c_long
        if shell.SHEmptyRecycleBinW(None, None, 7) != 0:
            raise OSError("Windows could not empty this user's Recycle Bin")
        return {**before, "after": recycle_bin()}
    return before


def cleanup_candidates():
    found = {}
    now = time.time()
    with TASKS.lock:
        active = list(TASKS.reserved) + [task.target for task in TASKS.tasks.values() if task.target and task.status in
                  ("waiting", "running", "paused", "pausing", "cancelling")]
    def add(group, path):
        download_target = path.with_name(path.name.removesuffix(".json").removesuffix(".robot-part"))
        if path.is_symlink() or any(inside(target, path) or target == download_target for target in active):
            return
        key = str(path.resolve())
        if key not in found:
            found[key] = {"group": group, "path": path, "size": file_size(path)}
    for name, group in (("trash", "trash"), ("exports", "exports"), ("imports", "imports"), ("node_uploads", "staging")):
        folder = DATA / name
        for path in folder.iterdir() if folder.is_dir() else ():
            if group != "staging" or now - path.stat().st_mtime > 86400:
                add(group, path)
    for path in roots()["custom_nodes"].iterdir():
        if path.name.startswith((".robot-install-", ".robot-restore-")) and now - path.stat().st_mtime > 86400:
            add("staging", path)
    available = roots()
    scanned = []
    for name, base in available.items():
        if name not in ("models", "output", "input", "temp") and not name.startswith("model:"):
            continue
        if not base.is_dir():
            continue
        if any(inside(base, other) for other in scanned):
            continue
        scanned.append(base)
        for parent, dirs, names in os.walk(base, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(parent) / name).is_symlink() and name not in (".git", "__pycache__")]
            for name in names:
                if name.endswith(".robot-part") or name.endswith(".robot-part.json") and not (Path(parent) / name.removesuffix(".json")).exists():
                    add("partials", Path(parent) / name)
    return list(found.values())


def preview_cleanup():
    groups = {}
    for item in cleanup_candidates():
        group = groups.setdefault(item["group"], {"items": 0, "size": 0})
        group["items"] += 1
        group["size"] += item["size"]
    return {"groups": groups, "recycle_bin": recycle_bin(),
            "note": "Cleanup applies to the ComfyUI server. Files still open by another process may keep disk space allocated."}


def clean(groups, windows_recycle=False):
    allowed = {"trash", "exports", "imports", "staging", "partials"}
    if not isinstance(groups, list) or any(group not in allowed for group in groups):
        raise ValueError("Choose cleanup categories from the preview")
    def run(task):
        # Do not remove an archive or staging folder while another operation is using it.
        with TASKS.lock:
            if any(item.id != task.id and item.status in ("waiting", "running", "pausing", "cancelling") for item in TASKS.tasks.values()):
                raise ValueError("Finish active file tasks and installations before cleanup")
        candidates = [item for item in cleanup_candidates() if item["group"] in groups]
        disks = {}
        for item in candidates:
            parent = item["path"].parent
            disks.setdefault(parent.stat().st_dev, (parent, shutil.disk_usage(parent).free))
        result = {"removed": 0, "removed_bytes": 0, "errors": []}
        for item in candidates:
            task.check()
            path = item["path"]
            try:
                if not any(inside(path.resolve(), root.resolve()) for root in [DATA, *roots().values()]):
                    raise ValueError("Cleanup path is outside the approved folders")
                reject_symlinks(path)
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                if path.exists():
                    raise OSError("File still exists")
                if item["group"] == "partials":
                    metadata = path.with_name(path.name + ".json")
                    if not metadata.is_symlink():
                        metadata.unlink(missing_ok=True)
                result["removed"] += 1
                result["removed_bytes"] += item["size"]
            except (OSError, ValueError) as error:
                result["errors"].append({"name": path.name, "error": str(error)[:300]})
            task.result = result
            task.progress(result["removed"], len(candidates))
        if windows_recycle:
            result["recycle_bin"] = recycle_bin(empty=True)
        if result["errors"]:
            task.error = str(len(result["errors"])) + " item(s) could not be removed; review the cleanup report"
            result["needs_attention"] = True
        result["free_space_change"] = sum(shutil.disk_usage(parent).free - before for parent, before in disks.values())
        return result
    return TASKS.add("cleanup", "Clean server storage", run)
