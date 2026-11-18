#!/usr/bin/env python3
"""Enhance a product photo with FLUX 3 Image Klein (BFL API).

Usage: python3 enhance.py <image> [<image> ...]
Standard library only; image prep uses macOS `sips`.
"""
import base64
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "results"


def _load_env_file(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_env_file(ROOT / ".env")

API_KEY = os.environ.get("BFL_API_KEY", "")
ENDPOINT = os.environ.get("BFL_ENDPOINT") or "https://api.bfl.ai/v1/flux-3-image-klein"
RESOLUTION = os.environ.get("IMAGE_ENHANCE_RESOLUTION") or "1k"
ASPECT_RATIO = os.environ.get("IMAGE_ENHANCE_ASPECT_RATIO") or "1:1"
TIMEOUT_SECONDS = float(os.environ.get("IMAGE_ENHANCE_TIMEOUT_SECONDS") or 120)

PROMPT = (
    "Convert this into a professional e-commerce product photograph. Keep the product EXACTLY as it is: "
    "same shape, same colors, same materials, same logos, labels, text and proportions. Do not add, remove "
    "or redesign any part of the product. Place the product centered on a clean pure white seamless studio "
    "background, with soft even studio lighting, a subtle natural contact shadow under the product, sharp "
    "focus, high resolution, no clutter, no hands, no other objects, no text overlays."
)

MIN_SIDE = 256
MAX_PIXELS = 16_000_000
POLL_INTERVAL = 1.5
MAX_ATTEMPTS = 3
IN_PROGRESS = {"Pending", "Reasoning", "Generating"}
FINAL_ERRORS = {
    "Request Moderated": ("request_moderated", "BFL moderó la solicitud. Prueba con otra foto; no se reintenta."),
    "Content Moderated": ("content_moderated", "BFL moderó la imagen generada. Prueba con otra foto o regenera."),
    "Error": ("provider_error", "BFL tuvo un error al generar la imagen. Intenta de nuevo."),
    "Task not found": ("task_not_found", "BFL no encontró la tarea (pudo expirar). Intenta de nuevo."),
}


class EnhanceError(Exception):
    def __init__(self, message, code="error"):
        super().__init__(message)
        self.code = code


def _image_size(path):
    out = subprocess.run(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)], capture_output=True, text=True
    )
    size = {}
    for line in out.stdout.splitlines():
        key, _, value = line.strip().partition(":")
        if key in ("pixelWidth", "pixelHeight"):
            size[key] = int(value)
    if len(size) != 2:
        raise EnhanceError("No pude leer la imagen. Usa JPG, PNG, WEBP o HEIC.", "invalid_image")
    return size["pixelWidth"], size["pixelHeight"]


