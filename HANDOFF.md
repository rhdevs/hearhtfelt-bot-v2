# Handoff — Care Network Bot (formerly HeaRHtfelt Companion Bot)

_Last updated: 2026-08-26_

Quick-start context for the next session. This bot is an anonymous Telegram
support helpline. An earlier session migrated its deployment to a self-owned
pipeline and added a second support track (PSS) alongside the existing HF track
under one "umbrella" bot. The `feat/care-network-phases-1-3` branch then
rebranded the umbrella to "Care Network", moved the timers onto each track, and
made the bot survive a restart. See §5b for the runbook items that branch adds.

---

## 1. What this bot is

- **One Telegram bot**, one entry point. A user sends `/chat` (`/help` is kept
  alive as an alias) and, when more than one track is enabled, picks **HF**
  ("friendly listening ear") or **PSS** ("trained peer supporters"). The bot then
  queues their request to that track's channel where an authorized member claims
  it and chats 1:1, fully anonymized.
- Requesters never register — they just `/chat`. Only **members** (HF/PSS) are
  pre-authorized.
- Python `python-telegram-bot==22.2`, MongoDB (Atlas) optional-but-used.

---

## 2. Deployment & CI/CD (self-owned as of this session)

- **Repo:** `github.com/rhdevs/hearhtfelt-bot-v2`, branch `main`.
- **Image:** `ghcr.io/rhdevs/heartfelt-bot:latest` (private GHCR package).
- **Droplet:** `root@137.184.251.240` (DigitalOcean, Docker). Container name
  `heartfelt-bot`, `--restart unless-stopped`, env from
  `/root/heartfelt-bot/.env`.
- **Pipeline:** `.github/workflows/deploy.yml` — on pull request: tests only; on
  push to `main`: **tests →** build → push GHCR → SSH to droplet →
  `deploy/deploy.sh` (pull, swap container w/ health check + auto-rollback).
  **To deploy: just push to `main`.** A red test job blocks the deploy; there is
  no way to deploy a commit whose tests failed except `workflow_dispatch`, which
  also runs them.
  Watch: `gh run watch -R rhdevs/hearhtfelt-bot-v2`.
- **GitHub secrets:** `DROPLET_HOST`, `DROPLET_USER`, `DROPLET_SSH_KEY`
  (dedicated ed25519 deploy key labelled `github-actions-deploy@heartfelt-bot`,
  installed in droplet root `authorized_keys`; revocable).
- **Predecessor:** old opaque `felixlmao/heartfelt-bot` Docker Hub image is still
  on the droplet as an emergency rollback only.
- **Secrets:** never in the repo. Prod values live in the droplet `.env` and a
  local gitignored `.env.production`. `.gitignore` covers `.env*` and
  `_deployed_reference/`.

⚠️ The repo was previously **behind** what was deployed; it was reconciled this
session (commit `6db425e`). Always trust the running image over old assumptions.

---

## 3. The HF/PSS umbrella architecture

Central idea: a **service registry** makes the whole bot "service-aware" so HF
and PSS are two configured instances of the same machinery.

- `config.py` → `SERVICES: Dict[str, Service]` with keys `"hf"` / `"pss"`. Each
  `Service` carries: `channel_id`, `roster` (AuthorizedMembersStore),
  `members_collection`, `member_label`, `anon_prefix`, `request_title`,
  `enabled`, and a `runnable` property (`enabled and channel_id`).
  - Helpers: `get_service(key)`, `enabled_services()`, `default_service_key()`,
    `is_member_of_service(uid, key)`, `is_any_member(uid)`.
  - `HEARTFELT_MEMBERS is SERVICES["hf"].roster` (same object — the refresh loop
    mutates what the code reads). `is_heartfelt_member` is now a union shim.
- Queue entries and Mongo session docs carry a **`service`** field (default
  `"hf"` for legacy/missing). Claim auth checks the entry's roster; anonymous
  labels resolve per-service; requester copy (`queue_status`, `queue_expired`) is
  templated with `{member}`.
