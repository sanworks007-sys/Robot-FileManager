import asyncio
import base64
import binascii
import json
import os
import re
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit

import aiohttp

from .catalog import database
from .security import DATA, download_url, public_url, read_json, safe_url, write_json
from .tasks import PublicResolver


CREDENTIALS = DATA / "credentials.json"
AUTH_PROFILES = DATA / "authorization.json"


def _windows_crypt(value, protect):
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    source_buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    result = Blob()
    function = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not function(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(result)):
        raise OSError(f"Windows credential protection failed ({ctypes.windll.kernel32.GetLastError()})")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


def credential(platform):
    saved = read_json(CREDENTIALS, {}).get(platform)
    if isinstance(saved, dict) and "dpapi" in saved:
        try:
            return _windows_crypt(base64.b64decode(saved["dpapi"]), False).decode("utf-8")
        except (OSError, ValueError, binascii.Error):
            return None
    return saved


def set_credential(platform, token):
    if (platform not in ("huggingface", "civitai") and not re.fullmatch(r"custom:[A-Za-z0-9_-]{1,40}", platform)) or not isinstance(token, str) or len(token) > 8192:
        raise ValueError("Invalid credential")
    saved = read_json(CREDENTIALS, {})
    if token.strip():
        value = token.strip()
        if os.name == "nt":
            try:
                saved[platform] = {"dpapi": base64.b64encode(_windows_crypt(value.encode("utf-8"), True)).decode("ascii")}
            except OSError:
                saved[platform] = value
        else:
            saved[platform] = value
    else:
        saved.pop(platform, None)
    write_json(CREDENTIALS, saved, private=True)


def auth_profiles():
    return read_json(AUTH_PROFILES, {})


def set_auth_profile(name, host, method, secret, username="", header="Authorization"):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
        raise ValueError("Profile name must contain letters, numbers, underscores or hyphens")
    profiles = auth_profiles()
    if method == "delete":
        profiles.pop(name, None)
        set_credential("custom:" + name, "")
    else:
        safe_url("https://" + host + "/")
        if urlsplit("https://" + host).netloc != host or "/" in host or host.endswith("."):
            raise ValueError("Enter an exact HTTPS host, such as downloads.example.com")
        if method not in ("bearer", "basic", "header") or not isinstance(secret, str) or not secret or len(secret) > 4096 or any(c in secret for c in "\r\n\x00"):
            raise ValueError("Choose an authorization method and enter its secret")
        if not isinstance(username, str) or ":" in username or any(c in username for c in "\r\n\x00"):
            raise ValueError("Invalid Basic authentication username")
        if not isinstance(header, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,79}", header) or header.lower() in ("host", "range", "content-length", "connection", "transfer-encoding", "cookie", "accept-encoding"):
            raise ValueError("Choose an authorization header, such as X-API-Key")
        set_credential("custom:" + name, json.dumps({"secret": secret, "username": username}))
        profiles[name] = {"name": name, "host": host.lower(), "method": method, "header": header}
    write_json(AUTH_PROFILES, profiles, private=True)


def authorization_headers(url, name):
    if not name:
        return {}
    profile = auth_profiles().get(name)
    if not profile or urlsplit(url).netloc.lower() != profile["host"] or urlsplit(url).scheme != "https":
        raise ValueError("Authorization profile does not match this download host")
    saved = credential("custom:" + name)
    if not saved:
        raise ValueError("Authorization profile has no saved secret")
    value = json.loads(saved)
    if profile["method"] == "basic":
        encoded = base64.b64encode((value["username"] + ":" + value["secret"]).encode()).decode("ascii")
        return {"Authorization": "Basic " + encoded}
    if profile["method"] == "bearer":
        return {"Authorization": "Bearer " + value["secret"]}
    return {profile["header"]: value["secret"]}


async def test_access(platform):
    if platform == "huggingface":
        if not credential(platform):
            raise ValueError("Save a Hugging Face token first")
        result = await request_json("https://huggingface.co/api/whoami-v2", platform)
        return {"message": "Authenticated as " + str(result.get("name", "Hugging Face user"))}
    if platform == "civitai":
        await request_json("https://civitai.com/api/v1/models?limit=1", platform)
        return {"message": "Civitai API is reachable. Access to restricted files is checked when downloading."}
    raise ValueError("Unknown provider")


