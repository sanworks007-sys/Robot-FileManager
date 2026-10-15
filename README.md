# Robot File Manager

A browser based file, download, model, workflow, and workspace migration manager for ComfyUI. It uses ComfyUI's existing `aiohttp` dependency and runs on Windows and headless Linux servers.

## Install

Place this directory at `ComfyUI/custom_nodes/Robot-FileManager`, restart ComfyUI, then click **Files** in the left sidebar, directly below Templates. The panel includes Browser, Downloads, Node Installer, Backup & Restore, Storage Cleanup, and the file/model/workflow tools.

On the first frontend load, the plugin installs the GitHub/custom-node edition of ComfyUI-Manager when absent. Progress appears in Tasks; restart ComfyUI after installation. Disable this in Settings, or retry from Node Installer after a failure. Setup attempts installation once instead of repeating network requests on every reload.

Protect remote ComfyUI access with authentication or a trusted network boundary. This extension exposes file modification and server download routes to anyone who can access the ComfyUI API.

## Move to another computer

1. In **Backup & Restore**, click **Export My Setup**. The completed ZIP downloads automatically and remains available in Tasks. Models are recorded as filenames, folders, available hashes and known public source links. Unpublished plugins, single-file nodes and modified repositories are bundled; clean GitHub repositories use their URL and saved commit. Model binaries, caches and common credential files are excluded.
2. Install ComfyUI and Robot File Manager on the destination computer. Choose the ZIP and click **Analyze archive**. Review files, existing items, plugins, model sources and destination folders; use **Select All**, category buttons or individual selections.
3. Click **Import All Selected**. One task restores selected files/preferences, installs selected GitHub/bundled plugins and their requirements, and downloads selected models. Existing plugin folders are preserved. Workflow/output replacement is an explicit option. Review the report for missing sources, unavailable revisions, conflicting files or dependency failures. Failed imports retain their report and offer retry; completed items are skipped where possible. Restart ComfyUI after installation.

**Export Setup + All Outputs** includes the output folder. **Export All Outputs** creates a separate importable output backup. Images/videos are stored without recompression; these archives can remain large. Advanced export options retain size analysis and output filters.

Model-folder paths and Python/package/ComfyUI versions are recorded for reference. Select target model folders during import: machine-specific paths, the Python environment and GPU packages are not blindly copied. Only supported UI preferences are restored, including preferences for additional user profiles. Unknown model sources require a URL or manual file selection. Configure tokens again on the destination computer.

## Browser and authorization

Browser provides Civitai/Hugging Face model search, previews where supplied, file/version selection, an HTTPS address bar, bookmarks and a website frame. Back/forward follows addresses opened through the toolbar. Sites may block frames, embedded sign-in or third-party cookies; **Open / log in in browser** opens their normal website. Website cookies are separate from server download credentials. This extension does not include a desktop browser engine or bypass site embedding restrictions.

Settings stores Hugging Face/Civitai tokens and provides access tests. Hugging Face's test checks the authenticated account; Civitai's test checks API reachability, with restricted-file access checked during download. Authentication does not guarantee faster downloads.

Custom authorization profiles support Bearer tokens, Basic authentication and a custom header. Select a profile in Downloads or the model import preview. Each profile applies only to its exact HTTPS host; authorization headers are removed on redirects to another host. Secrets stay in the private credential store and are excluded from backups. Provider tokens apply automatically to their respective downloads.

## Model identification and LoRA information

Model Library scans supported Safetensors/SFT headers without loading model weights. It recognizes known LoRA/LyCORIS, combined checkpoint, diffusion network, VAE, ControlNet, text encoder, embedding and other supported tensor layouts. Unsupported formats and ambiguous layouts remain unknown or show a folder/provider estimate. Filename extensions alone do not establish a model's type.

Select models and use **Identify selected** or **Identify all visible**. Enable **Calculate SHA256** for a full-file hash, and **Find sources online** to query Civitai/Hugging Face using the filename/hash. Progress and cancellation are in Tasks. Header information and hashes are cached; changing the file invalidates its hash and detected source associations. Model files are never uploaded for identification.

Source candidates distinguish provider hash matches, locally saved source associations, filename matches and possible matches. Only provider hash matches are accepted automatically. Filename searches cannot guarantee identity, and deleted/private/unindexed models can have no recoverable public source. Civitai/Hugging Face lookups depend on provider access and API availability.

**Info / edit** shows the model name/version, type, base model, source links and, when supplied by the file/provider, LoRA trigger words and recommended strength. Missing values remain empty. Correct these fields and add notes manually; **Reset manual corrections** returns to detected metadata. Corrections survive rescans, individual model rename/copy/move operations in the file manager and workspace export/import. New Civitai downloads retain available per-version metadata.

Downloads has **↻ Refresh** beside its title. It updates queue progress and storage/resource figures while preserving the source URL, selected files, filenames and destination.

## Self-update

Settings shows the running Robot File Manager version, update status, **Check for Updates**, **Update Now** and recovery controls. Updates come from `sanworks007-sys/Robot-FileManager` on GitHub. Automatic checks run at most once every six hours after startup; automatic installation is a separate setting and is off by default. When enabled, it waits for workflows, downloads, terminal commands and file tasks to finish. ComfyUI is not restarted automatically.