- **Landing-page chooser** is shown only when `len(enabled_services()) > 1`;
  with a single track it's skipped, so HF behaves exactly as before.
- Membership is **admin-managed and exclusive** (a person is HF or PSS, not both).
  Collections: `heartfelt_members`, `peer_supporters`. Roster auto-refreshes from
  DB every 5 min (`AUTHORIZED_MEMBER_REFRESH_SECONDS`).

Key files: `config.py`, `src/bot/handlers.py`, `src/bot/managers/{queue,session,expiry}.py`,
`src/database/{manager,connection,utils}.py`, `main.py`.

---

## 4. Current LIVE state (as of handoff)

Both tracks are enabled on the **production** bot:

| Track | Channel | Members |
|-------|---------|---------|
| HF | `-1002825528485` ("HeaRHtfelt Companion Queue") | 14 (real) |
| PSS | `-1004439634374` ("HeartfeltPeer Test") | 1 — **test only** |

- **PSS is in TEST mode.** `.env` on the droplet has
  `PSS_CHANNEL_ID=-1004439634374` and `PSS_ENABLED=true`. The `peer_supporters`
  roster holds a single **test** id `8321402486` (Patrick's own account — NOT a
  real supporter).
- Because PSS is enabled, **all real users now see the two-button chooser.** This
  is a live mental-health helpline — a real user picking PSS with no supporter
  watching will time out. Keep test windows short.

---

## 5. Operational runbook

**Manage a roster** (running bot picks up changes within ~5 min):
```bash
ssh root@137.184.251.240
docker exec heartfelt-bot python -m src.database.utils admins \
  --service pss --action add --telegram-id <ID> --username <name>
# actions: list | add | deactivate | remove ; --service hf|pss
```

**Enable/disable PSS is env-only.** `docker restart` does NOT reload `--env-file`;
you must recreate the container:
```bash
ssh root@137.184.251.240
ENV=/root/heartfelt-bot/.env
# disable PSS:
sed -i '/^PSS_CHANNEL_ID=/d; /^PSS_ENABLED=/d' "$ENV"
docker rm -f heartfelt-bot
docker run -d --name heartfelt-bot --restart unless-stopped --env-file "$ENV" \
  ghcr.io/rhdevs/heartfelt-bot:latest
docker logs --since 30s heartfelt-bot   # verify enabled services
```

**Get a user's Telegram ID:** they message `@userinfobot`, or `/start` the bot
and read it server-side.

---

## 5b. Runbook items from the Care Network phases

**R1 — rename the bot in BotFather (MANUAL, not code).**
The display name "HeaRHtfelt Companion Bot" should become "Care Network Bot" to
match the in-bot copy. Do it in @BotFather → /mybots → Edit Bot → Edit Name. The
`@username` does NOT change, so existing links and posters keep working. There is
no code path for this and no script; it is a human step.

**R2 — check for pre-existing orphaned sessions BEFORE deploying.**
The P0 storm (fixed in `9d4b55a`) means any `status:'active'` document left behind
by an earlier restart has been re-notifying its two participants every 3 minutes.
Count them first:
```bash
# in mongosh against the production database
db.sessions.countDocuments({status: 'active'})
db.sessions.countDocuments({status: 'pending'})
```
The pending count is expected to be large: until this branch, neither queue expiry
nor `/cancel` ever closed its Mongo row, so the collection holds a long tail going
back months. That is exactly what `STALE_NOTIFY_GRACE_MINUTES` protects against.

You do NOT need to close the orphaned `active` rows by hand. The first boot closes
them for you, and — since the stale-horizon fix — closes them **silently**: any
session idle for longer than its own window plus `STALE_NOTIFY_GRACE_MINUTES`
(HF: 30 + 120 = 150 min) is ended in Mongo with `end_reason:'idle_expired'` and
neither party is messaged. Count them first anyway, so you can check the number
against the boot log afterwards.

