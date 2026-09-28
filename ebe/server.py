"""Loopback, bearer-authenticated, read-only status API. No remote tool execution."""
import hmac
import json
import re
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from ebe.core import modules
from ebe.isolation import validate_local_path
from ebe.transfer import safe_child


def readonly_status(project):
    project = Path(validate_local_path(project))
    meta = safe_child(project, "project.json")
    db = safe_child(project, "state.sqlite3")
    metadata = json.loads(meta.read_text(encoding="utf-8"))
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=3)) as conn:
        conn.execute("PRAGMA query_only=ON")
        sources = dict(conn.execute("SELECT status, COUNT(*) FROM sources GROUP BY status"))
        artifacts = dict(conn.execute("SELECT artifact_type, COUNT(*) FROM artifacts GROUP BY artifact_type"))
    return {"project_id": metadata["project_id"], "sources": sources, "artifacts": artifacts}


def serve(root, port, token):
    if not isinstance(token, str) or len(token) < 32 or not 1024 <= port <= 65535:
        raise ValueError("invalid_server_configuration")
    root = Path(validate_local_path(root)).resolve()
    engine, _, _ = modules()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"} or self.headers.get("Origin"):
                self.send_error(403); return
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.send_error(401); return
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                data = {"status": "ok", "service": "EBE", "mode": "read-only"}
            elif parsed.path == "/status":
                slug = parse_qs(parsed.query).get("project", [""])[0]
                if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", slug):
                    self.send_error(400); return
                project = root / slug
                if project.is_symlink() or not project.resolve().is_relative_to(root):
                    self.send_error(403); return
                try:
                    data = readonly_status(project)
                except Exception:
                    self.send_error(404); return
            else:
                self.send_error(404); return
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers(); self.wfile.write(body)
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        server.serve_forever()
