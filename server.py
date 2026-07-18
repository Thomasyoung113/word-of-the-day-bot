"""Standalone entry point — bot polling in main thread + health check HTTP server.
No gunicorn needed.
"""
import http.server
import logging
import os
import threading

from main import main as run_bot

PORT = int(os.getenv("PORT", "10000"))
logger = logging.getLogger(__name__)


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def _run_http() -> None:
    server = http.server.HTTPServer(("0.0.0.0", PORT), _HealthHandler)
    logger.info("Health check server listening on 0.0.0.0:%s", PORT)
    server.serve_forever()


# Start HTTP health check in a background daemon thread
http_thread = threading.Thread(target=_run_http, daemon=True)
http_thread.start()

logger.info("Starting bot polling...")
# Run bot polling in the main thread (blocking call)
run_bot()