async def browse_models(platform, query="", page=1):
    if not isinstance(query, str) or len(query) > 200 or not isinstance(page, int) or not 1 <= page <= 5:
        raise ValueError("Use a search of up to 200 characters and pages 1 to 5")
    if platform == "civitai":
        data = await request_json("https://civitai.com/api/v1/models?" + urlencode({"limit": 20, "page": page, "query": query, "nsfw": "false"}), platform)
        items = []
        for model in data.get("items", []):
            versions = model.get("modelVersions", [])
            images = versions[0].get("images", []) if versions else []
            image = images[0].get("url") if images else None
            items.append({"name": str(model.get("name", "Model")), "type": str(model.get("type", "")),
                          "url": "https://civitai.com/models/" + str(int(model["id"])), "image": image})
        return {"items": items, "more": bool(data.get("metadata", {}).get("nextPage")) and page < 5}
    if platform == "huggingface":
        data = await request_json("https://huggingface.co/api/models?" + urlencode({"search": query, "limit": page * 20, "sort": "downloads", "direction": -1}), platform)
        return {"items": [{"name": item["id"], "type": item.get("pipeline_tag", "Model"),
                           "url": "https://huggingface.co/" + quote(item["id"], safe="/"), "image": None}
                          for item in data[(page - 1) * 20:page * 20]], "more": len(data) == page * 20 and page < 5}
    raise ValueError("Choose Civitai or Hugging Face")


