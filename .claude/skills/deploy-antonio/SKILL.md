---
name: deploy-antonio
description: Deploy this checkout's latest commit to antonio (the production board) and check it - the dry run, the deploy, check_board at once and again once the history's first write is due, a short report in Italian. Only when the user runs /deploy-antonio.
disable-model-invocation: true
---

# Deploy to antonio

antonio is the production board: real Shellys, the VMC in daily use. The
user ran this skill, so the deploy itself is asked for - but nothing else
on the board is: no device driven, no setting changed, no restart beyond
the deploy's own, no tune-board.

## 1. Before

- `git status --short` and `git log --oneline origin/main..main`: say what
  is local. Uncommitted changes are NOT deployed (the tool sends commits) -
  say so if there are any, and stop to ask if they look meant for it.
- The full suites must have passed on what's being deployed: if this
  session hasn't run them since the last commit, run `pytest -n auto -q`
  and `pytest -m ui -n 6 -q` (a known Windows-only `PermissionError` on
  `schedules.json` is not a failure: rerun that one test alone).
- The board's SSH password comes only from the `LH_SSH_PW` environment
  variable. If it isn't set, ask the user to set it in the session
  (`export LH_SSH_PW=...`) - never write it to a file, never echo it.

## 2. Dry run, then deploy

In Git Bash, from the repo root:

```bash
export MSYS_NO_PATHCONV=1 LH_SSH_USER=arduino
python tools/deploy_board.py antonio --check     # commits that would go, password and transfer tried
python tools/deploy_board.py antonio
```

- If `antonio.local` doesn't resolve (Windows mDNS after a reboot), set
  `LH_SSH_HOST=192.168.1.28` and use `192.168.1.28` for the checks below.
- "dependencies changed": the board's venv is reinstalled - minutes, fine.
- "note: deploy/ changed": tell the user to run, on antonio,
  `cd ~/progetto && sudo sh deploy/tune-board.sh` (then a reboot if it
  says so). Don't run it yourself.
- If the deploy fails half-way, report the output; offer
  `python tools/deploy_board.py antonio --rollback`, run it only on a yes.

## 3. Check

Right after (read-only):

```bash
python tools/check_board.py antonio --log
```

LocalHome has just restarted, so the history's checks are skipped ("first
write is due after 7"). Run it again once LocalHome has been up 7.5
minutes - in the background, so the user can go on:

```bash
until python -c "
import json,sys,urllib.request
c=json.load(urllib.request.urlopen('http://antonio.local:5000/api/server-time', timeout=10))
sys.exit(0 if c['epoch_ms']/1000 - c['started_at'] > 7*60 + 30 else 1)"; do sleep 20; done
python tools/check_board.py antonio
```

## 4. Report (in Italian, short)

- the commit now on antonio (`/api/system-status` → `software.commit`), and
  whether it's pushed;
- check_board: passed / not, each "NO" with its line; the problems the
  board shows;
- anything the user has to do (tune-board, a setting in the Shelly app the
  admin flags);
- the second check's result when it comes.

A "NO" in check_board is reported as it is - not explained away. If one
looks like a regression of what was just deployed, say so and offer the
rollback.