Updates are downloaded into private staging, checked for safe archive paths and Python syntax, and backed up before installation. Git checkouts update only by fast-forward; divergent history or local edits block the update. Archive installations record a baseline on first use and protect later modifications. A recovery copy supports rollback, and failed archive writes restore previous files. An update adding `requirements.txt` requires manual dependency review. Restart ComfyUI and refresh its browser page to activate an installed update.

Updates must first be published to the configured GitHub repository. The updater does not publish local changes, and protects uncommitted development work from replacement. Models, workflows, outputs and private credentials are stored outside the plugin update directory.

## GitHub installation and storage cleanup

Paste a repository URL in **Node Installer → Install from GitHub**. When Git is available, the installer preserves a checkout and its default branch so Manager's update features can work. Saved revisions restore at the recorded commit. Without Git, it installs a GitHub archive; Git-based updates still require Git. Existing node folders are protected. Repositories needing submodules or extra setup report manual steps. Requirements run with ComfyUI's Python, followed by a dependency check.

**Storage Cleanup** previews File Manager Trash, temporary import/export ZIPs, inactive partial downloads, node uploads/staging older than 24 hours and, on Windows, the server account's Recycle Bin. Select categories and confirm permanent removal. Active/paused downloads are protected; cleanup refuses to run alongside active file tasks. Failures and observed disk free-space changes appear in Tasks. The Recycle Bin option empties the server account's entire Windows Recycle Bin.

Permanent deletion bypasses Trash and verifies that the path disappeared from disk. Files held open by another process, other copies, hard links or filesystem snapshots can retain disk space. Cleanup reports failures instead of claiming that all logical file bytes were reclaimed.

## Main flows

- **Files:** browse an Explorer-style folder pane and file details, upload files or ZIP archives from your PC into the current folder using the upload button or drag and drop, download, copy, move, rename, duplicate, create folders or text files, ZIP selected files, and extract ZIPs into a chosen folder. Deletion always offers Trash or permanent deletion. Root, path, and symlink checks run on the server for every file operation.
- **Downloads:** inspect a direct HTTPS URL, Hugging Face repository or file URL, or Civitai model/version URL. Select exact files and a destination root/folder before queueing. The folder starts empty, which saves directly in the chosen root. Existing destination files and already queued downloads are skipped. Downloads stream to `.robot-part` files on the server and resume when the same source is queued again. Pause, resume, cancel, retry, progress, speed, and task history are in the panel. Provider SHA256 hashes are verified when available.
- **History:** download and upload records retain filenames, sizes, destinations, dates, status, and public source links independently of the task queue. Download saved files again or select a destination to re-download a public source. Signed links are redacted and require a fresh URL.
- **Server resources:** disk used/free/total, CPU usage, RAM usage, and total installed model size remain visible in every tab. Disk and memory refresh every two seconds while the panel is open; model totals refresh after file changes and every ten seconds.
- **Terminal (Linux):** choose a server working folder and run shell commands with streaming output, exit status, and Stop. Commands run with ComfyUI's OS permissions, including access beyond the file browser's approved roots. Stop signals the command's process group and escalates after three seconds. The terminal supports up to three concurrent commands, retains the last 30 in memory, and keeps the last million output characters per command. Commands start in the selected folder each time; interactive input and persistent shell sessions are unavailable. Protect ComfyUI access with authentication before exposing the terminal remotely.
- **Node Installer:** upload a ZIP, a folder, or a single `.py` node from your PC. Inspect the destination and `requirements.txt`, then install into `custom_nodes` without overwriting existing nodes. ZIP paths, links, and Python syntax are checked first. The optional requirements step runs `pip install -r requirements.txt` with ComfyUI's Python, then `pip check`; its output and any environment conflicts appear in the installer. Successful installs can restart ComfyUI automatically when the dependency check passes. Restart refuses active workflows, transfers, and terminal commands, then reconnects the browser. Installation history checks on-disk files and registered node classes after startup. Only install trusted node code and requirements. Pip environment changes cannot be rolled back automatically if installation fails.
- **Models and workflows:** scan ComfyUI model folders, including unregistered folders under `models`, calculate hashes on demand, save public source links, analyze workflow JSON, and look up source candidates. Possible matches require user selection.
- **Backup & Restore:** use setup/output presets or advanced export selections. Import compares dependencies and lists individual files, plugins and models before one batch restore/install/download action. Existing plugins are preserved; unknown sources and unsupported setup steps appear in the report.
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

The optional ComfyUI settings export includes supported UI preferences. It excludes arbitrary extension settings and credentials. Temporary export/import ZIPs are removed after the configured retention period (default: 24 hours); active backup/import jobs postpone expiration cleanup.
Signed direct URLs can be used for a download, but are not saved as reusable model sources. Add a public source URL separately if one is available.

Backup schema 3 adds environment information and single-file nodes; schemas 1 and 2 remain importable. Bundled folders omit model binaries, caches, Git metadata and common credential files. Review a ZIP before sharing it because arbitrary plugin files can contain private data. Restore never overwrites an existing plugin. The batch import confirmation covers selected plugin code and requirements installation, which can change the Python environment. The older requirements-only restore endpoint accepts simple package specifications; the migration installer uses each selected plugin's requirements file.

## Verify

Run the focused suite with the Python environment used by ComfyUI:

```text
python -m unittest discover -s tests -v
```
