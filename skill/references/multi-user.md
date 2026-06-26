# Hosting the bridge for a team (multi-user)

The default skill assumes single-user self-host. One bridge server can also serve **several people** — each with their own token and their own paired Chrome. This page is the delta from the single-user flow.

## The model

- Each person is a separate user (`server.cli create-user <name>`), with a token at `$BB_HOME/data/users/<name>.token`.
- Each person pairs their own browser with their own pairing code (`create-pairing-code <name>`), exactly as in `references/onboard.md` — just repeat it per user.
- A command carries one `Authorization: Bearer <token>`, and that token decides whose browser runs it.

## The ownership rule (enforced by the server)

**A token only ever drives its own owner's paired device.** This isn't a convention you have to police by hand — the server enforces it:

- When you don't pass `device_id`, the server resolves it from the token's user and only considers devices owned by that user (`_get_online_devices_for_user` filters on `user_id`).
- When you *do* pass an explicit `device_id`, the server checks the connection's `user_id` against the token's user and returns **404** if they don't match — so a token can't reach across to someone else's device, and can't even probe which device IDs exist (the **AUTHZ-1 / IDOR** guard in `server/api/command.py`).

So the worst case of a confused agent is a `DEVICE_OFFLINE`/404, not a cross-user action. Still, **pick the correct per-user token** for whoever's request you're serving — don't reuse one person's token for another's task.

### Mapping requests → users

How you decide "which user is this request for" depends on your front end:

- **CLI / single operator**: pick the username explicitly per task.
- **A chat/IM front end**: map the inbound sender to a username (e.g. use the IM `sender_id` as the bridge username). Treating the authenticated sender identity as the bridge user — and refusing to act as anyone else — is the convention used in internal deployments. The point is the same everywhere: never drive a browser on behalf of someone who isn't the authenticated requester.

There's no per-token file the agent can pick "wrong" in a way that leaks data — the server backstops it — but matching the token to the real requester is what keeps the audit trail honest.

## Per-user permissions

`evaluate` and `network` are per-user flags (see `references/operations.md` → Permissions). Grant them per person; they don't apply server-wide.
