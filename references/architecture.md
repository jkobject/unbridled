# Architecture — beeper-matrix

## Components

```
┌───────────────────────────────┐
│ External networks             │
│  FB / WA / IG / LI / X / …    │
└──────────────▲────────────────┘
               │
┌──────────────┴────────────────┐
│ Beeper bridges (cloud)        │
│  facebookgo, whatsapp,        │
│  instagramgo, linkedin,       │
│  twitter, signal, …           │
│  (managed by Beeper, run on   │
│   their infra)                │
└──────────────▲────────────────┘
               │ appservice
┌──────────────┴────────────────┐
│ hungryserv.beeper.com         │
│  per-user Matrix homeserver   │
│  https://matrix.beeper.com    │
│    /_hungryserv/<username>    │
│  enforces E2EE on outgoing    │
└──────────────▲────────────────┘
               │ Matrix CS API (HTTPS)
               │
        ┌──────┴──────────┐
        │  Agent host     │
        │                 │
        │  bbctl binary   │── stores access_token ──▶ ~/.config/bbctl/config.json
        │                 │
        │  ~/.venvs/beeper│── matrix-nio[e2e] ──▶ Olm/Megolm crypto
        │                 │
        │  scripts/beeper/│── nio_client.py ─── async CLI (send/list/history)
        │                 │── client.py ─────── sync HTTP (list, bridge state)
        │                 │── bootstrap_crosssign.py ── one-shot recovery-key → device signature
        │                 │── verify_interactive.py ─── optional SAS fallback
        │                 │
        │  ~/.local/share │
        │   /clawd-matrix │── Olm account + Megolm sessions + sync token (persistent, chmod 700)
        │                 │
        │  ~/.secrets/    │── recovery-key.txt (chmod 600)
        └─────────────────┘
```

## Why cross-signing matters

Beeper's policy on outgoing messages from a new device:

> `com.beeper.message_send_status` → `FAIL_RETRIABLE` with `"your device is not trusted (unverified)"`

A raw `room_send` through the CS API will be accepted by the homeserver but **silently dropped** by the bridge. Symptom: the user sees nothing on the external network, even though Matrix returns `event_id`.

Cross-signing fixes this by having the user's `self_signing` key (a per-account ed25519 identity, stored encrypted in Secret Storage) sign the agent's device public key. From then on, Beeper bridges will decrypt and forward.

## Recovery key → self-signing key (what `bootstrap_crosssign.py` does)

```
recovery-key.txt (e.g. "EsUC HBcy scrf uiTy …")
   │ strip spaces + base58 decode
   ▼
35-byte blob
   │ check prefix 0x8B 0x01, verify XOR parity
   ▼
32-byte SSSS seed
   │ HKDF-SHA256(salt=32·0x00, info="")
   ▼
(AES-32, HMAC-32) master keys
   │ verify MAC matches m.secret_storage.key.<default>   ← confirms seed is right
   │
   │ for each name in { master, self_signing, user_signing }:
   │    HKDF-SHA256(salt=32·0x00, info=name) → (AES, HMAC)
   │    fetch m.cross_signing.<name> account data
   │    HMAC-verify ciphertext, AES-CTR decrypt with given IV
   │    result = base64 of a 32-byte ed25519 seed
   ▼
3 × 32-byte ed25519 seeds (master, self_signing, user_signing)
   │
   │ sign (device_keys minus signatures/unsigned, canonical JSON)
   │   with self_signing seed
   ▼
ed25519 signature (base64)
   │ POST /_matrix/client/v3/keys/signatures/upload
   ▼
Device now has 2 signatures → trusted by bridges.
```

## Send path (nio_client.py send)

```
make_client()   ← load bbctl token, attach Olm account, load store
client.sync(full_state=False)           ← prime olm, ignore room population
client.joined_rooms()                   ← authoritative room list
assert room_id in joined.rooms
client.rooms[room_id] = MatrixRoom(…, encrypted=True)     ← workaround lazy sync
client.joined_members(room_id)          ← populate members for megolm
room.add_member(u, name, avatar) for m in members
client.keys_query()                     ← if stale
client.share_group_session(room_id)     ← create / resume Megolm session
client.room_send(room_id, m.room.message, {msgtype:text, body})
                                        ← nio encrypts with Megolm, PUTs via /rooms/{id}/send/…
                                        ← hungryserv encrypts to bridge puppet
                                        ← bridge decrypts (now trusted), sends on FB/WA/…
event_id → printed
```

## Lazy sync quirk

Beeper's hungryserv is a **per-user homeserver**. It implements a cut-down subset of the CS API that trims sync responses to what's "active". Symptoms:
- `client.sync(full_state=True)` returns 1 room or so (the most recently active).
- `/sync` response validation fails nio's jsonschema (`'events' is a required property`) → flood of warnings; silenced by `logging.getLogger("nio").setLevel(ERROR)`.
- The authoritative `/v3/joined_rooms` endpoint returns all 300+ rooms correctly.

The wrapper's approach:
1. Always use `joined_rooms()` for the membership check.
2. Manually build a `MatrixRoom` shell and `add_member` before `room_send` so nio's Megolm logic has what it needs.

This is a **documented Beeper behavior**, not a bug on our side, and unlikely to change given hungryserv's scale constraints.

## Why not self-hosted bridges (`bbctl run sh-meta`)?

Alternative we considered: run `mautrix-meta` / `mautrix-whatsapp` bridges directly on the agent host via `bbctl run sh-<name>`. Pros: encryption done locally, no cross-signing dance. Cons:
- Full bridge lifecycle to manage (systemd, restarts, crashes).
- Login must be redone per bridge (QR scans, FB credentials).
- Existing cloud bridges would have to be disconnected or coexist confusingly.
- Each bridge is a Go process eating ~50–200 MB RAM.

For an always-on agent on a VPS, cross-signing + cloud bridges is lighter. Self-hosting is a good fallback if Beeper ever tightens cloud bridge policies further.
