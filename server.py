"""WSGI entry point for Render.
Runs the bot polling loop in a daemon thread while gunicorn serves
a health-check endpoint to keep the web service alive.
"""
import os
import threading

from main import main as run_bot

# Start the bot in background — it creates its own event loop internally
bot_thread = threading.Thread(target=run_bot, daemon=True)
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