**R3 — PSS has no privacy policy of its own yet.**
`PSS_PRIVACY_POLICY_URL` falls back to the umbrella HF document. No URL was
invented. When a real PSS policy exists, set `PSS_PRIVACY_POLICY_URL` in the
droplet `.env` — no code change and no redeploy of the image is needed, just a
container recreate (see §5: `docker restart` does not reload `--env-file`).

**R4 — the FIRST deploy of restart durability MUST be a dry run.**
Rehydration reads every `pending` and `active` row at boot. On the first boot that
set includes months of never-closed pending rows. Run it in a low-traffic window:

> ⚠️ **`RESTORE_DRY_RUN=true` does not make the boot read-only.** It governs
> rehydration only. The two expiry sweeps in step 4 of `main()` run on every boot
> regardless of it, and the session sweep's DB-fallback branch still closes stale
> `status:'active'` rows in Mongo. That is deliberate — it is what remediates R2 —
> but do not read "dry run" as "touches nothing".
>
> The same caveat applies to the other two switches, and it used to be far worse:
> `RESTORE_ENABLED=false` and a tripped `RESTORE_MAX_*` circuit breaker both leave
> `active_sessions` empty, which makes *every* ancient row look like an orphan to
> that fallback branch. Before the stale horizon existed, turning a safety switch
> **on** strictly increased the number of people DMed — 60 orphaned rows and a
> tripped circuit breaker sent 120 "your conversation has been closed" messages.
> The horizon now caps all of these at zero, but the switches still are not a
> global "do nothing" flag.

```bash
ssh root@137.184.251.240
ENV=/root/heartfelt-bot/.env
echo 'RESTORE_DRY_RUN=true' >> "$ENV"
docker rm -f heartfelt-bot
docker run -d --name heartfelt-bot --restart unless-stopped --env-file "$ENV" \
  ghcr.io/rhdevs/heartfelt-bot:latest
docker logs --since 2m heartfelt-bot | grep Rehydration
```

The log line reads:
`🔄 Rehydration [DRY RUN]: N pending restored, N pending stale-closed, N pending skipped, N active restored, N active skipped`

Confirm those numbers are plausible. `pending restored` is the number of people
who would be brought back into the queue and, if already past their window, DMed.
If it is larger than a handful, stop and investigate before going live. Then:

```bash
sed -i '/^RESTORE_DRY_RUN=/d' "$ENV"
docker rm -f heartfelt-bot
docker run -d --name heartfelt-bot --restart unless-stopped --env-file "$ENV" \
  ghcr.io/rhdevs/heartfelt-bot:latest
```

Escape hatches, all env-only: `RESTORE_ENABLED=false` turns rehydration off
entirely (but see the warning above — it does not stop the expiry sweeps);
`RESTORE_MAX_PENDING` / `RESTORE_MAX_ACTIVE` (default 50 each) abort rehydration
if Mongo hands back more rows than expected; `STALE_NOTIFY_GRACE_MINUTES`
(default 120) is the horizon past which a row is closed silently instead of
notifying anyone. That last one now applies to BOTH sides: a pending request older
than `queue_expire_minutes + grace`, and a conversation idle longer than
`session_timeout_minutes + grace`. It is the single knob governing "how long after
the fact is a notification still kind rather than confusing", so raising it makes
the bot chattier about old state and lowering it makes it quieter.

**R5 — tell Ops and the HF leads about the timer change before it ships.**
An HF conversation used to warn at 5 minutes idle and close at 10. It now warns at
25 and closes at 30. Companions will be held in a conversation three times longer
before it auto-closes.

| Track | Request waits in channel | Idle conversation closes | Warning fires at |
|---|---|---|---|
| HF | 60 min | 30 min idle | 25 min idle (5 min lead) |
| PSS | 24 h | 24 h idle | 23 h idle (60 min lead) |

