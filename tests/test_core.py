import asyncio
import base64
import importlib
import hashlib
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer


PLUGIN = Path(__file__).resolve().parents[1]


class CoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name) / "ComfyUI"
        for name in ("models/checkpoints", "output", "input", "temp", "user/default/workflows", "custom_nodes"):
            (cls.base / name).mkdir(parents=True, exist_ok=True)
        paths = types.ModuleType("folder_paths")
        paths.base_path = str(cls.base)
        paths.models_dir = str(cls.base / "models")
        paths.folder_names_and_paths = {"checkpoints": ([str(cls.base / "models/checkpoints")], {".safetensors"})}
        paths.get_user_directory = lambda: str(cls.base / "user")
        paths.get_input_directory = lambda: str(cls.base / "input")
        paths.get_output_directory = lambda: str(cls.base / "output")
        paths.get_temp_directory = lambda: str(cls.base / "temp")
        sys.modules["folder_paths"] = paths
        nodes = types.ModuleType("nodes")
        nodes.NODE_CLASS_MAPPINGS = {}
        sys.modules["nodes"] = nodes
        package = types.ModuleType("rfm")
        package.__path__ = [str(PLUGIN)]
        sys.modules["rfm"] = package
        cls.security = importlib.import_module("rfm.security")
        cls.tasks = importlib.import_module("rfm.tasks")
        cls.catalog = importlib.import_module("rfm.catalog")
        cls.backup = importlib.import_module("rfm.backup")
        cls.providers = importlib.import_module("rfm.providers")
        cls.transfers = importlib.import_module("rfm.transfers")
        cls.terminal = importlib.import_module("rfm.terminal")
        cls.installer = importlib.import_module("rfm.installer")
        cls.storage = importlib.import_module("rfm.storage")
        cls.migration = importlib.import_module("rfm.migration")
        server = types.ModuleType("server")
        server.PromptServer = types.SimpleNamespace(instance=types.SimpleNamespace(routes=web.RouteTableDef(),
            prompt_queue=types.SimpleNamespace(get_tasks_remaining=lambda: 0)))
        sys.modules["server"] = server
        cls.routes = importlib.import_module("rfm.routes")
        cls.identification = cls.routes.identification
        cls.updater = cls.routes.updater

    @classmethod
    def tearDownClass(cls):
        cls.tasks.TASKS.executor.shutdown(wait=True)
        cls.temp.cleanup()

    def wait_task(self, task):
        deadline = time.time() + 10
        while task.status in ("waiting", "running") and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(task.status, "completed", task.error)
        return task.result

    def test_paths_reject_traversal_and_private_data(self):
        for value in ("../outside", "a/../../outside", "C:\\Windows", "/etc/passwd", "\\\\server\\share",
                      "data:stream", "NUL.txt", "folder/CON", "trailing."):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.security.resolve("output", value)
        with self.assertRaises(ValueError):
            self.security.resolve("user", ".robot_file_manager/credentials.json")
        link = self.base / "output" / "link"
        try:
            link.symlink_to(self.base / "user", target_is_directory=True)
        except (OSError, NotImplementedError):
            return
        with self.assertRaises(ValueError):
            self.security.resolve("output", "link/default")

    def test_owner_approved_root_supports_file_operations(self):
        approved = Path(self.temp.name) / "approved"
        approved.mkdir(exist_ok=True)
        (approved / "model.safetensors").write_bytes(b"model")
        self.security.write_json(self.security.ROOTS_FILE, {"linux": str(approved)}, private=True)
        try:
            self.assertEqual(self.security.roots()["extra:linux"], approved)
            self.wait_task(self.tasks.file_operation("move", [{"root": "extra:linux", "path": "model.safetensors"}],
                                                     {"root": "models", "path": "checkpoints"}))
            self.assertTrue((self.base / "models/checkpoints/model.safetensors").is_file())
        finally:
            self.security.ROOTS_FILE.unlink(missing_ok=True)

    def test_url_validation_and_provider_parsing(self):
        for value in ("http://example.com/file", "https://user:pass@example.com/file", "https://127.0.0.1/file"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.security.public_url(value)
        self.assertEqual(self.providers.detect("owner/model"), "huggingface")
        self.assertEqual(self.providers.huggingface_reference("https://huggingface.co/owner/model/blob/main/a.safetensors"),
                         ("owner/model", "main", "a.safetensors"))
        self.assertEqual(self.providers.civitai_reference("https://civitai.com/models/12?modelVersionId=34"), ("version", "34"))

    def test_civitai_file_selection_preserves_filters_without_token(self):
        digest = "a" * 64
        async def response(_url, _platform):
            return {"name": "Example", "type": "LORA", "modelVersions": [{"id": 12, "name": "v2", "files": [
                {"name": "example.safetensors", "sizeKB": 10, "hashes": {"SHA256": digest},
                 "downloadUrl": "https://civitai.com/api/download/models/12?type=Model&format=SafeTensor&token=secret"}]}]}
        with patch.object(self.providers, "request_json", response):
            info = asyncio.run(self.providers.civitai_files("12"))
        self.assertEqual(info["files"][0]["sha256"], digest)
        self.assertIn("format=SafeTensor", info["files"][0]["url"])
        self.assertNotIn("secret", info["files"][0]["url"])

    def test_credentials_stay_separate_from_backup(self):
        self.providers.set_credential("huggingface", "sample-secret")
        self.assertEqual(self.providers.credential("huggingface"), "sample-secret")
        self.assertTrue(self.providers.CREDENTIALS.is_file())
        self.providers.set_credential("huggingface", "")
        self.assertIsNone(self.providers.credential("huggingface"))

    def test_signed_direct_url_is_not_saved_as_model_source(self):
        model = self.base / "models/checkpoints/signed.safetensors"
        model.write_bytes(b"model")
        self.catalog.save_model_source(model, "direct", "https://example.com/signed.safetensors?opaque=secret-value", {})
        with self.catalog.database() as connection:
            platform, url = connection.execute("SELECT source_platform, source_url FROM models WHERE path=?", (str(model),)).fetchone()
        self.assertIsNone(platform)
        self.assertIsNone(url)

    def test_model_library_finds_unregistered_model_folder(self):
        folder = self.base / "models/ipadapter"
        folder.mkdir(exist_ok=True)
        model = folder / "adapter.safetensors"
        model.write_bytes(b"model")
        found = [item for item in self.catalog.scan_models() if item["name"] == model.name]
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["root"], found[0]["path"], found[0]["type"]),
                         ("models", "ipadapter/adapter.safetensors", "ipadapter"))

    def test_bulk_zip_excludes_private_data(self):
        self.providers.set_credential("civitai", "zip-secret")
        result = self.wait_task(self.backup.create_selection_zip("comfy", ["user"]))
        with zipfile.ZipFile(self.backup.EXPORTS / (result["export_id"] + ".zip")) as archive:
            self.assertFalse(any(".robot_file_manager" in name for name in archive.namelist()))
        self.providers.set_credential("civitai", "")

    def test_selected_custom_node_files_restore_without_overwriting_existing_folder(self):
        folder = self.base / "custom_nodes/bundled-node"
        folder.mkdir()
        (folder / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}", encoding="utf-8")
        (folder / "requirements.txt").write_text("example-package>=1\n", encoding="utf-8")
        (folder / ".env").write_text("SECRET=value", encoding="utf-8")
        (folder / "large-model.safetensors").write_bytes(b"binary")
        result = self.wait_task(self.backup.create_backup({"workflows": False, "models": False, "outputs": "skip",
                                                           "bundled_custom_nodes": ["bundled-node"]}))
        exported = self.backup.EXPORTS / (result["export_id"] + ".zip")
        with zipfile.ZipFile(exported) as archive:
            names = archive.namelist()
            self.assertIn("custom_nodes/bundled-node/__init__.py", names)
            self.assertIn("custom_nodes/bundled-node/requirements.txt", names)
            self.assertNotIn("custom_nodes/bundled-node/.env", names)
            self.assertNotIn("custom_nodes/bundled-node/large-model.safetensors", names)
            self.assertEqual(json.loads(archive.read("manifest.json"))["schema_version"], self.backup.SCHEMA)
        imported = self.backup.IMPORTS / (uuid.uuid4().hex + ".zip")
        shutil.copyfile(exported, imported)
        shutil.rmtree(folder)
        summary = self.backup.validate_archive(imported)
        package = next(item for item in summary["custom_nodes"] if item["package"] == "bundled-node")
        self.assertEqual((package["status"], package["bundled"]), ("Missing", True))
        def install_first(_archive, _names, _task):
            self.assertFalse(folder.exists())
            return 1
        with patch.object(self.backup, "install_package_requirements", side_effect=install_first):
            first = self.wait_task(self.backup.restore_backup(imported.stem, workflows=False, custom_nodes=["bundled-node"],
                                                              install_requirements=True))
        self.assertEqual(first["requirements_installed"], 1)
        self.assertTrue((folder / "__init__.py").is_file())
        (folder / "__init__.py").write_text("keep local changes", encoding="utf-8")
        second = self.wait_task(self.backup.restore_backup(imported.stem, workflows=False, custom_nodes=["bundled-node"]))
        self.assertEqual(second["skipped"], 1)
        self.assertEqual((folder / "__init__.py").read_text(), "keep local changes")

    def test_custom_node_requirements_reject_pip_options(self):
        path = self.backup.IMPORTS / (uuid.uuid4().hex + ".zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("custom_nodes/package/requirements.txt", "--extra-index-url https://example.com\n")
        with zipfile.ZipFile(path) as archive:
            with self.assertRaisesRegex(ValueError, "unsupported package source"):
                self.backup.install_package_requirements(archive, ["package"], types.SimpleNamespace(check=lambda: None))

    def test_backup_rejects_unlisted_custom_node_files(self):
        for schema, packages, entry in ((1, [], "custom_nodes/package/__init__.py"),
                                        (2, ["package"], "custom_nodes/other/__init__.py"),
                                        (2, ["package"], "custom_nodes/package/model.safetensors")):
            path = self.backup.IMPORTS / (uuid.uuid4().hex + ".zip")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("manifest.json", json.dumps({"backup_format": self.backup.FORMAT,
                                                              "schema_version": schema, "bundled_custom_nodes": packages}))
                archive.writestr(entry, "data")
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self.backup.validate_archive(path)

    def test_custom_node_requirements_use_comfy_python_without_shell(self):
        path = self.backup.IMPORTS / (uuid.uuid4().hex + ".zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("custom_nodes/package/requirements.txt", "example-package>=1\n")
        process = types.SimpleNamespace(poll=lambda: 0, returncode=0)
        with zipfile.ZipFile(path) as archive, patch.object(self.backup.subprocess, "Popen", return_value=process) as launch:
            count = self.backup.install_package_requirements(archive, ["package"], types.SimpleNamespace(check=lambda: None))
        self.assertEqual(count, 1)
        self.assertEqual(launch.call_args.args[0][-1], "example-package>=1")
        self.assertEqual(launch.call_args.args[0][:3], [sys.executable, "-m", "pip"])
        self.assertNotIn("shell", launch.call_args.kwargs)

    def test_existing_custom_node_can_install_requirements_explicitly(self):
        folder = self.base / "custom_nodes/existing-node"
        folder.mkdir(exist_ok=True)
        (folder / "requirements.txt").write_text("example-package>=1\n", encoding="utf-8")
        with patch.object(self.backup, "install_specs", return_value=1) as install:
            result = self.wait_task(self.backup.install_local_requirements("existing-node"))
        self.assertEqual(result["requirements_installed"], 1)
        self.assertEqual(install.call_args.args[0], ["example-package>=1"])

    def test_backup_preserves_same_named_workflows_from_two_users(self):
        first = self.base / "user/default/workflows/same.json"
        second = self.base / "user/secondary/workflows/same.json"
        second.parent.mkdir(parents=True, exist_ok=True)
        first.write_text('{"nodes": []}', encoding="utf-8")
        second.write_text('{"nodes": [{"type": "Other"}]}', encoding="utf-8")
        result = self.wait_task(self.backup.create_backup({"workflows": True, "models": False, "outputs": "skip"}))
        archive_path = self.backup.EXPORTS / (result["export_id"] + ".zip")
        with zipfile.ZipFile(archive_path) as archive:
            self.assertIn("workflows/user/default/same.json", archive.namelist())
            self.assertIn("workflows/user/secondary/same.json", archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(len([item for item in manifest["workflow_destinations"] if item["archive"].endswith("same.json")]), 2)

    def test_workflow_keeps_model_filename_with_spaces(self):
        path = self.base / "user/default/workflows/spaces.json"
        path.write_text(json.dumps({"nodes": [{"type": "Loader", "widgets_values": ["my model.safetensors"]}]}), encoding="utf-8")
        names = [item["name"] for item in self.catalog.analyze_workflow(path, [])["models"]]
        self.assertEqual(names, ["my model.safetensors"])

    def test_restore_reports_model_in_different_folder(self):
        model = self.base / "models/checkpoints/elsewhere.safetensors"
        model.write_bytes(b"model")
        located = self.catalog.locate_model("model:checkpoints:0", "elsewhere.safetensors")
        self.assertEqual(located["name"], model.name)
        path = self.backup.IMPORTS / (uuid.uuid4().hex + ".zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"backup_format": self.backup.FORMAT, "schema_version": 1}))
            archive.writestr("models_manifest.json", json.dumps([{"name": model.name, "expected_folder": "models/loras"}]))
        summary = self.backup.validate_archive(path)
        self.assertEqual(summary["models"][0]["status"], "Found Elsewhere")

    def test_task_history_redacts_url_query(self):
        def fail(_task):
            raise ValueError("Failed https://example.com/file?opaque=secret-value")
        task = self.tasks.TASKS.add("test", "Redaction", fail)
        deadline = time.time() + 5
        while task.status in ("waiting", "running") and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(task.status, "failed")
        self.assertNotIn("secret-value", task.error)
        self.assertNotIn("secret-value", self.tasks.TASKS.history_file.read_text(encoding="utf-8"))

    def test_restore_merges_supported_settings(self):
        comfy = self.base / "user/default/comfy.settings.json"
        comfy.write_text(json.dumps({"Other.Setting": "keep", "Comfy.Minimap.Visible": False}), encoding="utf-8")
        path = self.backup.IMPORTS / (uuid.uuid4().hex + ".zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"backup_format": self.backup.FORMAT, "schema_version": 1}))
            archive.writestr("settings.json", json.dumps({"plugin": {"trash": False, "export_ttl_hours": 12},
                                                           "comfy": {"Comfy.Minimap.Visible": True,
                                                                     "Secret.Token": "exclude"}}))
        self.wait_task(self.backup.restore_backup(path.stem, workflows=False, comfy_settings=True, plugin_settings=True))
        restored = json.loads(comfy.read_text(encoding="utf-8"))
        self.assertEqual(restored, {"Other.Setting": "keep", "Comfy.Minimap.Visible": True})
        self.assertEqual(self.security.settings(), {"trash": False, "export_ttl_hours": 12, "auto_install_manager": True, "manager_setup_attempted": False, "check_updates": True, "auto_update": False})
        self.security.write_json(self.security.SETTINGS, {"trash": True, "export_ttl_hours": 24}, private=True)

    def test_workflow_restore_ignores_unselected_output_size(self):
        path = self.backup.IMPORTS / (uuid.uuid4().hex + ".zip")
        destination = {"archive": "workflows/user/default/restore-only.json", "root": "user",
                       "path": "default/workflows/restore-only.json"}
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"backup_format": self.backup.FORMAT, "schema_version": 1,
                                                           "workflow_destinations": [destination]}))
            archive.writestr(destination["archive"], "{}")
            archive.writestr("outputs/large.txt", "x" * 100)
        with patch.object(self.backup.shutil, "disk_usage", return_value=types.SimpleNamespace(free=3)):
            self.wait_task(self.backup.restore_backup(path.stem, workflows=True, outputs=False))
        self.assertEqual((self.base / "user/default/workflows/restore-only.json").read_text(), "{}")

    def test_custom_node_manifest_strips_remote_credentials(self):
        folder = self.base / "custom_nodes/example/.git"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "config").write_text('[remote "origin"]\n    url = https://user:secret@github.com/owner/repo.git?token=secret\n', encoding="utf-8")
        report = {"default/workflow.json": {"custom_nodes": [{"node": "ExampleNode", "package": "example"}]}}
        entry = self.catalog.custom_node_manifest(report)[0]
        self.assertEqual(entry["repository"], "https://github.com/owner/repo.git")
        self.assertNotIn("secret", json.dumps(entry))

    def test_unknown_custom_node_does_not_break_manifest(self):
        report = {"default/workflow.json": {"custom_nodes": [{"node": "../Unknown", "package": None}]}}
        entry = self.catalog.custom_node_manifest(report)[0]
        self.assertEqual(entry["nodes"], ["../Unknown"])
        json.dumps(entry)

    def test_copy_trash_and_restore(self):
        source = self.base / "output" / "example.txt"
        source.write_text("example", encoding="utf-8")
        self.wait_task(self.tasks.file_operation("copy", [{"root": "output", "path": "example.txt"}],
                                                 {"root": "input", "path": ""}))
        self.assertEqual((self.base / "input" / "example.txt").read_text(), "example")
        self.wait_task(self.tasks.file_operation("delete", [{"root": "output", "path": "example.txt"}]))
        self.assertFalse(source.exists())
        item = next(item for item in self.tasks.trash_list() if item["path"] == "example.txt")
        self.tasks.trash_action(item["id"], "restore")
        self.assertEqual(source.read_text(), "example")

    def test_existing_download_is_skipped_without_network(self):
        target = self.base / "models" / "existing.safetensors"
        target.write_bytes(b"keep existing model")
        with patch.object(self.tasks, "_download") as download:
            task = self.tasks.queue_download("https://example.com/existing.safetensors", "models", "", target.name)
        self.assertEqual(task.status, "skipped")
        download.assert_not_called()
        self.assertEqual(target.read_bytes(), b"keep existing model")
        self.assertEqual(task.result["size"], target.stat().st_size)

    def test_pending_download_is_not_queued_twice_across_root_aliases(self):
        entered = threading.Event()
        finish = threading.Event()
        calls = []
        async def download(task, url, target, token):
            calls.append(url)
            entered.set()
            finish.wait(5)
            target.write_bytes(b"downloaded")
            task.progress(10, 10)
        with patch.object(self.tasks, "_download", download):
            first = self.tasks.queue_download("https://example.com/pending.safetensors", "models", "checkpoints", "pending.safetensors")
            try:
                self.assertTrue(entered.wait(5))
                second = self.tasks.queue_download("https://example.com/pending.safetensors", "model:checkpoints:0", "", "pending.safetensors")
                self.assertEqual(second.status, "skipped")
            finally:
                finish.set()
            self.wait_task(first)
        self.assertEqual(len(calls), 1)

    def test_transfer_history_retains_reusable_links_after_task_removal(self):
        async def download(task, url, target, token):
            target.write_bytes(b"history file")
            task.progress(12, 12)
        with patch.object(self.tasks, "_download", download):
            task = self.tasks.queue_download("https://example.com/history.safetensors", "models", "", "history.safetensors")
            self.wait_task(task)
        self.tasks.TASKS.control(task.id, "remove")
        item = next(item for item in self.transfers.history() if item["name"] == "history.safetensors")
        self.assertEqual((item["status"], item["size"], item["root"], item["path"]), ("completed", 12, "models", "history.safetensors"))
        self.assertEqual(item["url"], "https://example.com/history.safetensors")
        self.assertTrue(item["reusable"])
        self.assertEqual(self.transfers.history(), importlib.reload(self.transfers).history())

    def test_transfer_history_redacts_signed_links(self):
        self.transfers.record("download", "signed.bin", "models", "signed.bin", "https://example.com/signed.bin?token=private-value", "direct")
        item = next(item for item in self.transfers.history() if item["name"] == "signed.bin")
        self.assertEqual(item["url"], "https://example.com/signed.bin")
        self.assertFalse(item["reusable"])
        self.assertNotIn("private-value", json.dumps(item))

    def test_delete_choice_overrides_old_default(self):
        self.security.write_json(self.security.SETTINGS, {"trash": False}, private=True)
        source = self.base / "models" / "trash-choice.safetensors"
        direct = self.base / "models" / "delete-choice.safetensors"
        source.write_bytes(b"trash")
        direct.write_bytes(b"delete")
        try:
            self.wait_task(self.tasks.file_operation("delete", [{"root": "models", "path": source.name}], permanent=False))
            self.wait_task(self.tasks.file_operation("delete", [{"root": "models", "path": direct.name}], permanent=True))
            self.assertFalse(source.exists())
            self.assertFalse(direct.exists())
            trashed = [item for item in self.tasks.trash_list() if item["name"] in (source.name, direct.name)]
            self.assertEqual([item["name"] for item in trashed], [source.name])
            self.tasks.trash_action(trashed[0]["id"], "restore")
            self.assertEqual(source.read_bytes(), b"trash")
        finally:
            self.security.write_json(self.security.SETTINGS, {"trash": True}, private=True)

    def test_model_totals_refresh_and_deduplicate_roots(self):
        target = self.base / "models/checkpoints/total.safetensors"
        before = self.catalog.model_totals(True)
        target.write_bytes(b"123456789")
        after = self.catalog.model_totals(True)
        self.assertEqual(after["count"], before["count"] + 1)
        self.assertEqual(after["size"], before["size"] + 9)
        target.unlink()
        self.assertEqual(self.catalog.model_totals(True), before)

    def test_resource_stats_re_read_disk_and_upload_history_has_local_link(self):
        async def run():
            application = web.Application()
            application.add_routes(self.routes.routes)
            async with TestClient(TestServer(application)) as client:
                with patch.object(self.routes.shutil, "disk_usage", side_effect=[
                        types.SimpleNamespace(total=100, used=20, free=80), types.SimpleNamespace(total=100, used=35, free=65)]):
                    first = await (await client.get("/robot/system/stats?root=models")).json()
                    second = await (await client.get("/robot/system/stats?root=models")).json()
                self.assertEqual((first["disk"]["free"], second["disk"]["free"]), (80, 65))
                self.assertIn("cpu", second)
                self.assertIn("percent", second["ram"])
                self.assertIn("size", second["models"])
                form = FormData()
                form.add_field("file", b"uploaded data", filename="history-upload.txt")
                response = await client.post("/robot/files/upload?root=input&path=", data=form)
                self.assertEqual(response.status, 200, await response.text())
                data = await (await client.get("/robot/transfers")).json()
                entry = next(item for item in data["transfers"] if item["name"] == "history-upload.txt")
                self.assertEqual((entry["kind"], entry["status"], entry["size"], entry["path"]), ("upload", "completed", 13, "history-upload.txt"))
                response = await client.get("/robot/files/download?root=input&path=history-upload.txt")
                self.assertEqual(await response.read(), b"uploaded data")
        asyncio.run(run())

    def test_zip_upload_extract_and_download_in_chosen_folder(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("nested/readme.txt", "ZIP from PC")
        destination = self.base / "input/zip-destination"
        destination.mkdir()
        async def run():
            application = web.Application()
            application.add_routes(self.routes.routes)
            async with TestClient(TestServer(application)) as client:
                form = FormData()
                form.add_field("file", archive.getvalue(), filename="from-pc.zip", content_type="application/zip")
                response = await client.post("/robot/files/upload?root=input&path=zip-destination", data=form)
                self.assertEqual(response.status, 200, await response.text())
                self.assertTrue((destination / "from-pc.zip").is_file())
                response = await client.post("/robot/files/extract", json={"root": "input", "path": "zip-destination/from-pc.zip",
                                             "destination_root": "output", "destination_folder": ""})
                result = await response.json()
                self.wait_task(self.tasks.TASKS.tasks[result["task"]])
                self.assertEqual((self.base / "output/nested/readme.txt").read_text(), "ZIP from PC")
                response = await client.get("/robot/files/download?root=input&path=zip-destination/from-pc.zip")
                self.assertEqual(await response.read(), archive.getvalue())
        asyncio.run(run())

    def test_terminal_requires_linux_and_valid_working_folder(self):
        with patch.object(self.terminal, "available", return_value=False), patch.object(self.terminal, "Command") as process:
            with self.assertRaisesRegex(ValueError, "Linux"):
                self.terminal.run("ls", "output", "")
            process.assert_not_called()
        with patch.object(self.terminal, "available", return_value=True), patch.object(self.terminal, "Command") as process:
            with self.assertRaises(ValueError):
                self.terminal.run("ls", "output", "../user")
            process.assert_not_called()

    def test_terminal_streams_output_and_uses_server_working_folder(self):
        process = MagicMock()
        process.stdout = io.BytesIO(b"hello from Linux\n")
        process.wait.return_value = 0
        launch = MagicMock(return_value=process)
        subprocess_api = types.SimpleNamespace(Popen=launch, DEVNULL=-3, PIPE=-1, STDOUT=-2)
        with patch.object(self.terminal, "available", return_value=True), patch.object(self.terminal.shutil, "which", return_value="/bin/bash"), \
                patch.object(self.terminal, "subprocess", subprocess_api):
            job = self.terminal.run("printf 'hello from Linux\\n'", "output", "")
            deadline = time.time() + 5
            while job.status == "running" and time.time() < deadline:
                time.sleep(0.01)
        self.assertEqual(job.public()["status"], "completed")
        self.assertEqual(job.public()["output"], "hello from Linux\n")
        self.assertEqual(launch.call_args.args[0], ["/bin/bash", "-c", "printf 'hello from Linux\\n'"])
        self.assertEqual(launch.call_args.kwargs["cwd"], str(self.base / "output"))
        self.assertTrue(launch.call_args.kwargs["start_new_session"])

    def test_terminal_stop_signals_process_group(self):
        process = MagicMock()
        process.pid = 12345
        process.stdout = io.BytesIO(b"")
        process.wait.return_value = -15
        subprocess_api = types.SimpleNamespace(Popen=MagicMock(return_value=process), DEVNULL=-3, PIPE=-1, STDOUT=-2)
        with patch.object(self.terminal, "subprocess", subprocess_api), \
                patch.object(self.terminal.os, "killpg", create=True) as kill, patch.object(self.terminal.threading, "Timer"):
            command = self.terminal.Command("sleep 10", "output", "", self.base / "output")
            command.stop()
            kill.assert_called_once_with(12345, self.terminal.signal.SIGTERM)
            command.read()
            self.assertEqual(command.public()["status"], "cancelled")

    def node_stage(self, files):
        identifier = uuid.uuid4().hex
        folder = self.installer.STAGING / identifier / "uploaded"
        folder.mkdir(parents=True)
        for name, contents in files.items():
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)
        return identifier

    def test_node_zip_installs_requirements_and_checks_loaded_status(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("SampleNode-main/__init__.py", "NODE_CLASS_MAPPINGS = {}")
            zipped.writestr("SampleNode-main/requirements.txt", "sample-package>=1\n")
        identifier = self.node_stage({"SampleNode.zip": archive.getvalue()})
        summary = self.installer.prepare(identifier)
        self.assertEqual((summary["name"], summary["kind"], summary["exists"]), ("SampleNode", "folder", False))
        with patch.object(self.installer, "run_pip", side_effect=[(0, "Installed sample-package"), (0, "No broken requirements")]) as pip:
            result = self.wait_task(self.installer.install(identifier, summary["name"]))
        self.assertTrue(result["installed"])
        self.assertTrue(result["dependency_check"])
        self.assertEqual(pip.call_args_list[0].args[0][:3], ["install", "--no-input", "-r"])
        self.assertEqual(pip.call_args_list[1].args[0], ["check"])
        self.assertTrue((self.base / "custom_nodes/SampleNode/__init__.py").is_file())
        with patch.dict(sys.modules["nodes"].NODE_CLASS_MAPPINGS, {"Example": type("Example", (), {"RELATIVE_PYTHON_MODULE": "custom_nodes.SampleNode"})}):
            item = next(item for item in self.installer.history() if item["name"] == "SampleNode")
            self.assertEqual(item["loaded_nodes"], ["Example"])

    def test_node_installer_accepts_direct_python_and_protects_existing_files(self):
        identifier = self.node_stage({"single_node.py": b"NODE_CLASS_MAPPINGS = {}"})
        summary = self.installer.prepare(identifier)
        self.assertEqual(summary["kind"], "file")
        self.wait_task(self.installer.install(identifier, "single_node.py"))
        second = self.node_stage({"single_node.py": b"NODE_CLASS_MAPPINGS = {}\n# new copy"})
        self.assertTrue(self.installer.prepare(second)["exists"])
        with self.assertRaises(FileExistsError):
            self.installer.install(second, "single_node.py")
        self.assertEqual((self.base / "custom_nodes/single_node.py").read_bytes(), b"NODE_CLASS_MAPPINGS = {}")

    def test_node_installer_rejects_unsafe_zip_and_invalid_python(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("../outside.py", "pass")
        identifier = self.node_stage({"bad.zip": archive.getvalue()})
        with self.assertRaises(ValueError):
            self.installer.prepare(identifier)
        invalid = self.node_stage({"bad.py": b"def broken(:"})
        with self.assertRaisesRegex(ValueError, "syntax error"):
            self.installer.prepare(invalid)

    def test_node_requirements_failure_does_not_install_package(self):
        identifier = self.node_stage({"FailNode/__init__.py": b"NODE_CLASS_MAPPINGS = {}", "FailNode/requirements.txt": b"missing-package"})
        self.installer.prepare(identifier)
        with patch.object(self.installer, "run_pip", return_value=(1, "No matching distribution")):
            task = self.installer.install(identifier, "FailNode")
            deadline = time.time() + 5
            while task.status in ("waiting", "running") and time.time() < deadline:
                time.sleep(0.01)
        self.assertEqual(task.status, "failed")
        self.assertFalse((self.base / "custom_nodes/FailNode").exists())

    def test_node_pip_uses_comfy_python_package_folder_and_redacted_output(self):
        process = MagicMock()
        process.stdout = io.StringIO("Installed package\nSource https://example.com/file?token=private-secret\n")
        process.poll.return_value = 0
        process.returncode = 0
        launch = MagicMock(return_value=process)
        subprocess_api = types.SimpleNamespace(Popen=launch, PIPE=-1, STDOUT=-2)
        task = self.tasks.Task("install-node", "Requirements test")
        folder = self.base / "custom_nodes"
        with patch.object(self.installer, "subprocess", subprocess_api):
            code, output = self.installer.run_pip(["install", "--no-input", "-r", "requirements.txt"], task, "Installing requirements", folder)
        self.assertEqual(code, 0)
        self.assertIn("Installed package", output)
        self.assertNotIn("private-secret", output)
        self.assertEqual(launch.call_args.args[0][:3], [sys.executable, "-m", "pip"])
        self.assertEqual(launch.call_args.kwargs["cwd"], str(folder))

    def test_node_folder_upload_preserves_paths_and_restart_is_confirmed_and_idle(self):
        async def run():
            application = web.Application()
            application.add_routes(self.routes.routes)
            async with TestClient(TestServer(application)) as client:
                form = FormData()
                form.add_field("file", b"NODE_CLASS_MAPPINGS = {}", filename="FolderNode/__init__.py")
                form.add_field("file", b"value = 1", filename="FolderNode/nested/helper.py")
                response = await client.post("/robot/custom-nodes/upload", data=form)
                self.assertEqual(response.status, 200, await response.text())
                summary = (await response.json())["package"]
                self.assertEqual((summary["name"], summary["files"]), ("FolderNode", 2))
                response = await client.post("/robot/custom-nodes/install", json={"id": summary["id"], "name": "FolderNode", "requirements": False})
                result = await response.json()
                self.wait_task(self.tasks.TASKS.tasks[result["task"]])
                self.assertTrue((self.base / "custom_nodes/FolderNode/nested/helper.py").is_file())
                response = await client.post("/robot/system/restart", json={})
                self.assertEqual(response.status, 400)
                queue = self.routes.PromptServer.instance.prompt_queue
                with patch.object(queue, "get_tasks_remaining", return_value=1):
                    response = await client.post("/robot/system/restart", json={"confirm_restart": True})
                    self.assertEqual(response.status, 400)
                with patch.object(self.installer, "restart_process") as restart:
                    response = await client.post("/robot/system/restart", json={"confirm_restart": True})
                    self.assertEqual(response.status, 200, await response.text())
                    await asyncio.sleep(1.1)
                    restart.assert_called_once_with()
        asyncio.run(run())

    def test_trash_keeps_content_when_move_reports_failure(self):
        source = self.base / "output" / "keep.txt"
        source.write_text("keep", encoding="utf-8")
        original_move = shutil.move
        def move_then_fail(start, destination):
            original_move(start, destination)
            raise OSError("Move reported a failure")
        with patch.object(self.tasks.shutil, "move", side_effect=move_then_fail):
            with self.assertRaises(OSError):
                self.tasks.trash_item("output", "keep.txt", source)
        item = next(item for item in self.tasks.trash_list() if item["path"] == "keep.txt")
        self.assertEqual((self.tasks.TRASH / item["id"] / "content").read_text(), "keep")
        self.tasks.trash_action(item["id"], "restore")
        self.assertEqual(source.read_text(), "keep")

    def test_archive_rejects_traversal_and_model_binary(self):
        for entry in ("../escape.txt", "outputs/evil.safetensors", "workflows/script.py"):
            identifier = uuid.uuid4().hex
            path = self.backup.IMPORTS / (identifier + ".zip")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("manifest.json", json.dumps({"backup_format": self.backup.FORMAT, "schema_version": 1}))
                archive.writestr(entry, "data")
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self.backup.validate_archive(path)
        path = self.backup.IMPORTS / (uuid.uuid4().hex + ".zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"backup_format": self.backup.FORMAT, "schema_version": 1}))
            archive.writestr("models_manifest.json", json.dumps([{"name": "x.safetensors", "sources": {
                "direct": "https://example.com/x.safetensors?token=secret"}}]))
        with self.assertRaises(ValueError):
            self.backup.validate_archive(path)
        path = self.backup.IMPORTS / (uuid.uuid4().hex + ".zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"backup_format": self.backup.FORMAT, "schema_version": 1}))
            archive.writestr("outputs/Image.png", b"one")
            archive.writestr("outputs/image.png", b"two")
        with self.assertRaises(ValueError):
            self.backup.validate_archive(path)

    def test_generic_zip_extraction_stays_in_destination(self):
        source = self.base / "input/archive.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("nested/safe.txt", "ok")
        self.wait_task(self.tasks.extract_archive("input", "archive.zip", "output", ""))
        self.assertEqual((self.base / "output/nested/safe.txt").read_text(), "ok")
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("../escape.txt", "bad")
        task = self.tasks.extract_archive("input", "archive.zip", "output", "")
        deadline = time.time() + 5
        while task.status in ("waiting", "running") and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(task.status, "failed")
        self.assertFalse((self.base / "escape.txt").exists())

    def test_backup_has_manifest_without_model_binary_and_restores_workflow(self):
        model = self.base / "models/checkpoints/example.safetensors"
        model.write_bytes(b"model bytes")
        workflow = self.base / "user/default/workflows/test.json"
        workflow.write_text(json.dumps({"nodes": [{"type": "LoadCheckpoint", "widgets_values": ["example.safetensors"]}]}), encoding="utf-8")
        output = self.base / "output/generated.png"
        output.write_bytes(b"png bytes")
        result = self.wait_task(self.backup.create_backup({"workflows": True, "models": True, "custom_nodes": True,
                                                           "outputs": "all"}))
        archive_path = self.backup.EXPORTS / (result["export_id"] + ".zip")
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            self.assertIn("models_manifest.json", names)
            self.assertIn("outputs/generated.png", names)
            self.assertIn("workflows/user/default/test.json", names)
            self.assertFalse(any(name.endswith(".safetensors") for name in names))
            manifest = json.loads(archive.read("models_manifest.json"))
            self.assertEqual(manifest[0]["name"], model.name)
        imported = self.backup.IMPORTS / (uuid.uuid4().hex + ".zip")
        shutil.copyfile(archive_path, imported)
        summary = self.backup.validate_archive(imported)
        self.assertEqual(summary["workflows"], 1)
        workflow.unlink()
        self.wait_task(self.backup.restore_backup(imported.stem, workflows=True, outputs=False))
        self.assertTrue(workflow.is_file())

    def test_http_routes_smoke(self):
        app = web.Application()
        app.add_routes(self.routes.routes)

        async def exercise():
            async with TestServer(app) as test_server, TestClient(test_server) as client:
                response = await client.get("/robot/files/roots")
                self.assertEqual(response.status, 200)
                data = await response.json()
                self.assertIn("output", [root["id"] for root in data["roots"]])
                response = await client.get("/robot/files/list", params={"root": "output", "path": "../user"})
                self.assertEqual(response.status, 400)
                response = await client.post("/robot/files/mkdir", json={"root": "output", "path": "", "name": "new-folder"})
                self.assertEqual(response.status, 200)
                response = await client.get("/robot/files/list", params={"root": "output", "path": ""})
                self.assertIn("new-folder", [item["name"] for item in (await response.json())["entries"]])
                response = await client.get("/robot/logs")
                self.assertEqual(response.status, 200)

        asyncio.run(exercise())

    def test_migration_selects_files_and_restores_local_and_github_nodes(self):
        identifier = uuid.uuid4().hex
        path = self.backup.IMPORTS / (identifier + ".zip")
        local = "migration-local"
        remote = "migration-github"
        destination = {"archive": "workflows/user/default/migration.json", "root": "user", "path": "default/workflows/migration.json"}
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"backup_format": self.backup.FORMAT, "schema_version": 3,
                "bundled_custom_nodes": [local], "workflow_destinations": [destination]}))
            archive.writestr(destination["archive"], '{"nodes": []}')
            archive.writestr("outputs/migration-skipped.png", b"skip")
            archive.writestr("custom_nodes/" + local + "/__init__.py", "NODE_CLASS_MAPPINGS = {}")
            archive.writestr("custom_nodes_manifest.json", json.dumps([{"package": local}, {"package": remote,
                "repository": "https://github.com/example/migration-github", "commit": "a" * 40}]))
        def github(repository, name, revision, requirements, task=None):
            self.assertEqual(revision, "a" * 40)
            folder = self.base / "custom_nodes" / name
            folder.mkdir()
            (folder / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}")
            return {"installed": True}
        with patch.object(self.installer, "install_github", side_effect=github) as install:
            result = self.wait_task(self.migration.restore(identifier, {"selected_files": [destination["archive"]],
                "custom_nodes": [local, remote], "install_requirements": False, "install_manager": False}))
        self.assertTrue((self.base / "user/default/workflows/migration.json").is_file())
        self.assertFalse((self.base / "output/migration-skipped.png").exists())
        self.assertTrue((self.base / "custom_nodes" / local / "__init__.py").is_file())
        self.assertEqual(len(result["report"]["errors"]), 0)
        install.assert_called_once()

    def test_migration_downloads_exact_model_and_reports_unknown_sources(self):
        path = self.backup.IMPORTS / (uuid.uuid4().hex + ".zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"backup_format": self.backup.FORMAT, "schema_version": 3}))
            archive.writestr("models_manifest.json", json.dumps([
                {"name": "migration-download.safetensors", "sources": {"direct": "https://example.com/migration-download.safetensors"}},
                {"name": "unknown-download.safetensors"}]))
        async def source(value):
            return {"files": [{"name": "migration-download.safetensors", "url": value}]}
        async def download(task, url, target, token=None):
            target.write_bytes(b"model")
        with patch.object(self.providers, "source_info", side_effect=source), patch.object(self.tasks, "_download", side_effect=download):
            task = self.migration.restore(path.stem, {"selected_files": [], "install_manager": False,
                "models": [{"index": 0, "root": "models", "folder": "migration"}, {"index": 1}]})
            deadline = time.time() + 5
            while task.status in ("waiting", "running") and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(task.status, "failed")
            self.assertTrue(task.public()["retryable"])
            result = task.result
        self.assertEqual((self.base / "models/migration/migration-download.safetensors").read_bytes(), b"model")
        self.assertEqual(result["report"]["errors"][0]["name"], "unknown-download.safetensors")
        self.assertIn("source", result["report"]["errors"][0]["error"])

    def test_migration_rejects_unknown_selections_before_starting(self):
        path = self.backup.IMPORTS / (uuid.uuid4().hex + ".zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("manifest.json", json.dumps({"backup_format": self.backup.FORMAT, "schema_version": 2}))
        for options in ({"custom_nodes": ["../bad"]}, {"models": [{"index": 4}]}, {"selected_files": ["outputs/missing.txt"]}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.migration.restore(path.stem, options)

    def test_setup_preset_bundles_unpublished_modified_and_single_file_nodes(self):
        for name in ("preset-local", "preset-clean", "preset-dirty"):
            folder = self.base / "custom_nodes" / name
            folder.mkdir(exist_ok=True)
            (folder / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}")
        single = self.base / "custom_nodes/preset_single.py"
        single.write_text("NODE_CLASS_MAPPINGS = {}")
        records = [{"package": "preset-local", "repository": None},
            {"package": "preset-clean", "repository": "https://github.com/example/clean", "commit": "a" * 40, "modified": False},
            {"package": "preset-dirty", "repository": "https://github.com/example/dirty", "modified": True},
            {"package": single.name, "repository": None}]
        with patch.object(self.backup, "custom_node_manifest", return_value=records):
            options = self.backup.setup_options({"preset": "setup", "outputs": "skip"})
            self.assertEqual(options["bundled_custom_nodes"], ["preset-local", "preset-dirty", single.name])
            result = self.wait_task(self.backup.create_backup({"preset": "setup", "outputs": "skip"}))
        exported = self.backup.EXPORTS / (result["export_id"] + ".zip")
        with zipfile.ZipFile(exported) as archive:
            self.assertIn("environment.json", archive.namelist())
            self.assertNotIn("custom_nodes/preset-clean/__init__.py", archive.namelist())
            self.assertEqual(archive.getinfo("manifest.json").compress_type, zipfile.ZIP_DEFLATED)
        imported = self.backup.IMPORTS / (uuid.uuid4().hex + ".zip")
        shutil.copyfile(exported, imported)
        single.unlink()
        self.wait_task(self.backup.restore_backup(imported.stem, workflows=False, custom_nodes=[single.name]))
        self.assertEqual(single.read_text(), "NODE_CLASS_MAPPINGS = {}")

    def test_custom_authorization_is_host_scoped_and_excluded_from_export(self):
        self.providers.set_auth_profile("private", "example.com", "header", "secret-custom-key", header="X-API-Key")
        try:
            self.assertEqual(self.providers.authorization_headers("https://example.com/model.bin", "private"), {"X-API-Key": "secret-custom-key"})
            self.assertNotIn("secret-custom-key", json.dumps(self.providers.auth_profiles()))
            with self.assertRaises(ValueError):
                self.providers.authorization_headers("https://other.example.com/model.bin", "private")
            result = self.wait_task(self.backup.create_backup({"workflows": False, "models": False, "outputs": "skip"}))
            with zipfile.ZipFile(self.backup.EXPORTS / (result["export_id"] + ".zip")) as archive:
                self.assertFalse(any(b"secret-custom-key" in archive.read(name) for name in archive.namelist()))
        finally:
            self.providers.set_auth_profile("private", "", "delete", "")

    def test_custom_authorization_rejects_header_injection_and_reserved_headers(self):
        for header, secret in (("Host", "value"), ("Range", "bytes=0-"), ("X-Key", "value\r\nHost: other")):
            with self.subTest(header=header), self.assertRaises(ValueError):
                self.providers.set_auth_profile("bad", "example.com", "header", secret, header=header)
        self.providers.set_auth_profile("basic", "example.com", "basic", "password", username="user")
        try:
            self.assertEqual(self.providers.authorization_headers("https://example.com/x", "basic"), {"Authorization": "Basic dXNlcjpwYXNzd29yZA=="})
        finally:
            self.providers.set_auth_profile("basic", "", "delete", "")

    def test_cleanup_preserves_active_partial_download_and_removes_inactive_partial(self):
        active = self.base / "models/active-cleanup.bin"
        inactive = self.base / "models/inactive-cleanup.bin.robot-part"
        active.with_name(active.name + ".robot-part").write_bytes(b"active")
        inactive.write_bytes(b"inactive")
        inactive.with_name(inactive.name + ".json").write_text('{}')
        task = self.tasks.TASKS.external("download", "paused")
        task.target = active
        task.status = "paused"
        try:
            candidates = self.storage.cleanup_candidates()
            self.assertNotIn(active.with_name(active.name + ".robot-part"), [item["path"] for item in candidates])
            result = self.wait_task(self.storage.clean(["partials"]))
            self.assertTrue(active.with_name(active.name + ".robot-part").exists())
            self.assertFalse(inactive.exists())
            self.assertFalse(inactive.with_name(inactive.name + ".json").exists())
            self.assertGreaterEqual(result["removed_bytes"], len(b"inactive"))
        finally:
            self.tasks.TASKS.control(task.id, "remove")
            active.with_name(active.name + ".robot-part").unlink(missing_ok=True)

    def test_delete_refuses_files_reserved_by_an_active_writer(self):
        source = self.base / "output/reserved-delete.txt"
        source.write_text("keep")
        self.tasks.TASKS.reserve(source)
        try:
            task = self.tasks.file_operation("delete", [{"root": "output", "path": source.name}], permanent=True)
            deadline = time.time() + 5
            while task.status in ("waiting", "running") and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(task.status, "failed")
            self.assertTrue(source.exists())
        finally:
            self.tasks.TASKS.release(source)

    def test_github_installer_uses_archive_without_git_and_protects_existing_nodes(self):
        async def download(task, url, target, token=None):
            with zipfile.ZipFile(target, "w") as archive:
                archive.writestr("example-GitHubNode-abcdef0/__init__.py", "NODE_CLASS_MAPPINGS = {}")
        find_tool = shutil.which
        with patch.object(self.installer, "_download", side_effect=download), patch.object(self.installer.shutil, "which", side_effect=lambda name: None if name == "git" else find_tool(name)):
            result = self.wait_task(self.installer.install_github("https://github.com/example/GitHubNode", requirements=False))
        self.assertTrue(result["installed"])
        marker = json.loads((self.base / "custom_nodes/GitHubNode/.robot-source.json").read_text())
        self.assertEqual(marker["repository"], "https://github.com/example/GitHubNode")
        with self.assertRaises(FileExistsError):
            self.installer.install_github("https://github.com/example/GitHubNode")
        for value in ("https://github.com/example/node/tree/main", "https://evil.example/a/b", "https://github.com/a/b?token=secret"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.installer.github_repository(value)

    def test_browser_search_uses_provider_api_and_preserves_model_source(self):
        async def response(url, platform):
            self.assertIn("query=landscape", url)
            return {"items": [{"id": 10, "name": "Landscape", "type": "LORA", "modelVersions": []}], "metadata": {"nextPage": "ignored"}}
        with patch.object(self.providers, "request_json", side_effect=response):
            result = asyncio.run(self.providers.browse_models("civitai", "landscape"))
        self.assertEqual(result["items"][0]["url"], "https://civitai.com/models/10")
        self.assertTrue(result["more"])

    def test_download_authorization_is_removed_on_cross_host_redirect(self):
        captured = []
        class Content:
            async def iter_chunked(self, _size):
                yield b"data"
        class Response:
            def __init__(self, redirected):
                self.status = 302 if redirected else 200
                self.headers = {"Location": "https://cdn.example.com/model.bin"} if redirected else {}
                self.content = Content()
                self.content_length = 4
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_args):
                pass
        class Session:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *_args):
                pass
            def get(self, url, headers, allow_redirects):
                captured.append((url, dict(headers)))
                return Response(len(captured) == 1)
        target = self.base / "models/redirect-auth.bin"
        with patch.object(self.tasks, "public_url"), patch.object(self.tasks.aiohttp, "TCPConnector"), \
                patch.object(self.tasks.aiohttp, "ClientSession", return_value=Session()):
            asyncio.run(self.tasks._download(self.tasks.Task("download", "redirect"), "https://example.com/model.bin", target,
                                            authorization={"X-API-Key": "private-header"}))
        self.assertEqual(captured[0][1]["X-API-Key"], "private-header")
        self.assertNotIn("X-API-Key", captured[1][1])
        self.assertEqual(target.read_bytes(), b"data")

    def test_github_clone_keeps_repository_and_default_branch_for_manager_updates(self):
        commands = []
        def command(arguments, task, label, cwd, env=None):
            commands.append(arguments)
            if "clone" in arguments:
                folder = Path(arguments[-1])
                (folder / ".git/refs/heads").mkdir(parents=True)
                (folder / ".git/HEAD").write_text("ref: refs/heads/main\n")
                (folder / ".git/refs/heads/main").write_text("b" * 40)
                (folder / ".git/config").write_text('[remote "origin"]\nurl = https://github.com/example/CloneNode\n')
                (folder / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}")
                self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
                self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "https")
            return 0, "done"
        find_tool = shutil.which
        with patch.object(self.installer, "_run_command", side_effect=command), \
                patch.object(self.installer.shutil, "which", side_effect=lambda name: "git-test" if name == "git" else find_tool(name)):
            result = self.wait_task(self.installer.install_github("https://github.com/example/CloneNode", requirements=False))
        self.assertTrue(result["git_checkout"])
        self.assertEqual(result["commit"], "b" * 40)
        self.assertTrue((self.base / "custom_nodes/CloneNode/.git/HEAD").is_file())
        self.assertIn("--force", commands[-1])
        self.assertNotIn("--detach", commands[-1])

    def test_setup_install_is_default_and_attempted_once(self):
        async def run():
            application = web.Application()
            application.add_routes(self.routes.routes)
            async with TestClient(TestServer(application)) as client:
                with patch.object(self.installer, "manager_present", return_value=False), patch.object(self.installer, "install_github", return_value=types.SimpleNamespace(id="manager-task")) as install:
                    self.security.write_json(self.security.SETTINGS, {}, private=True)
                    first = await client.post("/robot/setup", json={})
                    self.assertEqual((await first.json())["task"], "manager-task")
                    second = await client.post("/robot/setup", json={})
                    self.assertNotIn("task", await second.json())
                    install.assert_called_once()
                response = await client.post("/robot/storage/cleanup", json={"groups": ["trash"]})
                self.assertEqual(response.status, 400)
        try:
            asyncio.run(run())
        finally:
            self.security.write_json(self.security.SETTINGS, {}, private=True)

    def test_export_import_round_trip_into_a_fresh_comfyui_layout(self):
        workflow = self.base / "user/default/workflows/portable.json"
        workflow.write_text('{"nodes": []}', encoding="utf-8")
        output = self.base / "output/portable.png"
        output.write_bytes(b"portable-output")
        package = self.base / "custom_nodes/portable-private"
        package.mkdir(exist_ok=True)
        (package / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}", encoding="utf-8")
        profile = self.base / "user/portable-profile"
        profile.mkdir(exist_ok=True)
        (profile / "comfy.settings.json").write_text(json.dumps({"Comfy.Graph.ZoomSpeed": 1.5, "Secret.Token": "exclude"}))
        exported = self.wait_task(self.backup.create_backup({"preset": "setup", "outputs": "all"}))
        imported = self.backup.IMPORTS / (uuid.uuid4().hex + ".zip")
        shutil.copyfile(self.backup.EXPORTS / (exported["export_id"] + ".zip"), imported)
        fresh = Path(self.temp.name) / "FreshComfyUI"
        for name in ("custom_nodes", "models", "output", "input", "temp", "user/.robot_file_manager"):
            (fresh / name).mkdir(parents=True, exist_ok=True)
        paths = sys.modules["folder_paths"]
        with patch.multiple(paths, base_path=str(fresh), models_dir=str(fresh / "models"), folder_names_and_paths={},
                get_user_directory=lambda: str(fresh / "user"), get_output_directory=lambda: str(fresh / "output"),
                get_input_directory=lambda: str(fresh / "input"), get_temp_directory=lambda: str(fresh / "temp")), \
                patch.object(self.backup, "SETTINGS", fresh / "user/.robot_file_manager/settings.json"):
            summary = self.backup.validate_archive(imported)
            files = [item["archive"] for item in summary["files"] if item["path"].endswith(("portable.json", "portable.png"))]
            result = self.wait_task(self.migration.restore(imported.stem, {"selected_files": files,
                "custom_nodes": ["portable-private"], "models": [], "install_manager": False, "install_requirements": False}))
        self.assertFalse(result["report"]["errors"])
        self.assertEqual((fresh / "output/portable.png").read_bytes(), b"portable-output")
        self.assertTrue((fresh / "user/default/workflows/portable.json").is_file())
        self.assertTrue((fresh / "custom_nodes/portable-private/__init__.py").is_file())
        preferences = json.loads((fresh / "user/portable-profile/comfy.settings.json").read_text())
        self.assertEqual(preferences, {"Comfy.Graph.ZoomSpeed": 1.5})

    def test_output_only_archive_is_importable_and_large_selection_uses_exclusions(self):
        output = self.base / "output/output-only-roundtrip.png"
        output.write_bytes(b"output-only")
        exported = self.wait_task(self.backup.create_backup({"workflows": False, "models": False,
            "custom_nodes": False, "plugin_settings": False, "comfy_settings": False, "outputs": "all"}))
        imported = self.backup.IMPORTS / (uuid.uuid4().hex + ".zip")
        shutil.copyfile(self.backup.EXPORTS / (exported["export_id"] + ".zip"), imported)
        output.unlink()
        summary = self.backup.validate_archive(imported)
        self.assertEqual(summary["workflows"], 0)
        self.assertEqual(summary["models"], [])
        excluded = [item["archive"] for item in summary["files"] if item["path"] != output.name]
        self.wait_task(self.migration.restore(imported.stem, {"excluded_files": excluded, "install_manager": False,
            "comfy_settings": False, "plugin_settings": False}))
        self.assertEqual(output.read_bytes(), b"output-only")

    def safetensor(self, name, tensors, metadata=None):
        path = self.base / "models" / name
        header = {key: {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]} for key in tensors}
        header["__metadata__"] = metadata or {}
        raw = json.dumps(header).encode()
        path.write_bytes(struct.pack("<Q", len(raw)) + raw + b"1234")
        return path

    def test_lora_header_detection_and_metadata_without_loading_weights(self):
        path = self.safetensor("detected-lora.safetensors", ["lora_unet_test.lora_down.weight"],
            {"ss_base_model_version": "sdxl", "ss_output_name": "Test LoRA", "modelspec.trigger_phrase": "robot, metallic", "recommended_strength": "0.7"})
        model = next(item for item in self.catalog.scan_models() if item["name"] == path.name)
        self.assertEqual(model["type"], "lora")
        self.assertEqual(model["details"]["base_model"], "sdxl")
        self.assertEqual(model["details"]["trigger_words"], ["robot", "metallic"])
        self.assertEqual(model["details"]["recommended_strength"], "0.7")

    def test_header_detection_distinguishes_checkpoint_diffusion_and_vae(self):
        examples = [("checkpoint", ["model.diffusion_model.input_blocks.0.weight", "first_stage_model.decoder.weight"]),
            ("diffusion_model", ["double_blocks.0.weight"]), ("vae", ["encoder.conv.weight", "decoder.conv.weight", "quant_conv.weight"]),
            ("other", ["unknown.weight"])]
        for expected, keys in examples:
            path = self.safetensor("detected-" + expected + ".safetensors", keys)
            self.assertEqual(self.catalog.details(path)["type"], expected)

    def test_manual_metadata_survives_scan_rename_and_copy(self):
        path = self.safetensor("manual-model.safetensors", ["lora_unet_x.lora_down.weight"])
        self.catalog.edit_details("models", path.name, {"type": "diffusion_model", "name": "Custom label", "notes": "keep me"})
        self.wait_task(self.catalog.hash_model("models", path.name))
        self.wait_task(self.tasks.file_operation("rename", [{"root": "models", "path": path.name}], {"root": "models", "path": ""}, "manual-renamed.safetensors"))
        model = next(item for item in self.catalog.scan_models() if item["name"] == "manual-renamed.safetensors")
        self.assertEqual(model["type"], "diffusion_model")
        self.assertEqual(model["details"]["notes"], "keep me")
        self.assertTrue(model["sha256"])
        self.wait_task(self.tasks.file_operation("duplicate", [{"root": "models", "path": model["path"]}], {"root": "models", "path": ""}, "manual-copy.safetensors"))
        duplicate = next(item for item in self.catalog.scan_models() if item["name"] == "manual-copy.safetensors")
        self.assertEqual(duplicate["details"]["name"], "Custom label")
        self.assertEqual(duplicate["sha256"], model["sha256"])

    def test_changed_model_invalidates_hash_and_header_cache(self):
        path = self.safetensor("changing-model.safetensors", ["lora_unet_x.lora_down.weight"])
        self.wait_task(self.catalog.hash_model("models", path.name))
        self.safetensor(path.name, ["double_blocks.0.weight"], {"ss_output_name": "Replaced"})
        model = next(item for item in self.catalog.scan_models() if item["name"] == path.name)
        self.assertEqual(model["type"], "diffusion_model")
        self.assertIsNone(model["sha256"])

    def test_identification_saves_verified_provider_metadata_and_preserves_manual_type(self):
        path = self.safetensor("online-lora.safetensors", ["lora_unet_x.lora_down.weight"])
        self.catalog.edit_details("models", path.name, {"type": "other", "notes": "my note"})
        async def sources(filename, sha256):
            self.assertEqual(sha256, hashlib.sha256(path.read_bytes()).hexdigest())
            return [{"platform": "civitai", "url": "https://civitai.com/api/download/models/123", "confidence": "Exact Hash Match",
                "details": {"type": "lora", "name": "Verified LoRA", "base_model": "SDXL", "trigger_words": ["robot"]}}]
        with patch.object(self.providers, "find_sources", side_effect=sources):
            result = self.wait_task(self.identification.identify([{"root": "models", "path": path.name}], online=True, calculate_hash=True))
        self.assertEqual(result["identification"]["verified_sources"], 1)
        model = next(item for item in self.catalog.scan_models() if item["name"] == path.name)
        self.assertEqual(model["type"], "other")
        self.assertEqual(model["details"]["notes"], "my note")
        self.assertEqual(model["details"]["base_model"], "SDXL")
        self.assertEqual(model["source_url"], "https://civitai.com/api/download/models/123")

    def test_unverified_source_candidates_do_not_become_saved_source(self):
        path = self.safetensor("possible-source.safetensors", ["unknown.weight"])
        async def sources(*_args):
            return [{"platform": "huggingface", "url": "https://huggingface.co/example/possible", "confidence": "Possible Match"}]
        with patch.object(self.providers, "find_sources", side_effect=sources):
            self.wait_task(self.identification.identify([{"root": "models", "path": path.name}], online=True))
        model = next(item for item in self.catalog.scan_models() if item["name"] == path.name)
        self.assertIsNone(model["source_url"])
        self.assertEqual(model["details"]["candidates"][0]["confidence"], "Possible Match")

    def test_invalid_or_unsupported_header_returns_unknown(self):
        path = self.base / "models/invalid-header.safetensors"
        path.write_bytes(struct.pack("<Q", 2 ** 63))
        self.assertEqual(self.catalog.details(path)["type"], "other")
        path = self.base / "models/unsafe-pickle.ckpt"
        path.write_bytes(b"pickle content must not execute")
        self.assertEqual(self.catalog.details(path)["type"], "other")

    def test_metadata_round_trip_in_backup_preserves_target_manual_corrections(self):
        path = self.safetensor("annotations.safetensors", ["lora_unet_x.lora_down.weight"])
        self.catalog.edit_details("models", path.name, {"type": "lora", "base_model": "SDXL", "notes": "exported note"})
        result = self.wait_task(self.backup.create_backup({"workflows": False, "models": True, "outputs": "skip"}))
        with zipfile.ZipFile(self.backup.EXPORTS / (result["export_id"] + ".zip")) as archive:
            record = next(item for item in json.loads(archive.read("models_manifest.json")) if item["name"] == path.name)
        self.assertEqual(record["metadata"]["manual"]["notes"], "exported note")
        self.catalog.edit_details("models", path.name, {"type": "lora", "notes": "target correction"})
        self.backup.restore_model_metadata(record, path)
        self.assertEqual(self.catalog.details(path)["notes"], "target correction")

    def update_fixture(self, name):
        root = Path(self.temp.name) / name
        (root / "web").mkdir(parents=True)
        (root / "__init__.py").write_text("# old version\n")
        (root / "routes.py").write_text("VALUE = 1\n")
        (root / "web/manager.js").write_text("// old JS")
        (root / "web/manager.css").write_text("/* old CSS */")
        return root

    def test_archive_self_update_and_rollback_preserve_recovery(self):
        root = self.update_fixture("UpdateTarget")
        private = Path(self.temp.name) / "UpdatePrivate"
        private.mkdir()
        async def download(task, url, target, token=None):
            with zipfile.ZipFile(target, "w") as archive:
                for name, text in {"__init__.py": "# new version\n", "routes.py": "VALUE = 2\n", "web/manager.js": "// new JS", "web/manager.css": "/* new CSS */"}.items():
                    archive.writestr("repo/" + name, text)
        with patch.multiple(self.updater, ROOT=root, DATA=private, STATE=private / "state.json", RECOVERY=private / "recovery"), \
                patch.object(self.updater, "idle", return_value=True), patch.object(self.updater, "_download", side_effect=download):
            state = self.updater.initialize()
            state["latest"] = {"available": True, "commit": "a" * 40}
            self.security.write_json(self.updater.STATE, state, private=True)
            result = self.wait_task(self.updater.install_update())
            self.assertTrue(result["restart_required"])
            self.assertEqual((root / "routes.py").read_text(), "VALUE = 2\n")
            self.wait_task(self.updater.rollback())
            self.assertEqual((root / "routes.py").read_text(), "VALUE = 1\n")

    def test_self_update_refuses_local_changes_and_busy_server(self):
        root = self.update_fixture("ModifiedUpdateTarget")
        private = Path(self.temp.name) / "ModifiedUpdatePrivate"
        private.mkdir()
        with patch.multiple(self.updater, ROOT=root, DATA=private, STATE=private / "state.json", RECOVERY=private / "recovery"), patch.object(self.updater, "idle", return_value=True):
            self.updater.initialize()
            (root / "routes.py").write_text("VALUE = 'local correction'\n")
            with self.assertRaisesRegex(ValueError, "Local plugin changes"):
                self.updater.install_update()
        with patch.object(self.updater, "idle", return_value=False), self.assertRaisesRegex(ValueError, "Finish workflows"):
            self.updater.install_update()

    def test_update_archive_rejects_traversal_without_changing_installation(self):
        root = self.update_fixture("UnsafeUpdateTarget")
        private = Path(self.temp.name) / "UnsafeUpdatePrivate"
        private.mkdir()
        async def download(task, url, target, token=None):
            with zipfile.ZipFile(target, "w") as archive:
                archive.writestr("repo/../../outside.py", "pass")
        with patch.multiple(self.updater, ROOT=root, DATA=private, STATE=private / "state.json", RECOVERY=private / "recovery"), \
                patch.object(self.updater, "idle", return_value=True), patch.object(self.updater, "_download", side_effect=download):
            state = self.updater.initialize()
            state["latest"] = {"available": True, "commit": "b" * 40}
            self.security.write_json(self.updater.STATE, state, private=True)
            task = self.updater.install_update()
            deadline = time.time() + 5
            while task.status in ("waiting", "running") and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(task.status, "failed")
            self.assertEqual((root / "routes.py").read_text(), "VALUE = 1\n")

    def test_update_check_reports_remote_version_without_executing_it(self):
        root = self.update_fixture("CheckUpdateTarget")
        private = Path(self.temp.name) / "CheckUpdatePrivate"
        private.mkdir()
        async def api(url, platform=None):
            if "/contents/" in url:
                return {"encoding": "base64", "content": base64.b64encode(b'VERSION = "9.1.0"\nraise RuntimeError("must not execute")').decode()}
            if "/commits/" in url:
                return {"sha": "c" * 40, "commit": {"message": "New update"}}
            return {"default_branch": "main"}
        with patch.multiple(self.updater, ROOT=root, DATA=private, STATE=private / "state.json", RECOVERY=private / "recovery"), \
                patch.object(self.providers, "request_json", side_effect=api):
            result = asyncio.run(self.updater.check_updates())
        self.assertEqual(result["latest"]["version"], "9.1.0")
        self.assertTrue(result["latest"]["available"])

    def test_failed_archive_update_restores_previous_files(self):
        root = self.update_fixture("RollbackOnFailureTarget")
        private = Path(self.temp.name) / "RollbackOnFailurePrivate"
        private.mkdir()
        async def download(task, url, target, token=None):
            with zipfile.ZipFile(target, "w") as archive:
                for name in ("__init__.py", "routes.py", "web/manager.js", "web/manager.css"):
                    archive.writestr("repo/" + name, "# new\n" if name.endswith(".py") else "/* new */")
        real_copy = shutil.copyfile
        def fail_copy(source, target, *args, **kwargs):
            if str(target).endswith("routes.py.robot-update"):
                raise OSError("simulated write failure")
            return real_copy(source, target, *args, **kwargs)
        with patch.multiple(self.updater, ROOT=root, DATA=private, STATE=private / "state.json", RECOVERY=private / "recovery"), \
                patch.object(self.updater, "idle", return_value=True), patch.object(self.updater, "_download", side_effect=download), \
                patch.object(self.updater.shutil, "copyfile", side_effect=fail_copy):
            state = self.updater.initialize()
            state["latest"] = {"available": True, "commit": "d" * 40}
            self.security.write_json(self.updater.STATE, state, private=True)
            task = self.updater.install_update()
            deadline = time.time() + 5
            while task.status in ("waiting", "running") and time.time() < deadline:
                time.sleep(0.02)
            self.assertEqual(task.status, "failed")
            self.assertEqual((root / "routes.py").read_text(), "VALUE = 1\n")
            self.assertEqual((root / "__init__.py").read_text(), "# old version\n")

    def test_resource_refresh_handles_a_destination_not_created_yet(self):
        async def run():
            app = web.Application()
            app.add_routes(self.routes.routes)
            async with TestClient(TestServer(app)) as client:
                response = await client.get("/robot/system/stats", params={"root": "models", "path": "not-created-yet/subfolder"})
                self.assertEqual(response.status, 200, await response.text())
                self.assertGreater((await response.json())["disk"]["total"], 0)
        asyncio.run(run())

    def test_update_git_trust_is_scoped_to_its_own_installation(self):
        process = types.SimpleNamespace(returncode=0, stdout=b"test", stderr=b"")
        with patch.object(self.updater.subprocess, "run", return_value=process) as run:
            self.updater.git("rev-parse", "HEAD")
        arguments = run.call_args.args[0]
        self.assertIn("safe.directory=" + str(self.updater.ROOT.resolve()), arguments)
        self.assertNotIn("safe.directory=*", arguments)

    def test_automatic_update_tolerates_a_cached_failed_check_without_latest(self):
        root = self.update_fixture("CachedFailedUpdateTarget")
        private = Path(self.temp.name) / "CachedFailedUpdatePrivate"
        private.mkdir()
        with patch.multiple(self.updater, ROOT=root, DATA=private, STATE=private / "state.json", RECOVERY=private / "recovery"), \
                patch.object(self.updater, "settings", return_value={"check_updates": True, "auto_update": True}), \
                patch.object(self.updater, "install_update") as install:
            state = self.updater.initialize()
            state["checked_at"] = time.time()
            self.security.write_json(self.updater.STATE, state, private=True)
            self.updater.automatic()
            install.assert_not_called()
            self.assertNotIn("last_error", self.security.read_json(self.updater.STATE, {}))


if __name__ == "__main__":
    unittest.main()
