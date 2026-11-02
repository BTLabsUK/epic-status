# Deploying status.btlabs.uk

The live board runs on the BT Labs server (`ssh hostinger-eu`) from a git checkout in
`~/epic-status`, as the systemd unit `epic-status.service` (127.0.0.1:13431), behind nginx
(`/etc/nginx/sites-available/epic-status`, `conf.d/status-ratelimit.conf`,
`conf.d/status-onion.conf`) and the Tor unit `epic-status-tor.service`. It is not run by pm2.

Source of truth: `github.com/BTLabsUK/epic-status`, branch `main`. Changes are made and
tested locally, pushed to GitHub, and the server only pulls. Never commit on the server.

## Steps

```sh
ssh hostinger-eu
cd ~/epic-status

# 1. Back up the checkout, minus the venv (the database is under data/).
tar --exclude=.venv -czf ~/backups/epic-status-$(date -u +%Y%m%d-%H%M%S).tgz -C ~ epic-status

# 2. Bring the checkout to GitHub main. Fast-forward only: a refusal means someone
#    committed on the server; push those commits to GitHub first instead of overwriting them.
git status --short          # must be empty
git fetch origin
git merge --ff-only origin/main

# 3. Dependencies, only when pyproject.toml or uv.lock changed. The unit runs
#    `uv run --no-sync`, so it never syncs by itself.
~/.local/bin/uv sync --frozen

# 4. Restart and check.
sudo systemctl restart epic-status
systemctl is-active epic-status
journalctl -u epic-status --since "-5 min" --no-pager | tail -20
curl -s -o /dev/null -w '%{http_code}\n' https://status.btlabs.uk/
curl -s https://status.btlabs.uk/api/v1/status.json | head -c 300
```

## What the files in `ops/` are

`ops/` holds templates with example paths (`/opt/epic-status`) and an example user. The
installed copies in `/etc/systemd/system/` and `/etc/nginx/` are edited for this server and
are not overwritten on deploy. When a change in `ops/` matters (a new nginx location, a unit
setting), apply that change to the installed file by hand, then `sudo nginx -t && sudo
systemctl reload nginx` or `sudo systemctl daemon-reload && sudo systemctl restart
epic-status`.

## Rollback

`git -C ~/epic-status reset --hard <previous commit>` (the previous `HEAD` is in
`git reflog`), `uv sync --frozen` if the lock changed, `sudo systemctl restart epic-status`.
The database is not migrated backwards; restore `data/status.db` from the backup only if a
release changed its schema.