`SESSION_SWEEP_SECONDS` stays at 180 and must NOT be lengthened: the binding
constraint is the shortest warning band (HF's 5 minutes), not the longest timeout.
A longer sweep could skip the band entirely and kill a conversation with no
warning. `tests/test_service_config.py` asserts the invariant.

**R6 — CI now gates deploys.**
The `test` job in `.github/workflows/deploy.yml` runs first, and
`build-and-push` (and therefore `deploy`) `needs: test`, so a red suite blocks
the deploy. It runs the nine suites listed in §6 below, in that order.
`tests/test_db_integration.py` is deliberately excluded: it needs a live Mongo
and it **writes documents** — there must never be a `MONGODB_URI` secret in the
test job. It also now refuses to run at all unless `ALLOW_DB_INTEGRATION_TEST=1`
is set, exiting non-zero so a refusal cannot be mistaken for a pass.
`tests/demo_session_expiry.py` is also excluded: it has zero
assertions and always exits 0. By design, no secrets are available to the
`test` job (`permissions: contents: read`, zero `secrets.` references in that
job), so a fork PR can run the gate safely.

**R7 — things an adversarial review of R6 found. Do not undo these.**

- **`tests/test_db_integration.py` writes to whatever `MONGODB_URI` resolves to,
  and its opt-in guard only covers `python tests/test_db_integration.py`.** The
  guard is inside `main()`. The file is named `test_*.py`, so `pytest` collected
  its three module-level `test_*` functions and ran them without ever entering
  `main()`; the file has zero `assert`s, so pytest reported "3 passed" while
  inserting documents. A maintainer with the production URI in `.env` typing the
  reflex command `pytest` was one keystroke from writing fabricated sessions into
  the live helpline. The checks are now named `check_*` and the module sets
  `__test__ = False`. **Never give anything in that file a `test_` prefix**, and
  always pass `MONGODB_URI=` explicitly on the command line (§6) — without it
  `load_dotenv()` silently supplies your `.env`, which is usually production.

- **A suite that discovers no tests used to pass.** Five suites build their test
  list with `sorted(globals())` and reported `All 0 tests passed!` with exit 0
  when discovery collected nothing. Each now asserts a minimum count. If you add
  tests you may raise the number; **never lower it to make a run go green.**

- **Concurrency must be scoped by event, not by ref.** `workflow_dispatch` runs on
  any branch and reaches `deploy`, so a ref-scoped group let a dispatch deploy and
  a main deploy run at the same time against one droplet. `deploy/deploy.sh` is
  not concurrency-safe (it renames the live container aside and `rm -f`s the
  rollback target), and two live containers means Telegram getUpdates 409s and
  dropped messages. Everything that can deploy shares one lane; only
  `pull_request` runs are per-ref.

- **Assert identity, not counts.** The boot suite counted nine handlers and
  checked command names, so rewiring `filters.PHOTO` to `handle_sticker` stayed
  green. It now asserts the ordered (handler class, callback name) pairs.

---

## 6. Tests

Run from repo root (needs `python-dotenv`, `python-telegram-bot`, `pymongo`).
This is exactly the set of files the CI `test` job runs, in the same order, so
"run the tests locally" and "what the gate runs" can never diverge:
```bash
python tests/test_dependency_pins.py     # installed versions == requirements.txt pins
python tests/test_boot.py                # PTB API surface + main() boot ordering
python tests/test_timeutil.py            # aware-UTC helpers
python tests/test_service_config.py      # registry/parity + timer invariants
python tests/test_copy.py                # requester-facing copy guards
python tests/test_pss_flow.py            # full HF+PSS flow w/ fake bot (no DB)
python tests/test_per_service_timers.py  # per-track queue/session expiry
python tests/test_restore.py             # restart durability (the important one)
python tests/test_session_expiry.py      # warn/expire lifecycle
```
Not run by CI:
```bash
python tests/demo_session_expiry.py      # demo, no assertions, not in CI
# WRITES DOCUMENTS. Set MONGODB_URI explicitly on the command line: without it
# config.py's load_dotenv() supplies whatever is in your .env, which on a
# maintainer's machine is usually PRODUCTION. Never omit it, never point it at prod.
ALLOW_DB_INTEGRATION_TEST=1 MONGODB_URI=mongodb://localhost:27017 python tests/test_db_integration.py
```
Run these from a virtualenv built with `pip install -r requirements.txt`;
`tests/test_dependency_pins.py` will tell you if you have not (it fails
deliberately on a mismatched environment).

On Windows set `PYTHONIOENCODING=utf-8` first, or the emoji in the output raise
`UnicodeEncodeError` from the console codec. CI sets it too.
`tests/test_pss_flow.py` proves: chooser, per-service channel routing,
cross-roster claim rejection, correct labels, HF/PSS in parallel.

---

## 7. Commits

### Branch `feat/care-network-phases-1-3` (NOT merged, NOT pushed)

**25 commits.** Do not maintain the list by hand — it was stale within a day of
being written, still showing five commits ending at `46f7bfc` when the branch had
twenty-five. Get the current set with:

```bash
git log --oneline fdb6079..HEAD        # fdb6079 is the merge-base with main
```

In four groups, oldest first:

1. **Phases 0–3** (`9d4b55a`, `8d7a07c`, `349f4c1`, `b5f5a77`, `46f7bfc`) —
   the P0 re-notify fix, aware-UTC, the Care Network rebrand with `/chat`,
   per-track queue/session expiry, restart durability.
2. **Review round 1 — correctness** (`d1499e4`..`f6c1022`) — boot-time expiry no
   longer mass-DMs months-old sessions, tracebacks kept on boot failure, tasks
   drained on shutdown, `end_session` no longer reports failure after the close
   commits, a claim can no longer be expired out from under itself.
3. **Review round 2 — the CI gate and test integrity** (`1133c99`..`cc0fedc`) —
   dependency-pin and boot-ordering suites, the unittest scaffolding that let six
   async tests assert nothing removed, `manual_test_expiry` renamed to
   `demo_session_expiry` and made truthful, the deploy gated on the suite,
   `test_db_integration` given an opt-in guard.
4. **Review round 3 — adversarial review of round 2** (`5bc9423`..`01bf3b8`) —
   see §5b R7. pytest could collect and run the database-writing integration
   script; a partial rebrand of the welcome message passed every suite; five
   suites reported success when they discovered zero tests; two boot cases hung
   for the whole CI budget instead of failing; handler callback identity was
   unchecked; and the ref-scoped concurrency group let two deploys overlap.

Read §5b before deploying any of it.

### Earlier session (on `main`)

- `6db425e` — Sync repo with deployed production baseline
- `6a36334` — Add self-owned CI/CD (GHCR + deploy workflow)
- `4e711cb` — Phase 1: service-aware HF/PSS umbrella (HF byte-identical)
- `fdb6079` — Template requester-facing copy per service + PSS flow test

Bonus security fix (in `4e711cb`): httpx log level raised + error-handler token
scrub → BOT_TOKEN no longer leaks into `docker logs`.

---

## 8. Open items / next steps

1. **Real PSS rollout:** replace the test channel `-1004439634374` and test member
   `8321402486` with the real PSS channel + real supporter Telegram IDs. Steps:
   set `PSS_CHANNEL_ID` to the real channel, add real ids via the `--service pss`
   CLI, remove the test id, ensure supporters are in the PSS channel. No code
   change needed.
2. **Membership model decision (pending user):** currently admin-managed. User is
   considering whether they want a **self-service `/register` flow** (people
   register themselves as PSS members, likely with an approval step). Not built
   yet — would be a new feature.
3. **Turn PSS back off** after testing if the real roster isn't ready (see §5).
4. **Minor:** the deploy workflow uses actions pinned to Node 20 (GitHub
   deprecation warning). Bump `actions/checkout`, `docker/*` versions when
   convenient.
5. **Cosmetic:** startup logs a one-time `No members configured for 'pss'` warning
   before the DB roster loads — harmless ordering artifact.

---

## 9. Memory

Durable facts are in the session memory palace (`MEMORY.md` index): see
`deployment-topology`, `hf-pss-umbrella`, `repo-vs-prod-drift`,
`bot-token-leaks-in-logs`.
