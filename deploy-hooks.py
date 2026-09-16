#!/usr/bin/env python3
"""Multi-repo GitHub webhook receiver: auto-deploy on push.

Listens on 127.0.0.1:8092 (Caddy proxies /api/deploy* to it).
Hooks are defined in CONFIG_FILE; each hook has its own URL, HMAC secret,
repo directory, branch, and deploy steps.

URLs:
  /api/deploy/<name>        - hook named <name>
  /api/deploy-chronafc      - legacy alias for the "chronafc" hook

On a valid push to the hook's branch: run its steps in order.
A failing step aborts the deploy, leaving the running version untouched.
Stdlib only.
"""

import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8092
CONFIG_FILE = "/home/ubuntu/deploy-hooks.json"
LOG_FILE = "/home/ubuntu/deploy-hooks.log"
MAX_BODY = 256 * 1024
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")


def log(hook: str, msg: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} [{hook}] {msg}\n"
    with open(LOG_FILE, "a") as f:
        f.write(line)


def load_config() -> dict:
    with open(CONFIG_FILE) as f:
        config = json.load(f)
    if not isinstance(config, dict):
        raise RuntimeError("config must be a JSON object")
    return config


def load_secret(secret_file: str) -> bytes:
    with open(secret_file) as f:
        secret = f.read().strip()
    if not secret:
        raise RuntimeError(f"secret file {secret_file} is empty")
    return secret.encode()


def run(hook: str, repo_dir: str, cmd: list[str]) -> bool:
    try:
        p = subprocess.run(
            cmd, cwd=repo_dir, capture_output=True, text=True, timeout=900
        )
    except Exception as e:  # noqa: BLE001 - log and fail the deploy
        log(hook, f"STEP {' '.join(cmd)} EXCEPTION {e}")
        return False
    tail = (p.stderr or p.stdout or "")[-1500:]
    log(hook, f"STEP {' '.join(cmd)} -> exit={p.returncode} tail={tail!r}")
    return p.returncode == 0


def notify(hook: str, cfg: dict, message: str) -> None:
    """Post a message to the hook's Discord webhook, if configured.

    The webhook URL lives in a file on the server (never in the repo).
    Failures are logged, never fatal to a deploy.
    """
    dest = (cfg.get("notify") or {}).get("discord_webhook_file")
    if not dest:
        return
    try:
        with open(dest) as f:
            url = f.read().strip()
        if not url:
            return
        data = json.dumps({"content": message}).encode()
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # noqa: BLE001 - notification must not break deploys
        log(hook, f"notify failed: {e}")


def deploy(hook: str, cfg: dict, sha: str) -> None:
    started = time.time()
    log(hook, "DEPLOY start")
    notify(hook, cfg, f"\U0001f680 Deploying **{hook}** (`{sha}`)...")
    repo_dir = cfg["repo_dir"]
    for cmd in cfg["steps"]:
        if not run(hook, repo_dir, cmd):
            log(hook, "DEPLOY FAILED - previous version still running")
            notify(
                hook,
                cfg,
                f"\u274c **{hook}** deploy FAILED at `{' '.join(cmd)}`",
            )
            return
    secs = int(time.time() - started)
    log(hook, "DEPLOY ok")
    notify(hook, cfg, f"\u2705 **{hook}** deployed in {secs}s")


class Handler(BaseHTTPRequestHandler):
    server_version = "deploy-hooks/1"

    def _send(self, code: int, body: str = "") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(data and len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _hook_name(self) -> str | None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/deploy-chronafc":
            return "chronafc"  # legacy alias
        if path.startswith("/api/deploy/"):
            name = path[len("/api/deploy/") :]
            if NAME_RE.match(name):
                return name
        return None

    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        name = self._hook_name()
        if not name:
            self._send(404, "not found")
            return
        try:
            config = load_config()
        except Exception as e:  # noqa: BLE001
            self._send(500, "bad config")
            return
        cfg = config.get(name)
        if not cfg:
            self._send(404, "unknown hook")
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send(400, "bad length")
            return
        if length <= 0 or length > MAX_BODY:
            self._send(413, "too large")
            return
        body = self.rfile.read(length)

        secret = load_secret(cfg["secret_file"])
        sig = self.headers.get("X-Hub-Signature-256") or ""
        expected = "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            log(name, "webhook rejected: bad signature")
            self._send(401, "bad signature")
            return

        try:
            event = self.headers.get("X-GitHub-Event") or ""
            payload = json.loads(body.decode("utf-8"))
        except Exception:  # noqa: BLE001
            self._send(400, "bad payload")
            return

        if event == "ping":
            log(name, "webhook ping ok")
            self._send(200, "pong")
            return
        branch = cfg.get("branch", "refs/heads/main")
        if event != "push" or payload.get("ref") != branch:
            self._send(200, "ignored")
            return

        sha = (payload.get("after") or "")[:8]
        log(name, f"webhook push to {branch} ({sha}) - deploying")
        threading.Thread(target=deploy, args=(name, cfg, sha), daemon=True).start()
        self._send(202, "deploying")

    def do_GET(self) -> None:  # noqa: N802
        self._send(405, "method not allowed")

    def log_message(self, *args):  # noqa: ANN002, ANN202
        pass


if __name__ == "__main__":
    cfg = load_config()  # fail fast on bad config
    for hook_name, hook_cfg in cfg.items():
        load_secret(hook_cfg["secret_file"])  # fail fast on missing secrets
    log("system", f"listener start ({len(cfg)} hooks)")
    HTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()
