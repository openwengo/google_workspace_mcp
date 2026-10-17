"""Serve the real workspace-mcp tools over streamable HTTP with Google faked at the HTTP layer.

Started by client.py (see README.md): ``python server.py PORT``. Only
``auth.service_decorator._authenticate_service`` is patched: tools receive a real
googleapiclient service whose transport serves synthetic payloads, stored
gzip-compressed and inflated per request as httplib2 does.

MEMLAB_KEEP_DOCSTRINGS=1 restores googleapiclient's schema docstrings, to
reproduce the pre-REHAUL behaviour.
"""

import gzip
import json
import os
import re
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
os.chdir(REPO)
os.environ.setdefault("MCP_ENABLE_OAUTH21", "false")
os.environ.setdefault("WORKSPACE_MCP_LOG_LEVEL", "WARNING")

import httplib2  # noqa: E402
import uvicorn  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

import synth  # noqa: E402

_PAYLOADS: dict = {}
_LOCK = threading.Lock()


def _compressed(key, factory):
    with _LOCK:
        if key not in _PAYLOADS:
            _PAYLOADS[key] = gzip.compress(json.dumps(factory()).encode(), 1)
        return _PAYLOADS[key]


class FakeHttp:
    """httplib2.Http stand-in routing Google API URLs to synthetic JSON.

    Docs IDs encode the payload size: ``doc-20mb`` serves ~20 MB of Docs JSON.
    """

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        path = urlparse(uri).path
        if m := re.search(r"/v1/documents/doc-([\d.]+)mb$", path):
            mb = float(m.group(1))
            payload = _compressed(("doc", mb), lambda: synth.docs_document(mb))
        elif re.search(r"/drive/v3/files/[^/]+/comments", path):
            payload = _compressed("comments", lambda: {"comments": []})
        elif m := re.search(r"/drive/v3/files/([^/]+)$", path):
            file_id = m.group(1)
            payload = _compressed(
                ("file", file_id),
                lambda: {
                    "id": file_id,
                    "name": file_id,
                    "mimeType": "application/vnd.google-apps.document",
                    "webViewLink": "https://docs.google.com/document/d/" + file_id,
                },
            )
        elif re.search(r"/v4/spreadsheets/[^/]+/values/", path):
            payload = _compressed("values", synth.sheet_values)
        elif path.endswith(":batchUpdate"):
            payload = _compressed(
                "batch", lambda: {"spreadsheetId": "s", "replies": [{}]}
            )
        elif re.search(r"/v4/spreadsheets/[^/]+$", path):
            payload = _compressed("sheet", synth.sheet_metadata)
        else:
            error = {
                "error": {"code": 404, "message": f"memlab has no route for {uri}"}
            }
            return httplib2.Response({"status": 404}), json.dumps(error).encode()
        headers = {"status": 200, "content-type": "application/json"}
        return httplib2.Response(headers), gzip.decompress(payload)

    def close(self):
        pass


async def fake_authenticate_service(
    use_oauth21,
    service_name,
    service_version,
    tool_name,
    user_google_email,
    *args,
    **kwargs,
):
    service = build(
        service_name, service_version, http=FakeHttp(), static_discovery=True
    )
    return service, user_google_email


import auth.service_decorator as service_decorator  # noqa: E402
from auth.google_api_docstrings import restore_schema_docstrings  # noqa: E402

service_decorator._authenticate_service = fake_authenticate_service
if os.environ.get("MEMLAB_KEEP_DOCSTRINGS") == "1":
    restore_schema_docstrings()

from core.server import server  # noqa: E402
import gdocs.docs_tools  # noqa: E402,F401
import gdrive.drive_tools  # noqa: E402,F401
import gsheets.sheets_tools  # noqa: E402,F401

app = server.http_app(path="/mcp", stateless_http=True)
print(f"READY pid={os.getpid()}", flush=True)
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
