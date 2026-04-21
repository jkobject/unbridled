#!/usr/bin/env python3
"""
Beeper/Matrix E2EE client for Clawd using matrix-nio.

Handles Olm/Megolm automatically. On first run, generates an Olm device
and uploads keys to the Beeper homeserver. Subsequent runs restore the
store from disk.

Store location:
    ~/.local/share/clawd-matrix/  (chmod 700)

Usage (run in the beeper venv):
    ~/.venvs/beeper/bin/python nio_client.py whoami
    ~/.venvs/beeper/bin/python nio_client.py list-chats --network messenger
    ~/.venvs/beeper/bin/python nio_client.py send --room '!xxx:beeper.local' --text "..."
    ~/.venvs/beeper/bin/python nio_client.py history --room '!xxx:beeper.local' --limit 10

The bridge alias mapping (messenger → facebookgo, etc.) is reused from client.py.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import stat
import sys
import time
from pathlib import Path

# Silence nio's noisy decryption warnings for rooms where we don't hold the group session.
# These appear during sync because nio streams events from all 344 rooms; we only care
# about the ones Clawd explicitly reads or writes.
logging.getLogger("nio.rooms").setLevel(logging.ERROR)
logging.getLogger("nio.crypto").setLevel(logging.ERROR)
logging.getLogger("nio").setLevel(logging.ERROR)

from nio import (
    AsyncClient,
    AsyncClientConfig,
    LoginResponse,
    MatrixRoom,
    MegolmEvent,
    RoomMessageText,
)

BBCTL_CONFIG = Path.home() / ".config" / "bbctl" / "config.json"
STORE_DIR = Path.home() / ".local" / "share" / "clawd-matrix"
SESSION_FILE = STORE_DIR / "session.json"

NETWORK_ALIASES = {
    "messenger": "facebookgo",
    "facebook":  "facebookgo",
    "fb":        "facebookgo",
    "instagram": "instagramgo",
    "ig":        "instagramgo",
    "whatsapp":  "whatsapp",
    "wa":        "whatsapp",
    "linkedin":  "linkedin",
    "twitter":   "twitter",
    "x":         "twitter",
    "discord":   "discordgo",
}


def load_bbctl_creds() -> dict:
    data = json.loads(BBCTL_CONFIG.read_text())
    env = data["environments"]["prod"]
    username = env["username"]
    return {
        "access_token": env["access_token"],
        "user_id": f"@{username}:beeper.com",
        "homeserver": f"https://matrix.beeper.com/_hungryserv/{username}",
        "device_id": data.get("device_id", "bbctl"),
    }


def ensure_store() -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STORE_DIR, stat.S_IRWXU)  # 0o700


def save_session(session: dict) -> None:
    ensure_store()
    SESSION_FILE.write_text(json.dumps(session, indent=2))
    os.chmod(SESSION_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


def load_session() -> dict | None:
    if SESSION_FILE.exists():
        return json.loads(SESSION_FILE.read_text())
    return None


async def make_client() -> AsyncClient:
    """Build an AsyncClient. Reuses nio device_id from previous session if possible."""
    ensure_store()
    bbctl = load_bbctl_creds()
    session = load_session()

    # If we already have a nio-created device_id with a store, reuse it.
    # Otherwise register a new device by logging in with the bbctl access_token
    # via the "login with token" flow? Beeper doesn't support that.
    # Workaround: use bbctl's token directly and use its device_id; nio will
    # bootstrap crypto state for this device.
    device_id = session["device_id"] if session else bbctl["device_id"]

    config = AsyncClientConfig(
        store_sync_tokens=True,
        encryption_enabled=True,
    )
    client = AsyncClient(
        homeserver=bbctl["homeserver"],
        user=bbctl["user_id"],
        device_id=device_id,
        store_path=str(STORE_DIR),
        config=config,
    )
    client.access_token = bbctl["access_token"]
    client.user_id = bbctl["user_id"]
    client.device_id = device_id

    # Required for e2ee init — loads/creates olm account for this device
    client.load_store()
    # Upload identity keys if not already (no-op if already uploaded)
    if client.should_upload_keys:
        await client.keys_upload()

    # Persist session metadata on first go
    if not session:
        save_session({
            "user_id": bbctl["user_id"],
            "device_id": device_id,
            "homeserver": bbctl["homeserver"],
            "created": int(time.time()),
        })

    return client


async def sync_once(client: AsyncClient, timeout_ms: int = 3000) -> None:
    """One sync pass to populate rooms + fetch keys. Non-blocking-ish.

    Uses a persisted next_batch token so subsequent syncs are incremental.
    """
    # full_state=False after first sync keeps it fast; first sync has to be full.
    # We don't track next_batch ourselves — nio does via store_sync_tokens=True.
    await client.sync(timeout=timeout_ms, full_state=False)


async def cmd_whoami(args) -> int:
    client = await make_client()
    try:
        resp = await client.whoami()
        print(f"user_id:    {resp.user_id}")
        print(f"device_id:  {resp.device_id}")
        print(f"homeserver: {client.homeserver}")
        print(f"store:      {STORE_DIR}")
        print(f"e2ee:       enabled={client.olm is not None}")
    finally:
        await client.close()
    return 0


async def cmd_list_chats(args) -> int:
    """Beeper hungryserv does lazy sync so client.rooms is mostly empty. Iterate
    the authoritative joined_rooms() list instead and fetch m.bridge state per room.
    """
    client = await make_client()
    try:
        # One sync to keep olm/device state fresh; we don't rely on client.rooms
        await sync_once(client, timeout_ms=5000)
        joined = await client.joined_rooms()
        room_ids = getattr(joined, "rooms", None) or []
        target = NETWORK_ALIASES.get((args.network or "").lower(), args.network)
        results = []
        for room_id in room_ids:
            state = await client.room_get_state_event(room_id, "m.bridge", "")
            if not hasattr(state, "content"):
                continue
            info = state.content or {}
            bridge = info.get("com.beeper.bridge_name") or info.get("protocol", {}).get("id")
            if target and bridge != target:
                continue
            chan = info.get("channel", {}) or {}
            results.append({
                "room_id": room_id,
                "network": bridge,
                "name": chan.get("displayname") or room_id,
                "external_id": chan.get("id"),
            })
            if len(results) >= args.limit:
                break
        if args.json:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            print(f"{len(results)} chat(s):")
            for c in results:
                print(f"  [{c['network']}] {c['name']!r:40} {c['room_id']}")
    finally:
        await client.close()
    return 0


async def _fetch_room_info(client: AsyncClient, room_id: str, self_user_id: str) -> dict | None:
    """Fetch bridge state + members for one room. Returns None on error."""
    bridge_state, members_resp = await asyncio.gather(
        client.room_get_state_event(room_id, "m.bridge", ""),
        client.joined_members(room_id),
        return_exceptions=True,
    )
    network = None
    if not isinstance(bridge_state, Exception) and hasattr(bridge_state, "content"):
        info = bridge_state.content or {}
        network = info.get("com.beeper.bridge_name") or info.get("protocol", {}).get("id")
    members = []
    if not isinstance(members_resp, Exception) and hasattr(members_resp, "members"):
        members = [
            m for m in members_resp.members
            if m.user_id != self_user_id
            and "bridge bot" not in (m.display_name or "").lower()
            and not m.user_id.startswith("@_")
        ]
    return {"room_id": room_id, "network": network, "members": members}


async def cmd_search_chats(args) -> int:
    """Scan all joined rooms and match members' display_name against a regex.

    DMs on Beeper have no room name — the contact identity lives in the
    member's display_name. This command is the right way to find a room_id
    for a given person before calling `send` or `history`.

    Example patterns: "baptiste", "jean.baptiste", "kalfon|simon"

    Parallelised: all rooms are fetched concurrently in batches of 50 to
    avoid hammering hungryserv while staying fast (~3-6 s on 350 rooms).
    """
    client = await make_client()
    try:
        await sync_once(client, timeout_ms=5000)
        joined = await client.joined_rooms()
        room_ids = getattr(joined, "rooms", None) or []
        target_bridge = NETWORK_ALIASES.get((args.network or "").lower(), args.network)

        try:
            pattern = re.compile(args.pattern, re.IGNORECASE)
        except re.error as e:
            print(f"Invalid regex pattern: {e}", file=sys.stderr)
            return 1

        # Fetch all rooms in parallel, batched to avoid overwhelming hungryserv
        BATCH = 50
        all_rooms: list[dict] = []
        for i in range(0, len(room_ids), BATCH):
            batch = room_ids[i:i + BATCH]
            infos = await asyncio.gather(
                *[_fetch_room_info(client, rid, client.user_id) for rid in batch]
            )
            all_rooms.extend(r for r in infos if r is not None)

        results = []
        for room in all_rooms:
            if target_bridge and room["network"] != target_bridge:
                continue
            if args.dm_only and len(room["members"]) != 1:
                continue
            for m in room["members"]:
                name = m.display_name or m.user_id
                if pattern.search(name):
                    results.append({
                        "room_id": room["room_id"],
                        "network": room["network"],
                        "display_name": name,
                        "user_id": m.user_id,
                    })
                    break  # one match per room

        if args.json:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            print(f"{len(results)} match(es) for /{args.pattern}/:")
            for r in results:
                net = f"[{r['network']}]" if r['network'] else "[?]"
                print(f"  {net:20} {r['display_name']!r:35} {r['room_id']}")
    finally:
        await client.close()
    return 0


async def cmd_send(args) -> int:
    """Send a text message. Because Beeper hungryserv does lazy sync (only
    ships rooms with recent activity), we can't rely on client.rooms being
    populated. We inject the room into client.rooms manually after a full sync,
    based on the authoritative joined_rooms() list.
    """
    client = await make_client()
    try:
        # Sync FIRST so device state / olm is primed. This populates only
        # the rooms with recent activity (Beeper hungryserv behavior).
        await sync_once(client, timeout_ms=10000)

        # Authoritative membership check (after sync to avoid races)
        joined = await client.joined_rooms()
        room_list = getattr(joined, "rooms", None)
        if room_list is None:
            print(f"joined_rooms() error: {joined}", file=sys.stderr)
            return 1
        if args.room not in room_list:
            print(f"Room {args.room} not in joined rooms.", file=sys.stderr)
            return 1

        # Inject minimal room state AFTER sync so it isn't overwritten
        from nio.rooms import MatrixRoom
        if args.room not in client.rooms:
            client.rooms[args.room] = MatrixRoom(args.room, client.user_id, encrypted=True)

        # Load current members into nio (required for megolm share)
        members_resp = await client.joined_members(args.room)
        if hasattr(members_resp, "members"):
            for m in members_resp.members:
                client.rooms[args.room].add_member(m.user_id, m.display_name, m.avatar_url)

        if client.should_query_keys:
            await client.keys_query()

        # Ensure Megolm group session exists
        try:
            await client.share_group_session(args.room, ignore_unverified_devices=True)
        except Exception as e:
            msg = str(e).lower()
            if "already shared" not in msg and "no users" not in msg:
                print(f"share_group_session warning: {e}", file=sys.stderr)

        resp = await client.room_send(
            room_id=args.room,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": args.text},
            ignore_unverified_devices=True,
        )
        if hasattr(resp, "event_id"):
            print(f"sent event_id: {resp.event_id}")
        else:
            print(f"send failed: {resp}", file=sys.stderr)
            return 1
    finally:
        await client.close()
    return 0


async def cmd_history(args) -> int:
    client = await make_client()
    try:
        # Authoritative membership check (before any sync; matches send pattern)
        joined = await client.joined_rooms()
        room_list = getattr(joined, "rooms", None)
        if room_list is None or args.room not in room_list:
            print(f"Room {args.room} not in joined rooms.", file=sys.stderr)
            return 1

        # Sync AFTER to prime olm / group sessions
        await sync_once(client, timeout_ms=10000)

        # Inject MatrixRoom to workaround lazy sync
        from nio.rooms import MatrixRoom
        if args.room not in client.rooms:
            client.rooms[args.room] = MatrixRoom(args.room, client.user_id, encrypted=True)
        members_resp = await client.joined_members(args.room)
        if hasattr(members_resp, "members"):
            for m in members_resp.members:
                client.rooms[args.room].add_member(m.user_id, m.display_name, m.avatar_url)
        if client.should_query_keys:
            await client.keys_query()

        # Use pagination: messages endpoint via room_messages
        resp = await client.room_messages(
            room_id=args.room, start="", limit=args.limit, direction="b"
        )
        events = getattr(resp, "chunk", [])
        out = []
        for ev in events:
            if isinstance(ev, RoomMessageText):
                out.append({
                    "ts": ev.server_timestamp,
                    "sender": ev.sender,
                    "body": ev.body,
                })
            elif isinstance(ev, MegolmEvent):
                # Try to decrypt with the loaded store
                try:
                    decrypted = client.decrypt_event(ev)
                    if isinstance(decrypted, RoomMessageText):
                        out.append({
                            "ts": decrypted.server_timestamp,
                            "sender": decrypted.sender,
                            "body": decrypted.body,
                        })
                    else:
                        out.append({
                            "ts": ev.server_timestamp,
                            "sender": ev.sender,
                            "body": f"[encrypted — {type(decrypted).__name__}]",
                        })
                except Exception as e:
                    out.append({
                        "ts": ev.server_timestamp,
                        "sender": ev.sender,
                        "body": f"[encrypted — {type(e).__name__}]",
                    })
        if args.json:
            print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        else:
            for m in out:
                ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(m["ts"] / 1000))
                body = (m["body"] or "")[:120]
                print(f"  [{ts}] {m['sender']}: {body}")
    finally:
        await client.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Beeper/Matrix E2EE client")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("whoami")

    p_list = sub.add_parser("list-chats")
    p_list.add_argument("--network")
    p_list.add_argument("--limit", type=int, default=25)
    p_list.add_argument("--json", action="store_true")

    p_search = sub.add_parser("search-chats")
    p_search.add_argument("pattern", help="regex pattern matched against member display_name")
    p_search.add_argument("--network", help="restrict to one bridge (messenger, whatsapp, …)")
    p_search.add_argument("--dm-only", action="store_true", help="only return 1-on-1 DMs (2 members)")
    p_search.add_argument("--json", action="store_true")

    p_send = sub.add_parser("send")
    p_send.add_argument("--room", required=True)
    p_send.add_argument("--text", required=True)

    p_hist = sub.add_parser("history")
    p_hist.add_argument("--room", required=True)
    p_hist.add_argument("--limit", type=int, default=20)
    p_hist.add_argument("--json", action="store_true")

    args = parser.parse_args()

    handlers = {
        "whoami":       cmd_whoami,
        "list-chats":   cmd_list_chats,
        "search-chats": cmd_search_chats,
        "send":         cmd_send,
        "history":      cmd_history,
    }
    return asyncio.run(handlers[args.cmd](args))


if __name__ == "__main__":
    sys.exit(main())
