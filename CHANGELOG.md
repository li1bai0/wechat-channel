# Changelog

## 2.2.2 - 2026-08-31

- Remove the maintainer's private identity from public default prompts; deployments can keep personal behavior in an ignored `persona.json`.
- Ignore credentials, sessions, logs, QR codes, private personas and agent-local notes; remove historical working notes from the release tree.

## 2.2.1 - 2026-08-31

- Route greetings and acknowledgements through the low-reasoning model so replies remain context-aware; fixed local phrases are now reserved for service fallbacks.
- Inject shared memory only when starting a model session instead of repeating the full memory on every turn.

## 2.2.0 - 2026-08-31

- Four-level zero-latency routing: greeting, normal question, complex task, and long task.
- Context-aware immediate acknowledgement for task lanes; greetings no longer wait for a model turn.
- First silence fallback now appears after 12 seconds (the previous timer accidentally delayed it to 60 seconds).

## 2.1.1 — 2026-08-28

- Live iLink send response verified: a successful response may contain only `message_id`, with no `ret` or `errcode`. Accept this success form; explicit error codes still take precedence. Add regression cases for success, null ID and ID-with-error.

## 2.1.0 — 2026-08-28

- `version` / `--version`: version, build date and source fingerprint; deployed copies also show commit.
- `check-update`: query public GitHub tags, never auto-download or install.
- Default event logging is quiet; `debug_events` prints event type/request ID, not raw model payloads.
- Rotate bridge.log at 5 MiB with three backups.
- Persist last send attempt ID/status and validate both ret and errcode. API acceptance is NOT handset delivery/read confirmation. Network timeout is `unknown`; existing retry queue can produce duplicates after an ambiguous timeout.
- Recheck rate-limit capacity after every wait (concurrent senders cannot all claim one slot).
- Dedicated configurable app-server port (default 38125), optional independent CODEX_HOME, explicit helper WebSocket URL and startup error log.
- Migrate content fingerprints to a five-minute window, tolerate old stored strings, isolate per-message handling exceptions and use reentrant state locking.
- Local persona/configuration stays outside the public source. Clean-checkout deployment copies only code, records commit/hash and keeps recoverable backups.
- Offline tests use a temporary data directory; no production WeChat tokens or network calls.

Not included: speech transcription, group chat, per-contact inbound throttling, semantic classifier, unattended upgrades or handset receipts. Existing classifiers/queues remain in place.
