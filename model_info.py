import json
import re
import struct


MODEL_TYPES = ("checkpoint", "lora", "diffusion_model", "vae", "controlnet", "text_encoder", "clip", "embedding", "upscaler", "ipadapter", "unet", "other")


def normalize_type(value):
    value = str(value or "").lower().replace(" ", "_")
    return {"locon": "lora", "lycoris": "lora", "textualinversion": "embedding", "textual_inversion": "embedding",
            "poses": "other", "upscaler": "upscaler", "diffusionmodel": "diffusion_model"}.get(value, value if value in MODEL_TYPES else "other")


def clean_details(value):
    if not isinstance(value, dict):
        raise ValueError("Model details must be an object")
    result = {}
    for key in ("name", "version", "base_model", "notes", "description", "page_url", "download_url", "confidence", "evidence"):
        if isinstance(value.get(key), str):
            result[key] = value[key][:4000 if key in ("notes", "description") else 1000]
    if "type" in value:
        result["type"] = normalize_type(value["type"])
    words = value.get("trigger_words", [])
    if isinstance(words, str):
        words = [word.strip() for word in re.split(r"[,\n]", words) if word.strip()]
    if isinstance(words, list):
        result["trigger_words"] = [word[:200] for word in words[:100] if isinstance(word, str)]
    strength = value.get("recommended_strength")
    if isinstance(strength, (str, int, float)) and not isinstance(strength, bool):
        result["recommended_strength"] = str(strength)[:100]
    return result


def inspect_file(path):
    result = {"type": "other", "confidence": "Unknown", "evidence": "No supported type signature"}
    if path.suffix.lower() not in (".safetensors", ".sft"):
        return {**result, "evidence": "Use provider metadata or a manual type for this file format"}
    try:
        with path.open("rb") as stream:
            size = stream.read(8)
            if len(size) != 8:
                raise ValueError("Incomplete Safetensors header")
            length = struct.unpack("<Q", size)[0]
            if not 2 <= length <= min(16 * 1024 * 1024, path.stat().st_size - 8):
                raise ValueError("Unsupported or incomplete Safetensors header")
            header = json.loads(stream.read(length))
        if not isinstance(header, dict):
            raise ValueError("Invalid Safetensors metadata")
        metadata = header.get("__metadata__", {})
        metadata = metadata if isinstance(metadata, dict) else {}
        keys = [key.lower() for key, value in header.items() if key != "__metadata__" and isinstance(value, dict) and isinstance(value.get("shape"), list)]
        network = str(metadata.get("ss_network_module", "")).lower()
        if network and any(word in network for word in ("lora", "lycoris")) or any(re.search(r"(?:lora_(?:down|up|a|b)|hada_w|lokr_w|\.lora_[ab])", key) for key in keys):
            kind, evidence = "lora", "LoRA/LyCORIS tensor keys or training metadata"
        elif any("first_stage_model." in key for key in keys) and any("diffusion_model." in key for key in keys):
            kind, evidence = "checkpoint", "Combined diffusion and VAE weights"
        elif any("controlnet_" in key or "control_model." in key for key in keys):
            kind, evidence = "controlnet", "ControlNet tensor keys"
        elif any("ip_adapter." in key for key in keys):
            kind, evidence = "ipadapter", "IP-Adapter tensor keys"
        elif any(key.startswith(("model.diffusion_model.", "diffusion_model.", "double_blocks.", "input_blocks.", "single_blocks.")) for key in keys):
            kind, evidence = "diffusion_model", "Diffusion network tensor keys"
        elif any(key.startswith("encoder.") for key in keys) and any(key.startswith("decoder.") for key in keys) and any("quant_conv" in key for key in keys):
            kind, evidence = "vae", "VAE encoder/decoder and quantization layers"
        elif any("text_model.encoder." in key or "text_model.embeddings." in key for key in keys):
            kind, evidence = "text_encoder", "Text encoder tensor keys"
        elif any(key in ("emb_params", "clip_l", "clip_g", "string_to_param") for key in keys) and len(keys) <= 10:
            kind, evidence = "embedding", "Embedding tensor layout"
        elif any("rrdb_trunk." in key for key in keys):
            kind, evidence = "upscaler", "RRDB upscaling tensor keys"
        else:
            kind, evidence = "other", "Type could not be established from this header"
        details = clean_details({"name": metadata.get("modelspec.title") or metadata.get("ss_output_name"),
            "base_model": metadata.get("ss_base_model_version") or metadata.get("ss_sd_model_name") or metadata.get("modelspec.architecture"),
            "description": metadata.get("modelspec.description"), "trigger_words": metadata.get("modelspec.trigger_phrase") or metadata.get("ss_trigger_words", []),
            "recommended_strength": metadata.get("recommended_strength")})
        return {**details, "type": kind, "confidence": "Header detected" if kind != "other" else "Unknown", "evidence": evidence}
    except (OSError, ValueError, UnicodeError, struct.error) as error:
        return {**result, "evidence": str(error)[:200]}
