"""Root entrypoint for Career Pulse & Radar (Vercel & Local)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Vercel's Python builder uses static AST parsing to find the "app" variable.
# It MUST be a top-level import, otherwise Vercel fails with "Could not find a top-level app".
from app.main import app

# FIX: Vercel rewrites internal destinations (like /api/index) directly into ASGI PATH_INFO, 
# breaking FastAPI routers and auth redirects. This middleware strips it back to the original path.
from starlette.types import ASGIApp, Receive, Scope, Send

class VercelPathFixMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            
            headers_dict = {}
            for k, v in scope.get("headers", []):
                k_str = k.decode('utf-8', 'ignore')
                v_str = v.decode('utf-8', 'ignore')
                headers_dict[k_str] = v_str
            
            # Read and parse query string
            import urllib.parse
            query_str = scope.get("query_string", b"").decode("utf-8")
            parsed_qs = urllib.parse.parse_qs(query_str, keep_blank_values=True)
            
            # Extract __vercel_path if present
            if "__vercel_path" in parsed_qs:
                # Get the path and format it with a leading slash
                original = parsed_qs.pop("__vercel_path")[0]
                if not original.startswith("/"):
                    original = "/" + original
                scope["path"] = original
                
                # Reconstruct query string without __vercel_path
                new_qs = urllib.parse.urlencode(parsed_qs, doseq=True).encode("utf-8")
                scope["query_string"] = new_qs
            else:
                # Temporary fallback just in case
                if path == "/api/index" or path == "/main.py":
                    scope["path"] = "/"
                elif path.startswith("/api/index/"):
                    scope["path"] = path.replace("/api/index", "", 1)
                elif path.startswith("/main.py/"):
                    scope["path"] = path.replace("/main.py", "", 1)
                
        await self.app(scope, receive, send)

app.add_middleware(VercelPathFixMiddleware)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8770, reload=True)
