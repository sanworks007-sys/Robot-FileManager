import asyncio
import importlib
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch


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
            self.assertEqual(json.loads(archive.read("manifest.json"))["schema_version"], 2)
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
        self.assertEqual(self.security.settings(), {"trash": False, "export_ttl_hours": 12})
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
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        server = types.ModuleType("server")
        server.PromptServer = type("PromptServer", (), {"instance": types.SimpleNamespace(routes=web.RouteTableDef())})
        sys.modules["server"] = server
        importlib.import_module("rfm.routes")
        app = web.Application()
        app.add_routes(server.PromptServer.instance.routes)

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


if __name__ == "__main__":
    unittest.main()
