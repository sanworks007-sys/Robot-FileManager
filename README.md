# Robot File Manager

A browser based file, download, model, workflow, and workspace migration manager for ComfyUI. It uses ComfyUI's existing `aiohttp` dependency and runs on Windows and headless Linux servers.

## Install

Place this directory at `ComfyUI/custom_nodes/Robot-FileManager`, restart ComfyUI, then click **Files** at the lower right of the web interface. The panel includes Files, Downloads, Models, Workflows, Outputs, Backup & Restore, Tasks, Trash, and Settings.

Protect remote ComfyUI access with authentication or a trusted network boundary. This extension exposes file modification and server download routes to anyone who can access the ComfyUI API.

## Main flows

- **Files:** browse approved roots, upload, download, copy, move, rename, duplicate, create folders or text files, delete to Trash, ZIP selected files, and safely extract ZIPs. Root, path, and symlink checks run on the server for every operation.
- **Downloads:** inspect a direct HTTPS URL, Hugging Face repository or file URL, or Civitai model/version URL. Select exact files and a destination root/folder before queueing. Downloads stream to `.robot-part` files on the server and resume when the same source is queued again. Pause, resume, cancel, retry, progress, speed, and task history are in the panel. Provider SHA256 hashes are verified when available.
- **Models and workflows:** scan ComfyUI model folders, including unregistered folders under `models`, calculate hashes on demand, save public source links, analyze workflow JSON, and look up source candidates. Possible matches require user selection.
- **Backup & Restore:** preview, create, and download a versioned ZIP with workflows, model and custom node manifests, safe settings, and optional output files. Normal backups exclude model binaries. Select custom node folders to bundle their code so they can be restored without another download. Import compares installed dependencies, restores selected workflows and outputs, offers individual or batch missing-model downloads, and skips custom node folders already present. Requirements for selected bundled nodes can be installed into ComfyUI's Python environment only with a separate confirmation. Restart ComfyUI after restoring node code. Metadata-only custom nodes show repository guidance.
- **Outputs:** browse image and video previews, filter by date and extension, and ZIP selected files. Backup output filters support a folder, selected paths, date range, and extensions.

The built in downloader uses native HTTP streaming with three concurrent background workers. It does not require `aria2`, `wget`, or `curl` and does not perform segmented downloading. Active tasks do not restart automatically after a server restart; completed task history is saved, and a partial download can be resumed by queueing the same source and destination again.

## Paths and private data

Default roots come from ComfyUI's configured base, model, input, output, temp, user, and custom node folders. The server owner can add roots in `ComfyUI/user/.robot_file_manager/allowed_roots.json`:

```json
{
  "shared_models": "/mnt/shared-models"
}
```

Custom roots appear as `extra:shared_models`. The browser cannot approve an arbitrary server path. The plugin's `.robot_file_manager` data directory is excluded from browsing and ZIPs. Credentials live there separately from backups. On Windows, tokens use DPAPI when available, with a restricted file ACL as a fallback; on Linux, the directory and credential files are owner only.

For full Linux filesystem browsing, the server owner can explicitly add `"linux": "/"` to that JSON file, then reload the ComfyUI page. **Linux filesystem (/)** will appear in the Files root selector. Files can be moved between approved roots, and the Model Library provides direct Open folder, Move, Rename, and Delete actions for installed models. The picker can create a destination folder. Operations run with the Linux permissions of the ComfyUI process; symbolic links remain unavailable. Only enable the filesystem root on a ComfyUI instance protected by authentication or a trusted network boundary.

The optional ComfyUI settings export includes only a small allowlist of UI preferences. It does not export arbitrary settings or credentials. Temporary export/import ZIPs are removed after the configured retention period (default: 24 hours).
Signed direct URLs can be used for a download, but are not saved as reusable model sources. Add a public source URL separately if one is available.

Backup schema 2 adds optional custom node code bundles; schema 1 backups remain importable. Bundled folders omit model binaries, caches, Git metadata, and common credential files. Review a ZIP before sharing it because arbitrary plugin files can still contain private data. Restore never overwrites a custom node folder already present. Supported package requirements can be installed from the backup before node files are restored, or from an already present folder, only after explicit confirmation. Pip options, nested requirements files, and direct package URLs are not accepted; install those manually when needed.

## Verify

Run the focused suite with the Python environment used by ComfyUI:

```text
python -m unittest discover -s tests -v
```