def prepare_image(raw):
    """Validate size, shrink to <= 16 MP and convert to JPEG. Returns (jpeg_bytes, original_size)."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "input"
        src.write_bytes(raw)
        width, height = _image_size(src)
        if min(width, height) < MIN_SIDE:
            raise EnhanceError(
                f"La imagen mide {width}x{height}; el mínimo es {MIN_SIDE}x{MIN_SIDE}.", "too_small"
            )
        out = Path(tmp) / "prepared.jpg"
        cmd = ["sips", "-s", "format", "jpeg", "-s", "formatOptions", "90"]
        if width * height > MAX_PIXELS:
            scale = math.sqrt(MAX_PIXELS / (width * height))
            cmd += ["-Z", str(int(max(width, height) * scale))]
        result = subprocess.run(cmd + [str(src), "--out", str(out)], capture_output=True, text=True)
        if result.returncode != 0 or not out.exists():
            raise EnhanceError("No pude convertir la imagen a JPEG.", "invalid_image")
        return out.read_bytes(), (width, height)


def _request(method, url, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"accept": "application/json", "x-key": API_KEY}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        raw = err.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw.decode(errors="replace")[:500]}
        return err.code, payload
    except (urllib.error.URLError, TimeoutError) as err:
        raise EnhanceError(f"No pude conectar con BFL: {err}", "network") from err


def _detail(payload):
    if isinstance(payload, dict):
        detail = payload.get("detail", payload)
        if isinstance(detail, list):
            return "; ".join(f"{'.'.join(map(str, d.get('loc', [])))}: {d.get('msg')}" for d in detail)
        return str(detail)[:300]
    return str(payload)[:300]


def _http_error(status, payload):
    if status == 402:
        return EnhanceError("La cuenta de BFL no tiene créditos. Recarga en dashboard.bfl.ai.", "no_credits")
    if status in (401, 403):
        return EnhanceError("BFL rechazó la API key. Revisa BFL_API_KEY.", "unauthorized")
    if status == 429:
        return EnhanceError("BFL limitó las solicitudes (429) tras 3 intentos. Espera un minuto.", "rate_limited")
    if status == 400:
        return EnhanceError(f"BFL rechazó la imagen (400), probablemente > 16 MP: {_detail(payload)}", "too_large")
    if status == 422:
        return EnhanceError(f"BFL rechazó la solicitud (422): {_detail(payload)}", "invalid_request")
    if status == 503 and isinstance(payload, dict) and payload.get("status"):
        return EnhanceError(f"BFL respondió 503 con estado «{payload['status']}».", "unavailable")
    return EnhanceError(f"BFL respondió HTTP {status}: {_detail(payload)}", "provider_error")


def _submit(image_b64):
    body = {"prompt": PROMPT, "images": [image_b64], "resolution": RESOLUTION, "aspect_ratio": ASPECT_RATIO}
    for attempt in range(MAX_ATTEMPTS):
        status, payload = _request("POST", ENDPOINT, body)
        if status == 200:
            return payload
        if status == 429 and attempt < MAX_ATTEMPTS - 1:
            time.sleep(2 ** (attempt + 1))
            continue
        raise _http_error(status, payload)


def _poll(polling_url, deadline, on_status):
    while True:
        if time.monotonic() > deadline:
            raise EnhanceError(f"Se agotó el tiempo ({TIMEOUT_SECONDS:.0f} s) esperando a BFL.", "timeout")
        status, payload = _request("GET", polling_url, timeout=30)
        state = payload.get("status") if isinstance(payload, dict) else None
        if status == 429 or (state in IN_PROGRESS and status in (200, 503)):
            on_status(state or "Pending")
            time.sleep(POLL_INTERVAL)
            continue
        if status == 200 and state == "Ready":
            return payload
        if state in FINAL_ERRORS:
            code, message = FINAL_ERRORS[state]
            raise EnhanceError(message, code)
        if status == 200:
            raise EnhanceError(f"BFL devolvió un estado desconocido: «{state}».", "provider_error")
        raise _http_error(status, payload)


def _download(url, dest_stem):
    # Signed URL that expires in 1 h; it must not receive the x-key header.
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=60) as resp:
            content_type = resp.headers.get_content_type()
            data = resp.read()
    except (urllib.error.URLError, TimeoutError) as err:
        raise EnhanceError(f"No pude descargar el resultado de BFL: {err}", "download_failed") from err
    ext = {"image/png": ".png", "image/webp": ".webp"}.get(content_type, ".jpg")
    dest = dest_stem.with_suffix(ext)
    dest.write_bytes(data)
    return dest


def _log(entry):
    RESULTS.mkdir(exist_ok=True)
    with (RESULTS / "log.jsonl").open("a") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    print(json.dumps({k: v for k, v in entry.items() if k != "expanded_prompt"}, ensure_ascii=False), flush=True)


def enhance(raw, job_id=None, on_status=lambda state: None):
    """Run the full flow for one image and return a dict with the saved paths, cost and timing."""
    job_id = job_id or uuid.uuid4().hex[:12]
    started = time.monotonic()
    entry = {"job_id": job_id, "task_id": None, "status": "failed", "cost": None}
    try:
        if not API_KEY:
            raise EnhanceError("Falta BFL_API_KEY. Cópiala en el archivo .env (ver .env.example).", "missing_key")
        jpeg, original_size = prepare_image(raw)
        RESULTS.mkdir(exist_ok=True)
        original_path = RESULTS / f"{job_id}-original.jpg"
        original_path.write_bytes(jpeg)

        on_status("Submitting")
        task = _submit(base64.b64encode(jpeg).decode())
        entry.update(task_id=task.get("id"), cost=task.get("cost"),
                     input_mp=task.get("input_mp"), output_mp=task.get("output_mp"))

        done = _poll(task["polling_url"], started + TIMEOUT_SECONDS, on_status)
        result = done.get("result") or {}
        if not result.get("sample"):
            raise EnhanceError("BFL marcó la tarea como lista pero sin imagen.", "provider_error")
        entry["expanded_prompt"] = result.get("prompt")

        on_status("Downloading")
        enhanced_path = _download(result["sample"], RESULTS / f"{job_id}-enhanced")
        entry["status"] = "ready"
        return {
            "job_id": job_id,
            "original": original_path,
            "enhanced": enhanced_path,
            "original_size": original_size,
            "cost": entry["cost"],
            "task_id": entry["task_id"],
            "expanded_prompt": entry["expanded_prompt"],
            "duration_s": round(time.monotonic() - started, 1),
        }
    except EnhanceError as err:
        entry.update(error_code=err.code, error=str(err))
        raise
    finally:
        entry["duration_s"] = round(time.monotonic() - started, 1)
        _log(entry)


def main(paths):
    if not paths:
        print(__doc__.strip())
        return 2
    failures = 0
    for path in paths:
        print(f"\n→ {path}")
        try:
            out = enhance(Path(path).read_bytes(), on_status=lambda s: print(f"  {s}…", flush=True))
            print(f"  original: {out['original']}\n  mejorada: {out['enhanced']}")
            print(f"  {out['duration_s']} s · costo {out['cost']} créditos · task {out['task_id']}")
        except (EnhanceError, OSError) as err:
            failures += 1
            print(f"  ✗ {err}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
