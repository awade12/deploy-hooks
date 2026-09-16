# deploy-hooks

A tiny, dependency-free GitHub webhook receiver that auto-deploys repos on push.
One listener serves any number of repos, each with its own URL, HMAC secret,
branch, and deploy steps. Stdlib Python only.

## How it works

1. You `git push` to a repo's main branch.
2. GitHub POSTs to `https://<your-domain>/api/deploy/<name>` with an
   HMAC-SHA256 signature.
3. The listener verifies the signature, then runs that hook's steps:
   `git fetch` → `git reset --hard` → install → build → restart service.
4. A failed step aborts the deploy; the running version stays up.

## Setup on the server

```bash
# 1. clone this repo
git clone <this-repo> ~/deploy-hooks-src
sudo cp ~/deploy-hooks-src/deploy-hooks.py /home/ubuntu/deploy-hooks.py  # or run from anywhere

# 2. config (NOT in git — secrets stay on the server)
cp deploy-hooks.example.json ~/deploy-hooks.json
# edit: set repo_dir, secret_file, steps per hook
openssl rand -hex 32 > ~/.my-site-webhook-secret
chmod 600 ~/deploy-hooks.json ~/.my-site-webhook-secret

# 3. systemd
sudo cp deploy-hook.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now deploy-hook

# 4. allow the restart step (least privilege: only the service it deploys)
echo "ubuntu ALL=(root) NOPASSWD: /bin/systemctl restart my-site" \
  | sudo tee /etc/sudoers.d/deploy-my-site
sudo chmod 440 /etc/sudoers.d/deploy-my-site

# 5. Caddy — route the hook paths to the listener
handle /api/deploy* {
    reverse_proxy 127.0.0.1:8092
}
```

## Adding a repo

1. Add an entry to `~/deploy-hooks.json` (see the example).
2. Generate its secret: `openssl rand -hex 32 > ~/.<name>-webhook-secret`.
3. `sudo systemctl restart deploy-hook`.
4. In GitHub: repo Settings → Webhooks → Add webhook, payload URL
   `https://<your-domain>/api/deploy/<name>`, content type `application/json`,
   paste the secret, events: just the push event.

## Config reference

```json
{
  "<name>": {
    "secret_file": "/home/ubuntu/.<name>-webhook-secret",
    "repo_dir": "/home/ubuntu/<repo>",
    "branch": "refs/heads/main",
    "steps": [["git", "fetch", "origin"], "..."]
  }
}
```

- `<name>` becomes the URL path `/api/deploy/<name>` (lowercase letters,
  numbers, dashes).
- `branch` defaults to `refs/heads/main`.
- Steps run in order with `repo_dir` as the working directory.

## Notifications (optional)

Each hook can post deploy start / success / failure to Discord:

```json
"notify": {"discord_webhook_file": "/home/ubuntu/.deploy-discord-webhook"}
```

Create a webhook in Discord (channel settings → Integrations → Webhooks),
save its URL to that file (`chmod 600`), and restart the listener. The file
is read at deploy time, so rotating the URL needs no restart. If the file is
missing or empty, notifications are silently skipped — deploys still run.

## Logs

`~/deploy-hooks.log` — every webhook event and every deploy step with its exit
code and output tail.

## Security notes

- Every hook has its own secret; signatures are compared with `hmac.compare_digest`.
- Request bodies are capped at 256 KB; only POST is accepted.
- The listener binds to 127.0.0.1 — it is only reachable through the reverse proxy.
- Keep `deploy-hooks.json` and `*.secret` files at `600` and out of git.
