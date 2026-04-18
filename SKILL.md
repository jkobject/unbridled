---
name: unbridled
description: Send and read messages on Facebook Messenger, WhatsApp, Instagram, LinkedIn, Twitter/X, Signal, Telegram, Discord and other networks through a Beeper account, using the cloud Matrix bridges (hungryserv). Installs bbctl, sets up E2EE with matrix-nio, bootstraps cross-signing from the user's Beeper recovery key, runs a long-running sync daemon for incoming decryption, and exposes a single Python wrapper to list/search/send/read chats across all bridged networks.
version: 0.1.0
author: clawd + jeremie
homepage: https://github.com/jkobject/unbridled
tags: [messaging, beeper, matrix, e2ee, facebook-messenger, whatsapp, instagram, linkedin, multi-platform]
---

# unbridled

Unified messaging for agents via a personal Beeper account.

## What it does

- Connects to the user's **Beeper Cloud** account (`https://matrix.beeper.com/_hungryserv/<user>`).
- Uses the **hungryserv Matrix homeserver** that Beeper exposes per-user.
- Reads / sends messages across all networks bridged inside Beeper (Messenger, WhatsApp, Instagram, LinkedIn, Twitter/X, Signal, Telegram, Discord, Google Messages, …).
- Handles **Olm/Megolm E2EE** via `matrix-nio[e2e]`.
- Bootstraps **cross-signing** from the user's Beeper Recovery Key, so Beeper's bridges trust the agent's device and actually relay messages to the external networks.

Under the hood it is a thin, dependency-minimal layer:

```
agent / clawd
    │
    │ CLI / Python import
    ▼
scripts/beeper/
    ├── nio_client.py          ← async send / list / history (E2EE)
    ├── client.py              ← sync HTTP wrapper (no e2ee; list + bridge state)
    ├── bootstrap_crosssign.py ← one-shot: recovery key → cross-sign device
    └── verify_interactive.py  ← fallback: SAS verification via Beeper Desktop
    ▼
bbctl + access token (~/.config/bbctl/config.json)
    ▼
https://matrix.beeper.com/_hungryserv/<user>   (Matrix CS API)
    ▼
Beeper bridges (cloud) → Messenger / WhatsApp / IG / LinkedIn / Twitter / …
```

## When to use this skill

Use **beeper-matrix** when the user asks to:
- Send a Messenger / WhatsApp / IG / LinkedIn / Twitter DM from an agent context.
- Read or search their chats across networks.
- Build a recap, auto-reply, digest, or cron job that touches personal chats.

Do **not** use it to blast messages to many contacts or to automate replies without explicit consent per-thread.

## Prerequisites (user-side)

The user must have:
1. A **Beeper account** (beeper.com) with the desired networks bridged (Facebook, WhatsApp, etc.).
2. Their **Beeper Recovery Key** — a phrase like `EsUC HBcy scrf uiTy DTU2 rvEB Jmgj 9Cpa D6V2 z7Vk ZrU9 9RMh`.

### Where to find the Beeper Recovery Key

In **Beeper Desktop**:
1. Click **Settings** (gear icon)
2. Click on **your name** at the top
3. Click the **arrow (⌄)** next to your name to expand
4. Click **"Show Recovery Code"**

The code is shown as 12 groups of 4 base58 characters.

⚠️ It's a full account recovery secret — treat it like a password. If it's ever exposed, regenerate it from the same menu.

## Prerequisites (host-side, agent)

- Linux (tested on Ubuntu 24.04). macOS should work too.
- `python3` with `venv`
- `uv` (or plain `pip`) to create the venv
- `libolm-dev` (package for Olm/Megolm crypto)
- `ffmpeg` (optional, for media attachments later)

## Setup (one-time, ~5 min)

Follow these steps once on the agent host.

### 1. Install `bbctl` and deps

```bash
# bbctl binary (Beeper Bridge Manager)
mkdir -p ~/bin
curl -sL -o ~/bin/bbctl https://github.com/beeper/bridge-manager/releases/latest/download/bbctl-linux-amd64
chmod +x ~/bin/bbctl

# System deps
sudo apt install -y libolm-dev ffmpeg python3-venv

# Agent Python venv (dedicated, not global)
uv venv ~/.venvs/beeper --python 3.12
VIRTUAL_ENV=~/.venvs/beeper uv pip install 'matrix-nio[e2e]' aiohttp pycryptodome cryptography pynacl unpaddedbase64 canonicaljson
```

### 2. Log into Beeper from `bbctl`

```bash
bbctl login
```

The user enters their Beeper credentials. This stores an access token at `~/.config/bbctl/config.json`. That token is all the nio client needs.

Verify:
```bash
bbctl whoami
# Should show: User ID: @<user>:beeper.com, all bridges RUNNING
```

### 3. Save the Beeper Recovery Key

```bash
mkdir -p ~/.secrets && chmod 700 ~/.secrets
cat > ~/.secrets/beeper-recovery-key.txt <<EOF
EsUC HBcy scrf uiTy DTU2 rvEB Jmgj 9Cpa D6V2 z7Vk ZrU9 9RMh
EOF
chmod 600 ~/.secrets/beeper-recovery-key.txt
```

Replace the sample with the actual recovery key. Never commit to git.

### 4. Copy skill scripts into place

The 4 scripts live at `scripts/beeper/` in the workspace (they come with the skill — see `scripts/` subfolder). Ensure:

```bash
ls ~/.openclaw/workspace/scripts/beeper/
# client.py  nio_client.py  bootstrap_crosssign.py  verify_interactive.py
```

### 5. Bootstrap nio store + cross-sign the device

