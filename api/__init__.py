"""
api/process_trigger/__init__.py
Azure FunctionHTTP trigger that runs the processor pipeline server-side.

Deploy this alongside your Azure Static Web App so the connection string
stays in Application Settings (environment variables) and never touches
the browser.

Endpoint:  POST /api/process_trigger
Body (JSON):
  {
    "container_path": "drivers/ASUS/BIOS/asus_bios_v310.zip",
    "container_name": "updates"          // optional, default from env
  }

Required Application Settings (Azure Portal → Configuration):
  AZURE_STORAGE_CONNECTION_STRING  your storage account connection string
  BLOB_CONTAINER_NAME              default container (overridable per request)

Response (JSON):
  {
    "status":        "ok" | "error",
    "message":       str,
    "zipmanifest":   { ... },   // only on success
    "metadirs":      { ... },   // only on success
    "out_blob_path": str        // path of processed ZIP in container
  }
"""

import json
import logging
import os
import sys
import tempfile

import azure.functions as func

# Bring processor.py into scope.  In a real deployment, package processor.py
# as a shared module; here we assume it lives one directory up from api/.
_HERE   = os.path.dirname(__file__)
_ROOT   = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    import processor as proc
    PROC_AVAILABLE = True
except ImportError as e:
    PROC_AVAILABLE = False
    _IMPORT_ERROR  = str(e)


def main(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("process_trigger: received request")

    # ── Guard: processor module must be importable ────────────────────────────
    if not PROC_AVAILABLE:
        return _err(500, f"processor.py could not be imported: {_IMPORT_ERROR}")

    # ── Parse request body ────────────────────────────────────────────────────
    try:
        body = req.get_json()
    except ValueError:
        return _err(400, "Request body must be valid JSON.")

    container_path = body.get("container_path", "").strip()
    if not container_path:
        return _err(400, "Missing required field: container_path")

    # ── Resolve config from env ───────────────────────────────────────────────
    conn_str       = os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
    container_name = body.get("container_name") or os.environ.get("BLOB_CONTAINER_NAME", "updates")

    if not conn_str:
        return _err(500, "AZURE_STORAGE_CONNECTION_STRING not set in Application Settings.")

    # ── Download the source blob to a temp file ───────────────────────────────
    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError:
        return _err(500, "azure-storage-blob is not installed in the Function runtime.")

    work_dir = tempfile.mkdtemp(prefix="fn_updpkg_")
    zip_name = container_path.split("/")[-1]
    local_zip = os.path.join(work_dir, zip_name)

    logging.info(f"process_trigger: downloading blob {container_name}/{container_path}")

    try:
        svc     = BlobServiceClient.from_connection_string(conn_str)
        blob_cl = svc.get_blob_client(container=container_name, blob=container_path)
        with open(local_zip, "wb") as fh:
            stream = blob_cl.download_blob()
            stream.readinto(fh)
    except Exception as e:
        return _err(500, f"Failed to download blob: {e}")

    # ── Run the processor pipeline ────────────────────────────────────────────
    logging.info("process_trigger: running pipeline")

    try:
        result = proc.process(
            zip_path=local_zip,
            container_path=container_path,
            connection_string=conn_str,
            azure_container_name=container_name,
            work_dir=work_dir,
        )
    except Exception as e:
        logging.exception("process_trigger: pipeline error")
        return _err(500, f"Pipeline error: {e}")

    # ── Build response ────────────────────────────────────────────────────────
    zm   = result["zipmanifest"]
    md   = result["metadirs"]
    segs = container_path.split("/")
    out_blob = "/".join(segs[:-1] + ["out", zm["output_zip"]])

    payload = {
        "status":        "ok",
        "message":       f"Processed successfully. Output at {container_name}/{out_blob}",
        "out_blob_path": out_blob,
        "zipmanifest":   zm,
        "metadirs":      md,
    }

    logging.info(f"process_trigger: done{out_blob}")
    return func.HttpResponse(
        body=json.dumps(payload, indent=2),
        status_code=200,
        mimetype="application/json",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _err(status: int, msg: str) -> func.HttpResponse:
    logging.error(f"process_trigger [{status}]: {msg}")
    return func.HttpResponse(
        body=json.dumps({"status": "error", "message": msg}),
        status_code=status,
        mimetype="application/json",
    )
