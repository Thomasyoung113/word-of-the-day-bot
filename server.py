"""WSGI entry point for Render.
Runs the bot polling loop in a daemon thread while gunicorn serves
a health-check endpoint to keep the web service alive.
"""
import logging
import threading
import time

from main import main as run_bot

logger = logging.getLogger(__name__)


def _run_bot_forever() -> None:
    """Run the bot with auto-restart on crash."""
    while True:
        try:
            run_bot()
        except Exception as e:
            logger.exception("Bot polling crashed: %s — restarting in 5s", e)
            time.sleep(5)


# Start the bot in background with auto-restart
bot_thread = threading.Thread(target=_run_bot_forever, daemon=True)
bot_thread.start()


def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    if path == "/healthz":
        status = "200 OK"
        body = b"OK"
    else:
        status = "200 OK"
        body = b"Word of the Day bot is running."
    headers = [("Content-Type", "text/plain")]
    start_response(status, headers)
    return [body]