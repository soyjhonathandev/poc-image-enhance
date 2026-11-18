#!/usr/bin/env python3
"""Local web UI for the enhance POC: python3 server.py, then open http://127.0.0.1:8787"""
import base64
import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import enhance

PORT = int(os.environ.get("PORT") or 8787)
MAX_BODY = 40 * 1024 * 1024
STATUS_TEXT = {
    "Queued": "En cola…",
    "Submitting": "Enviando la foto a BFL…",
    "Pending": "Esperando turno en BFL…",
    "Reasoning": "Analizando el producto…",
    "Generating": "Generando imagen profesional…",
    "Downloading": "Descargando el resultado…",
}

jobs = {}
jobs_lock = threading.Lock()


def _update(job_id, **fields):
    with jobs_lock:
        jobs[job_id].update(fields)


def _run(job_id, raw):
    def on_status(state):
        _update(job_id, provider_status=state, message=STATUS_TEXT.get(state, state))

    try:
        out = enhance.enhance(raw, job_id=job_id, on_status=on_status)
        _update(
            job_id,
            status="ready",
            message="Lista",
            original_url=f"/results/{out['original'].name}",
            enhanced_url=f"/results/{out['enhanced'].name}",
            cost=out["cost"],
            duration_s=out["duration_s"],
            expanded_prompt=out["expanded_prompt"],
        )
    except enhance.EnhanceError as err:
        _update(job_id, status="failed", error_code=err.code, message=str(err))
    except Exception as err:  # keep the UI informed instead of leaving the job hanging
        _update(job_id, status="failed", error_code="internal", message=f"Error interno: {err}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body, content_type="application/json"):
        data = json.dumps(body, ensure_ascii=False).encode() if content_type == "application/json" else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(200, (enhance.ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/config":
            return self._send(200, {
                "has_key": bool(enhance.API_KEY),
                "endpoint": enhance.ENDPOINT,
                "resolution": enhance.RESOLUTION,
                "aspect_ratio": enhance.ASPECT_RATIO,
                "timeout_s": enhance.TIMEOUT_SECONDS,
            })
        if self.path.startswith("/api/jobs/"):
            with jobs_lock:
                job = dict(jobs.get(self.path.rsplit("/", 1)[-1]) or {})
            return self._send(200, job) if job else self._send(404, {"message": "Trabajo no encontrado"})
        if self.path.startswith("/results/"):
            name = Path(self.path).name
            file = enhance.RESULTS / name
            if name and file.is_file() and file.parent == enhance.RESULTS:
                types = {".png": "image/png", ".webp": "image/webp"}
                return self._send(200, file.read_bytes(), types.get(file.suffix, "image/jpeg"))
        self._send(404, {"message": "No encontrado"})

    def do_POST(self):
        if self.path != "/api/enhance":
            return self._send(404, {"message": "No encontrado"})
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            return self._send(413, {"message": "La imagen pesa demasiado (máx. ~30 MB)."})
        try:
            image = json.loads(self.rfile.read(length))["image"]
            raw = base64.b64decode(image.split(",", 1)[-1], validate=True)
        except (ValueError, KeyError, TypeError):
            return self._send(400, {"message": "Cuerpo inválido: se esperaba {\"image\": <base64>}."})
        job_id = uuid.uuid4().hex[:12]
        with jobs_lock:
            jobs[job_id] = {"id": job_id, "status": "processing", "message": STATUS_TEXT["Queued"]}
        threading.Thread(target=_run, args=(job_id, raw), daemon=True).start()
        self._send(202, {"id": job_id})


if __name__ == "__main__":
    if not enhance.API_KEY:
        print("Aviso: BFL_API_KEY no está definida; la UI abrirá pero no podrá generar.")
    print(f"POC en http://127.0.0.1:{PORT}  ·  endpoint {enhance.ENDPOINT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
