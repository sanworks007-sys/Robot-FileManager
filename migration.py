import asyncio
from pathlib import Path

from aiohttp import ClientError

from . import backup, installer, providers
from .security import resolve, roots
from .tasks import TASKS, Stopped, error_text, queue_download


def restore(import_id, options):
    summary = backup.validate_archive(backup.import_archive(import_id))
    selected = options.get("custom_nodes", [])
    known = {item["package"]: item for item in summary["custom_nodes"]}
    if not isinstance(selected, list) or any(not isinstance(name, str) or name not in known for name in selected) or len(selected) != len(set(selected)):
        raise ValueError("Select custom nodes from the import preview")
    models = options.get("models", [])
    if not isinstance(models, list) or len(models) > len(summary["models"]):
        raise ValueError("Invalid model selection")
    seen = set()
    for model in models:
        if not isinstance(model, dict) or type(model.get("index")) is not int or not 0 <= model["index"] < len(summary["models"]) or model["index"] in seen:
            raise ValueError("Select each model once from the import preview")
        seen.add(model["index"])
        if model.get("root", "models") not in roots():
            raise ValueError("Unknown model destination root")
        resolve(model.get("root", "models"), model.get("folder", ""))
    selected_files = options.get("selected_files")
    excluded = options.get("excluded_files", [])
    if not isinstance(excluded, list) or any(not isinstance(name, str) for name in excluded) or not set(excluded) <= {item["archive"] for item in summary["files"]}:
        raise ValueError("Invalid excluded files")
    if selected_files is None and excluded:
        excluded_set = set(excluded)
        selected_files = [item["archive"] for item in summary["files"] if item["archive"] not in excluded_set]
    if selected_files is not None and (not isinstance(selected_files, list) or any(not isinstance(name, str) for name in selected_files) or
            not set(selected_files) <= {item["archive"] for item in summary["files"]}):
        raise ValueError("Select files from the import preview")
    requirements = bool(options.get("install_requirements", True))

    def run(task):
        report = {"items": [], "errors": [], "restart_required": False}
        task.result = {"stage": "Restoring selected files", "report": report}
        bundled = [name for name in selected if known[name].get("bundled")]
        result = backup.restore_backup(import_id, workflows=True, outputs=True, selected_files=selected_files,
            overwrite=bool(options.get("overwrite", False)), comfy_settings=bool(options.get("comfy_settings", True)),
            plugin_settings=bool(options.get("plugin_settings", True)), custom_nodes=bundled, task=task)
        report.update(restored=result["restored"], skipped=result["skipped"])
        report["restart_required"] = any(not known[name]["files_present"] for name in bundled)
        packages = [(name, known[name]) for name in selected]
        if options.get("install_manager", True) and not installer.manager_present() and not any(
                name.lower().replace("_", "-") == "comfyui-manager" for name, _ in packages):
            packages.insert(0, ("comfyui-manager", {"repository": installer.MANAGER_REPOSITORY, "files_present": False}))
        for name, node in packages:
            task.check()
            task.result = {"stage": "Installing " + name, "report": report}
            try:
                target = resolve("custom_nodes", name)
                if not target.exists():
                    if not node.get("repository"):
                        raise ValueError("No bundled files or GitHub repository; add this plugin manually")
                    installed = installer.install_github(node["repository"], name, node.get("commit"), requirements, task=task)
                    report["restart_required"] = True
                    if installed.get("dependency_check") is False:
                        raise ValueError("Installed, but Python dependencies conflict; see Node Installer")
                elif requirements and target.is_dir() and (target / "requirements.txt").is_file():
                    requirement_path = resolve("custom_nodes", name + "/requirements.txt", must_exist=True)
                    code, output = installer.run_pip(["install", "--no-input", "-r", str(requirement_path)], task, "Installing " + name + " requirements", target)
                    if code:
                        raise OSError("Requirements failed: " + output[-300:])
                    code, output = installer.run_pip(["check"], task, "Checking dependencies", target)
                    if code:
                        raise OSError("Python dependency conflicts: " + output[-300:])
                report["items"].append({"name": name, "kind": "plugin", "status": "ready"})
            except Stopped:
                raise
            except (ValueError, OSError, ClientError, asyncio.TimeoutError) as error:
                report["errors"].append({"name": name, "kind": "plugin", "error": error_text(error)})
            task.result = {"stage": "Plugins", "report": report}
        for selection in models:
            task.check()
            model = summary["models"][selection["index"]]
            task.result = {"stage": "Downloading " + model["name"], "report": report}
            try:
                sources = model.get("sources", {})
                source = selection.get("source") or (next(iter(sources.values())) if len(sources) == 1 else None)
                if not source:
                    raise ValueError("Choose a model source URL in the import preview")
                info = asyncio.run(providers.source_info(source))
                matches = [item for item in info["files"] if item.get("name", "").lower() == model["name"].lower()]
                verified = [item for item in info["files"] if model.get("sha256") and (item.get("sha256") or "").lower() == model["sha256"].lower()]
                file = verified[0] if len(verified) == 1 else matches[0] if len(matches) == 1 else info["files"][0] if info.get("platform") == "direct" and len(info["files"]) == 1 else None
                if not file or (model.get("sha256") and file.get("sha256") and model["sha256"].lower() != file["sha256"].lower()):
                    raise ValueError("Source needs manual version/file selection or its hash differs")
                root = selection.get("root", "models")
                expected = model.get("expected_folder", "models")
                folder = selection.get("folder", expected[7:] if expected.startswith("models/") else "")
                destination = resolve(root, folder)
                destination.mkdir(parents=True, exist_ok=True)
                target = resolve(root, (Path(folder) / model["name"]).as_posix())
                if target.exists() and model["status"] == "Hash Mismatch":
                    raise ValueError("A conflicting model exists; choose another destination or remove it first")
                platform = providers.detect(file["url"])
                downloaded = queue_download(file["url"], root, folder, model["name"], platform,
                    providers.credential(platform) if platform in ("huggingface", "civitai") else None,
                    {"sha256": model.get("sha256") or file.get("sha256"), "model_type": model.get("model_type"), "details": file.get("details", {})},
                    authorization=providers.authorization_headers(file["url"], selection.get("auth_profile")), task=task)
                if model.get("metadata"):
                    backup.restore_model_metadata(model, target)
                report["items"].append({"name": model["name"], "kind": "model", "status": "skipped" if downloaded.get("skipped") else "downloaded"})
            except Stopped:
                raise
            except (ValueError, OSError, ClientError, asyncio.TimeoutError) as error:
                report["errors"].append({"name": model["name"], "kind": "model", "error": error_text(error)})
            task.result = {"stage": "Models", "report": report}
        if report["errors"]:
            task.error = str(len(report["errors"])) + " item(s) need attention; review the import report"
        return {"stage": "Finished with issues" if report["errors"] else "Finished", "report": report,
                "restart_required": report["restart_required"], "needs_attention": bool(report["errors"])}
    return TASKS.add("migration", "Import workspace", run)
