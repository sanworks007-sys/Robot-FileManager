import asyncio
import json

from aiohttp import ClientError

from . import catalog, providers, transfers
from .security import download_url, public_source_url, resolve
from .tasks import TASKS, Stopped, error_text


def identify(items, online=False, calculate_hash=False):
    if not isinstance(items, list) or not 1 <= len(items) <= 1000:
        raise ValueError("Select 1 to 1000 models")
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("Invalid model selection")
        path = resolve(item["root"], item["path"], must_exist=True)
        if not path.is_file() or path.suffix.lower() not in catalog.MODEL_EXTENSIONS:
            raise ValueError("Select model files")

    def run(task):
        report = {"identified": 0, "verified_sources": 0, "unknown_sources": 0, "errors": [], "items": []}
        history = transfers.history()
        for index, item in enumerate(items):
            task.check()
            task.result = {"stage": "Identifying " + item["path"], "identification": report}
            try:
                path = resolve(item["root"], item["path"], must_exist=True)
                info = catalog.details(path)
                sha = None
                if calculate_hash:
                    sha = catalog.hash_model(item["root"], item["path"], task=task)["sha256"]
                elif info["hash_current"]:
                    with catalog.database() as connection:
                        row = connection.execute("SELECT sha256 FROM models WHERE path=?", (str(path),)).fetchone()
                        sha = row[0] if row else None
                candidates = list(info["candidates"])
                for transfer in history:
                    if transfer["name"].lower() == path.name.lower() and transfer["url"] and transfer["reusable"]:
                        candidates.append({"platform": transfer["platform"], "url": transfer["url"], "confidence": "Saved download history"})
                if online:
                    candidates.extend(asyncio.run(providers.find_sources(path.name, sha)))
                verified = [candidate for candidate in candidates if candidate["confidence"] == "Exact Hash Match"]
                if verified:
                    candidate = verified[0]
                    automatic = {**info["automatic"], **candidate.get("details", {}), "confidence": "Hash verified", "evidence": "Provider file hash matches the local file"}
                    if automatic.get("type") == "other" and info["automatic"].get("type") != "other":
                        automatic["type"] = info["automatic"]["type"]
                    catalog.save_model_source(path, candidate["platform"], candidate["url"], {"sha256": sha, "model_type": automatic.get("type"), "model_name": automatic.get("name")})
                    report["verified_sources"] += 1
                else:
                    automatic = info["automatic"]
                unique = {}
                for candidate in candidates:
                    try:
                        candidate["url"] = download_url(candidate["url"])
                        public_source_url(candidate["url"])
                        unique.setdefault((candidate["platform"], candidate["url"]), candidate)
                    except ValueError:
                        continue
                with catalog.database() as connection:
                    connection.execute("UPDATE model_details SET automatic=?, candidates=? WHERE path=?",
                        (json.dumps(automatic), json.dumps(list(unique.values())[:50]), str(path)))
                info = catalog.details(path)
                report["identified"] += info.get("type") != "other"
                report["unknown_sources"] += not bool(candidates)
                report["items"].append({"name": path.name, "type": info.get("type"), "sources": len(unique), "verified": bool(verified)})
            except Stopped:
                raise
            except (OSError, ValueError, ClientError, asyncio.TimeoutError) as error:
                report["errors"].append({"name": item["path"], "error": error_text(error)})
            task.result = {"stage": "Identifying models", "identification": report}
            task.progress(index + 1, len(items))
        return {"stage": "Identification finished", "identification": report, "needs_attention": bool(report["errors"])}
    return TASKS.add("identify", "Identify " + str(len(items)) + " model(s)", run)