```bash
# First run initializes the Olm store (~/.local/share/clawd-matrix/)
~/.venvs/beeper/bin/python ~/.openclaw/workspace/scripts/beeper/nio_client.py whoami
# Should print: e2ee: enabled=True

# Sign the device using the recovery key
~/.venvs/beeper/bin/python ~/.openclaw/workspace/scripts/beeper/bootstrap_crosssign.py
# Expected output ends with:
#   🎉 SUCCESS — device is now cross-signed. Bridge should accept our messages.
```

After this step, Beeper's bridges trust the agent's device and will relay messages to the external networks.

## Daily usage

All commands run inside the venv:

```bash
NIO=~/.venvs/beeper/bin/python
SCRIPT=~/.openclaw/workspace/scripts/beeper/nio_client.py

# Identity check
$NIO $SCRIPT whoami

# List chats for a specific network
$NIO $SCRIPT list-chats --network messenger --limit 25
$NIO $SCRIPT list-chats --network whatsapp  --limit 50
$NIO $SCRIPT list-chats --network linkedin

# Aliases accepted: messenger/facebook/fb, whatsapp/wa, instagram/ig,
# linkedin, twitter/x, signal, telegram, discord

# Send a message (E2EE handled automatically)
$NIO $SCRIPT send --room '!xxx:beeper.local' --text "Hello from clawd"

# Read recent history in a room (requires megolm session; may be empty on first run)
$NIO $SCRIPT history --room '!xxx:beeper.local' --limit 20
```

Python import usage (recommended for cron / scripts):

```python
import sys, asyncio
sys.path.insert(0, "/home/ubuntu/.openclaw/workspace/scripts/beeper")
from nio_client import make_client

async def ping():
    c = await make_client()
    try:
        # ... use c.room_send / c.joined_rooms / etc.
        pass
    finally:
        await c.close()

asyncio.run(ping())
```

## Quirks to know about

- **Lazy sync**: Beeper hungryserv doesn't ship all rooms on `sync(full_state=True)`. nio's `client.rooms` will be mostly empty. The wrapper sidesteps this by calling `joined_rooms()` directly and manually injecting a minimal `MatrixRoom` before `room_send`. Don't rely on `client.rooms` being complete.
- **Lots of decryption warnings**: nio tries to decrypt every event across all 300+ rooms during sync and prints warnings for rooms where the group session is missing. The wrapper silences them via `logging.getLogger("nio").setLevel(ERROR)`.
- **Schema warning flood** (`'events' is a required property`): hungryserv returns sync responses with fields nio doesn't fully recognize. Safe to ignore; already silenced.
- **Bridge trust**: without cross-signing, Beeper bridges reject outgoing messages with `com.beeper.undecryptable_event` / `your device is not trusted (unverified)`. `bootstrap_crosssign.py` fixes this once and for all for this device.
- **"Notes to self" chats don't relay**: the `Facebook Messenger (Jérémie Kalfon)` auto-chat has no external recipient. Test sends on a chat with a real other user.
- **First message to a new chat**: may take 1-2 seconds extra as nio establishes the Megolm group session.
- **Recovery key != SSH key**: this is a secret that lets *anyone with it* impersonate the user on all Beeper-bridged networks. Handle accordingly.

## Safety rules for the agent

- Never send a DM on a new thread without explicit user confirmation.
- Never mass-send or loop over contacts.
- In group chats, the agent is not the user's voice — stay silent unless directly asked.
- Log every outgoing send in the workspace journal so the user can audit.
- If the user revokes the Beeper session (logout device), the Olm store becomes stale — re-run `bootstrap_crosssign.py` after a new `bbctl login`.

## Files in this skill

```
unbridled/
├── SKILL.md                              (this file)
├── README.md                             GitHub-facing doc
├── LICENSE                               MIT
├── install.sh                            prerequisites installer
├── scripts/
│   ├── client.py                         sync HTTP wrapper (list + bridge state)
│   ├── nio_client.py                     async E2EE client (send/list/history)
│   ├── bootstrap_crosssign.py            one-shot: recovery key → cross-sign
│   ├── verify_interactive.py             fallback: SAS via Beeper Desktop
│   ├── sync_daemon.py                    long-running sync (Megolm accumulation)
│   └── collect_beeper_daily.py           daily digest generator
├── systemd/
│   ├── clawd-beeper-sync.service         user-level systemd unit for the daemon
│   └── install.sh                        installs the unit into ~/.config/systemd/user/
└── references/
    ├── setup-checklist.md                step-by-step for humans
    └── architecture.md                   diagrams and crypto flow
```

## Status

- v0.1.0 — 2026-04-18 — Works for send on cross-signed device. History (megolm decrypt) partial. Multi-platform: Messenger / WhatsApp / IG / LinkedIn / Twitter confirmed on an active Beeper account.

## Inbound decryption — the sync daemon

Beeper enforces E2EE on outgoing, but also means inbound messages stay encrypted until your device has received the right Megolm group sessions (via `to_device` events during a sync). Short-lived scripts miss most of them.

`scripts/sync_daemon.py` + `systemd/clawd-beeper-sync.service` provide a supervised long-running sync that accumulates these sessions. After a few minutes of running, `history` and `collect_beeper_daily.py` will decrypt nearly all new traffic.

Install:
```bash
bash systemd/install.sh
systemctl --user enable --now clawd-beeper-sync.service
systemctl --user status clawd-beeper-sync
journalctl --user -u clawd-beeper-sync -f
```

Resource footprint: ~35 MB RAM idle, negligible CPU.

## Roadmap

- [ ] `unread --network X` helper (summarize unread chats)
- [ ] `reply --to <event_id>` with proper Matrix reply threading
- [ ] `mark-read` on inbound messages
- [ ] Media / attachments (image send)
- [ ] Publish v1.0 on ClawHub
