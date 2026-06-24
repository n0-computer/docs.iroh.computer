#!/usr/bin/env python3
"""Local repro of the *production* serving conditions for the diagrams.

`mint dev` serves SVGs with no CSP, so CSS animation works in preview but dies
on the deployed site, which sends `Content-Security-Policy: default-src 'none'`.
This server replays that exact header and embeds every diagram via <img> (the
way Mintlify does), so you can confirm the SMIL animations survive it.

Run:   python3 scripts/csp_preview.py
Open:  http://localhost:8000/

If an animation moves here, it will move on docs.iroh.computer. If it freezes
here but works in `mint dev`, it's still relying on CSS/<style> animation.
"""
import http.server
import os
import socketserver
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
IMG_DIR = os.path.normpath(os.path.join(HERE, "..", "images", "how-iroh-works"))
# Override with: python3 scripts/csp_preview.py 8080   (or PORT=8080 ...)
PORT = int(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PORT", 8000))

SVGS = sorted(f for f in os.listdir(IMG_DIR) if f.endswith(".svg"))
INDEX = "<!doctype html><meta charset=utf-8><title>CSP preview</title>" \
        "<body style='background:#fff;font-family:sans-serif;max-width:760px;margin:2em auto'>" \
        "<p style='color:#888'>Served with <code>Content-Security-Policy: default-src 'none'</code> " \
        "(production conditions). If it animates here, it animates on the deployed site.</p>" + \
        "".join(
            f"<h3 style='color:#888;font-weight:400'>{name}</h3>"
            f"<img src='/{name}' style='width:100%;border:1px solid #eee'/>"
            for name in SVGS
        ) + "</body>"


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=IMG_DIR, **k)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = INDEX.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def end_headers(self):
        # Match what Vercel/Mintlify sends for the SVG assets.
        if self.path.endswith(".svg"):
            self.send_header("Content-Security-Policy", "default-src 'none'")
        super().end_headers()


socketserver.TCPServer.allow_reuse_address = True
with socketserver.TCPServer(("", PORT), Handler) as httpd:
    print(f"serving {len(SVGS)} diagrams with production CSP at http://localhost:{PORT}/")
    print("Ctrl-C to stop.")
    httpd.serve_forever()