async def request_json(url, platform=None):
    await asyncio.to_thread(public_url, url)
    token = credential(platform) if platform else None
    timeout = aiohttp.ClientTimeout(total=40, connect=15)
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), ttl_dns_cache=0)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False) as session:
        current = url
        for _ in range(5):
            await asyncio.to_thread(public_url, current)
            headers = {"Authorization": f"Bearer {token}"} if token and urlsplit(current).hostname == urlsplit(url).hostname else {}
            async with session.get(current, headers=headers, allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    current = urljoin(current, response.headers.get("Location", ""))
                    continue
                if response.status != 200:
                    raise ValueError(f"Source lookup failed with HTTP {response.status}")
                raw = await response.content.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise ValueError("Source response is too large")
                return json.loads(raw)
    raise ValueError("Too many source redirects")


def detect(value):
    if not isinstance(value, str):
        raise ValueError("Expected a source URL or ID")
    value = value.strip()
    if value.startswith("https://"):
        host = (urlsplit(value).hostname or "").lower()
        if host == "huggingface.co":
            return "huggingface"
        if host in ("civitai.com", "www.civitai.com"):
            return "civitai"
        return "direct"
    if re.fullmatch(r"[\w.-]+/[\w.-]+", value):
        return "huggingface"
    if value.isdigit():
        return "civitai"
    raise ValueError("Enter an HTTPS URL, Hugging Face repository, or Civitai ID")


def huggingface_reference(value):
    if not isinstance(value, str):
        raise ValueError("Expected a Hugging Face reference")
    value = value.strip()
    if value.startswith("https://"):
        safe_url(value, {"huggingface.co"})
        parts = [unquote(part) for part in urlsplit(value).path.strip("/").split("/")]
        if len(parts) < 2:
            raise ValueError("Invalid Hugging Face model URL")
        if parts[0] in ("datasets", "spaces"):
            raise ValueError("Choose a model repository")
        repository = "/".join(parts[:2])
        if len(parts) >= 5 and parts[2] in ("blob", "resolve"):
            return repository, parts[3], "/".join(parts[4:])
        return repository, "main", None
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", value):
        raise ValueError("Invalid Hugging Face repository")
    return value, "main", None


async def huggingface_files(value):
    repository, revision, selected = huggingface_reference(value)
    url = f"https://huggingface.co/api/models/{quote(repository, safe='/')}/tree/{quote(revision, safe='')}?recursive=true&limit=1000"
    entries = await request_json(url, "huggingface")
    files = []
    for entry in entries:
        path = entry.get("path", "")
        if entry.get("type") != "file" or not path:
            continue
        oid = entry.get("lfs", {}).get("oid") if isinstance(entry.get("lfs"), dict) else None
        if isinstance(oid, str) and oid.startswith("sha256:"):
            oid = oid[7:]
        if not isinstance(oid, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", oid):
            oid = None
        files.append({"name": PurePosixPath(path).name, "path": path, "size": entry.get("size"),
                      "url": f"https://huggingface.co/{quote(repository, safe='/')}/resolve/{quote(revision, safe='')}/{quote(path, safe='/')}",
                      "sha256": oid,
                      "selected": path == selected})
    return {"platform": "huggingface", "model_name": repository, "files": files}


def civitai_reference(value):
    if not isinstance(value, str):
        raise ValueError("Expected a Civitai reference")
    if value.isdigit():
        return "model", value
    safe_url(value, {"civitai.com", "www.civitai.com"})
    parsed = urlsplit(value)
    match = re.search(r"/models/(\d+)", parsed.path)
    version = re.search(r"(?:^|&)modelVersionId=(\d+)", parsed.query)
    if match:
        return ("version", version.group(1)) if version else ("model", match.group(1))
    match = re.search(r"/api/download/models/(\d+)", parsed.path)
    if match:
        return "version", match.group(1)
    match = re.search(r"/model-versions/(\d+)", parsed.path)
    if match:
        return "version", match.group(1)
    raise ValueError("Invalid Civitai model URL")


async def civitai_files(value):
    kind, identifier = civitai_reference(value.strip())
    endpoint = f"https://civitai.com/api/v1/{'models' if kind == 'model' else 'model-versions'}/{identifier}"
    model = await request_json(endpoint, "civitai")
    versions = model.get("modelVersions", []) if kind == "model" else [model]
    files = []
    for version in versions:
        for item in version.get("files", []):
            url = item.get("downloadUrl") or f"https://civitai.com/api/download/models/{version['id']}"
            files.append({"name": item.get("name"), "size": (item.get("sizeKB") or 0) * 1024,
                          "sha256": item.get("hashes", {}).get("SHA256"), "url": download_url(url),
                          "version": version.get("name"), "version_id": version.get("id"),
                          "base_model": version.get("baseModel")})
    return {"platform": "civitai", "model_name": model.get("name") or model.get("model", {}).get("name"),
            "model_type": model.get("type") or model.get("model", {}).get("type"), "files": files}


async def source_info(value):
    platform = detect(value)
    if platform == "huggingface":
        return await huggingface_files(value)
    if platform == "civitai":
        return await civitai_files(value)
    await asyncio.to_thread(public_url, value)
    name = PurePosixPath(urlsplit(value).path).name
    if not name:
        raise ValueError("The URL needs a filename")
    size = None
    timeout = aiohttp.ClientTimeout(total=20, connect=10)
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), ttl_dns_cache=0)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout, trust_env=False) as session:
        current = value
        for _ in range(5):
            await asyncio.to_thread(public_url, current)
            try:
                async with session.head(current, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        current = urljoin(current, response.headers.get("Location", ""))
                        continue
                    if response.status == 200:
                        size = response.content_length
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                break
    return {"platform": "direct", "model_name": None, "files": [{"name": name, "url": value, "size": size}]}


async def find_sources(filename, sha256=None):
    if not isinstance(filename, str) or not filename.strip() or len(filename) > 500:
        raise ValueError("Invalid model filename")
    if sha256 and (not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256)):
        raise ValueError("Invalid SHA256")
    results = []
    with database() as connection:
        if sha256:
            for platform, url in connection.execute("SELECT source_platform, source_url FROM models WHERE sha256=? AND source_url IS NOT NULL", (sha256,)):
                results.append({"platform": platform, "url": url, "confidence": "Exact Hash Match"})
        for path, platform, url in connection.execute("SELECT path, source_platform, source_url FROM models WHERE source_url IS NOT NULL"):
            if path.replace("\\", "/").split("/")[-1].lower() == filename.lower() and not any(item["url"] == url for item in results):
                results.append({"platform": platform, "url": url, "confidence": "Exact Filename Match"})
    if sha256:
        try:
            version = await request_json(f"https://civitai.com/api/v1/model-versions/by-hash/{sha256}", "civitai")
            for item in version.get("files", []):
                if item.get("hashes", {}).get("SHA256", "").lower() == sha256.lower() and item.get("downloadUrl"):
                    results.append({"platform": "civitai", "url": download_url(item["downloadUrl"]), "confidence": "Exact Hash Match"})
        except (ValueError, aiohttp.ClientError, OSError):
            pass
    query = quote(PurePosixPath(filename).stem[:100])
    try:
        repositories = await request_json(f"https://huggingface.co/api/models?search={query}&limit=10", "huggingface")
        async def inspect_repo(item):
            repository = item.get("id")
            if not repository:
                return []
            try:
                info = await huggingface_files(repository)
                matches = [file for file in info["files"] if file["name"].lower() == filename.lower()]
                if matches:
                    return [{"platform": "huggingface", "url": file["url"], "confidence": "Exact Filename Match"} for file in matches]
            except (ValueError, aiohttp.ClientError, OSError):
                pass
            return [{"platform": "huggingface", "url": "https://huggingface.co/" + repository, "confidence": "Possible Match"}]
        groups = await asyncio.gather(*(inspect_repo(item) for item in repositories[:5]))
        results.extend(candidate for group in groups for candidate in group)
    except (ValueError, aiohttp.ClientError, OSError):
        pass
    try:
        data = await request_json(f"https://civitai.com/api/v1/models?query={query}&limit=10", "civitai")
        for item in data.get("items", [])[:10]:
            if not item.get("id"):
                continue
            matches = [file for version in item.get("modelVersions", []) for file in version.get("files", [])
                       if file.get("name", "").lower() == filename.lower() and file.get("downloadUrl")]
            if matches:
                results.extend({"platform": "civitai", "url": download_url(file["downloadUrl"]),
                                "confidence": "Exact Filename Match"} for file in matches)
            else:
                results.append({"platform": "civitai", "url": f"https://civitai.com/models/{item['id']}",
                                "confidence": "Possible Match"})
    except (ValueError, aiohttp.ClientError, OSError):
        pass
    priority = {"Exact Hash Match": 0, "Exact Filename Match": 1, "Possible Match": 2}
    unique = {}
    for item in results:
        key = (item["platform"], item["url"])
        if key not in unique or priority[item["confidence"]] < priority[unique[key]["confidence"]]:
            unique[key] = item
    return sorted(unique.values(), key=lambda item: (priority[item["confidence"]], item["platform"], item["url"]))
