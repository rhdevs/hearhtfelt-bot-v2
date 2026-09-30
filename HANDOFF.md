# Handoff — Care Network Bot (formerly HeaRHtfelt Companion Bot)

_Last updated: 2026-09-30_

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
  roster holds the **test** id `8321402486` (Patrick's own account — NOT a real
  supporter) **and** Evan `534188521`, who is also on the HF default roster
  (`DEFAULT_HEARTFELT_MEMBERS` in `config.py`). Membership is supposed to be
  exclusive — see §8 — but this dual entry predates that rule and hasn't been
  cleaned up. `/available` / `/unavailable` only ever touch his HF record; his
  PSS row is a separate document with its own `available` flag.
- Because PSS is enabled, **all real users now see the two-button chooser.** This
  is a live mental-health helpline — a real user picking PSS with no supporter
  watching will time out. Keep test windows short.
- **After branch `feat/pss-specific-or-anyone` ships, `PSS_DIRECTED_ENABLED`
  defaults to `false`** (env opt-in; see R11). Merging or deploying that branch
  does NOT by itself change what a student sees: PSS keeps going straight to the
  channel exactly as it does today, test account and Evan's dual membership
  included, until an operator explicitly sets the env var and recreates the
  container.

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
`🔄 Rehydration [DRY RUN]: N pending restored (N choosing, N directed), N pending stale-closed, N pending skipped, N active restored, N active skipped`

Confirm those numbers are plausible. `pending restored` is the number of people
who would be brought back into the queue and, if already past their window, DMed.
The `(N choosing, N directed)` pair is a breakdown of that same `pending restored`
count, not additional people on top of it: `choosing` is how many were mid-pick of
a PSS supporter, `directed` is how many had already named one. If it is larger
than a handful, stop and investigate before going live. (`choosing` counts a
requester standing at the fork question — "a specific peer supporter" vs "any
available" — exactly the same as one already browsing the numbered list: both
are `routing:'choosing'` / `UserState.CHOOSING_SUPPORTER`, with no separate
state for the two; see R16.) Then:

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
the deploy. It runs the fourteen suites listed in §6 below, in that order.
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

**R8 — PSS follow-up copy is env-supplied and empty by default.**
`PSS_FOLLOWUP_NOTE` (appended to the SUPPORTER's closing message) and
`PSS_CLOSING_NOTE` (appended to the REQUESTER's closing message) default to `""`,
which makes every closing message byte-identical to today's. They are deliberately
NOT written in the repo: inventing clinical follow-up instructions for a
mental-health service is not a coding decision, and the stakeholder has not
supplied wording. When wording arrives, set them in `.env.production` on the
droplet — and note that `config.py` reads env at import, so the container must be
RECREATED, not merely restarted, for a change to take effect.

**R9 — `/end` is now REQUESTER-ONLY, on BOTH tracks, and `/release` is new. Tell
the HF leads BEFORE this ships.** Today any party can `/end`. After this, a
member who types `/end` gets a refusal pointing them at `/release`. This is a
behaviour change for every existing HF member, and they will hit it the first time
they try to close a conversation. `/release` hands the conversation back instead:
the requester keeps their place and gets someone else, rather than being closed out.

**R10 — supporters must press /start (or send the bot any private message) before
they can be chosen.** Telegram forbids a bot messaging someone who has never
messaged it. `has_started_bot` is recorded on **any** private interaction by a
PSS supporter — text, any command including `/name`, or a button tap in their own
chat with the bot — by the group `-1` `TypeHandler` hook `note_private_contact`
(`src/bot/handlers.py`), which runs before every other handler on every private
update. The same hook captures their Telegram first name (see R16).

Unset means they are never DMed. If they DO have a listed name (R16), they still
appear on the list — marked `(busy)`, not omitted — because "not reachable" is
just one of several reasons a supporter can't be directed to right now, and the
busy marker deliberately never says which one. A supporter with `has_started_bot`
unset AND no usable name is omitted from the list entirely (both conditions have
to fail to disappear from it).

The CLI third column and the set-started caveat are unchanged: check with
`python -m src.database.utils admins --action list --service pss` — the third
column reads `NOT SET` for anyone who has not started the bot. There is an admin
override (`--action set-started`) but it should be a last resort: it CLAIMS the
chat exists, and if it does not the directed request fails and the requester is
asked to choose again. Prefer asking the supporter to press /start or send `/name`.

**R11 — the fork is OFF until `PSS_DIRECTED_ENABLED=true`, and that env var is
now the ONLY rollout lever.** Names default automatically to a supporter's
Telegram first name (R16), so the old lever this section used to describe —
"no `display_name` set => invisible in the picker" — no longer holds. A
supporter needs no admin step and no override to appear; the moment they send
the bot any private message they have a listed name, which is exactly why the
env var, not name data, is what gates real students seeing the list.

**Behaviour while `PSS_DIRECTED_ENABLED` is false (the current default):** PSS
goes straight to the channel exactly as it does today. Any old picker button
still in flight (a restart mid-question, see R8/rehydration) shows only "Send
to anyone instead" / "Cancel", with no names — the kill switch in
`_render_picker` treats `directed_enabled=False` as zero listable entries
regardless of who is on the roster. No new directed DMs go out. A request
already sitting with a supporter (state `directed`) still resolves normally —
turning the flag off mid-flight does not strand anyone already claimed.

**The fork only appears when at least one listable supporter OTHER than the
requester exists** (active, has a usable name — R16) — on top of the env
check. An empty or all-requester roster still falls through to the plain
channel path, same as today.

**ENABLE procedure**, once the real PSS roster is ready:

1. Remove the test account:
   ```bash
   docker exec heartfelt-bot python -m src.database.utils admins \
     --service pss --action remove --telegram-id 8321402486
   ```
   and resolve Evan's dual HF/PSS membership (§4, §8) — decide whether he stays
   on PSS, HF, or is re-added to PSS deliberately once membership is exclusive
   again.
2. Add the real supporters:
   ```bash
   docker exec heartfelt-bot python -m src.database.utils admins \
     --service pss --action add --telegram-id <ID> --username <name>
   ```
3. Tell supporters, before flipping the switch: students will see their
   Telegram first name by default; `/name` lets them change or reset it;
   `/unavailable` shows them as `(busy)` on the list rather than hiding them.
4. Ask each supporter to send the bot any private message (a `/start`, any
   text, or `/name`) so `has_started_bot` and their Telegram first name get
   captured (R10, R16).
5. Check everyone is listable:
   ```bash
   docker exec heartfelt-bot python -m src.database.utils admins \
     --action list --service pss
   ```
   Every active supporter should show a listed name (`[telegram]` or
   `[chosen]`), not `- [no usable name]`. Fix any `[no usable name]` row by
   asking that supporter to send `/name <their name>`.
6. Add `PSS_DIRECTED_ENABLED=true` to `/root/heartfelt-bot/.env` and RECREATE
   the container — `docker restart` does NOT reload `--env-file`:
   ```bash
   ssh root@137.184.251.240
   ENV=/root/heartfelt-bot/.env
   echo 'PSS_DIRECTED_ENABLED=true' >> "$ENV"
   docker rm -f heartfelt-bot
   docker run -d --name heartfelt-bot --restart unless-stopped --env-file "$ENV" \
     ghcr.io/rhdevs/heartfelt-bot:latest
   ```
7. Verify the boot:
   ```bash
   docker logs --since 30s heartfelt-bot
   ```
8. Smoke-test from a throwaway account: `/chat` → PSS → describe → "A specific
   peer supporter", and confirm the list shows real names, not the test account.

**ROLLBACK:** delete the `PSS_DIRECTED_ENABLED` line (or set it `false`) in
`/root/heartfelt-bot/.env`, then recreate the container the same way (steps 6–7
above). Requests already directed to a supporter still resolve; new requests
go straight to the channel again, same as before the flag was ever set.

A supporter can change what they are shown as with `/name` (private chat,
active PSS roster only — R16), or an admin can set an override with
`python -m src.database.utils admins --action set-profile --service pss --telegram-id <id> --display-name "<name>" [--blurb "<one line>"]`
— `--display-name ""` resets it back to their Telegram first name.

**R12 — `/release` requires Mongo.** The request's description lives only on the
session document; `active_sessions` has never carried one. With Mongo down,
`/release` refuses with a clear message rather than handing back a request nobody
could be shown. If Mongo is down and a supporter genuinely cannot continue, the
conversation will idle-expire on its own timer.

**R13 (link: `https://t.me/hearhtfelt_companion_bot?start=register` -- the only discovery route; `/register` is not in the command menu by design) — `/register` is inert until `REGISTRATION_ADMIN_IDS` is set, and every id on
it must have pressed /start.** The env var is a comma-separated list of positive
Telegram **user** ids; a negative id is a channel id, is rejected, and is logged at
boot as an unusable entry (a channel would have been DMed the applicant's real name,
id and username). While it is unset the feature is completely inert: a disabled
`/register` answers with the same neutral string the bot sends for **text** it
does not understand, and every approval button is refused. This is NOT the same
neutral string as R16's `/name`, and the distinction matters: an unrecognised
`/command` gets **no reply at all** (main.py registers no handler for it, and
`handle_message` is filtered on `~filters.COMMAND` — see R16 and §8), so
`/register`, `/available` and `/unavailable` are in fact distinguishable from an
unknown command by a determined prober, even though they read as "nothing
happened" to an ordinary user. Telegram forbids a bot messaging a user who has never
messaged it, and unlike supporters there is **no `has_started_bot` record for admins**
— the send attempt *is* the check, which is why it can never go stale. Before
switching it on: have each admin send the bot any private message, then run one
`/register` from a throwaway account and confirm **both** admins receive the card. If
nobody is reachable the applicant is told honestly that it was not submitted, and an
ERROR naming the ids appears in `docker logs`. Env-only, read at import → **recreate**
the container, do not just restart it.

**R14 — first tap wins, and it is Mongo-atomic.** `decide_registration` is a single
`find_one_and_update` filtered on `status: 'pending'`, so of two simultaneous taps
exactly one gets a document back and only that one writes a roster or messages the
applicant. A second tap is answered "already handled" and the applicant is never
messaged twice — including the Approve-then-Reject case, where the loser would
otherwise send a rejection to somebody who had just been approved. Every admin's card
is then rewritten to a settled state with its buttons stripped, from the
`(admin_id, message_id)` pairs stored on the row, so this still works after a
redeploy. With Mongo down nothing is decided and the buttons are deliberately left
live, so the same tap works once it is back.

**R15 — approval makes a supporter claim-authorized immediately, and (once R11
is on) listed by name with no admin step.** They can claim from their track's
channel the moment they are approved — that part is unchanged and does not
depend on `PSS_DIRECTED_ENABLED`. The PSS approval DM (`registration_approved_named`
on a name-listed track) mentions `/name` directly, so a new supporter knows
from their very first message that they control what students see.

Once the fork is on for that track (R11), they are listed under their
Telegram first name from their **next private message** — there is nothing an
admin needs to do for the ordinary case; the capture point is the same
`note_private_contact` hook described in R10/R16.

An admin override still works and is validated with the same rules `/name`
itself enforces (R16's rule list — length, character set, no lookalike
collision, and so on):
```bash
python -m src.database.utils admins --action set-profile --service pss --telegram-id <id> --display-name "<name>"
```
`--display-name ""` resets it back to their Telegram first name. The old
justification for this override — that a supporter's self-chosen string was
otherwise unvetted — is now handled by validation rather than by admin
gatekeeping; the override exists for convenience (an admin setting someone up
before they've messaged the bot), not because unvalidated names are a risk.

Audit and close commands are unchanged: `registrations --action list --status
approved` (the row shows `decided_by`); `registrations --action close` clears a
row nobody can act on. There is deliberately **no CLI approve**: it would
bypass the atomic gate and the applicant notification. To add somebody
out-of-band use `admins --action add`, then `registrations --action close`.

**R16 — `/name` and the name students see.**

**Who can use it:** active PSS supporters (any track with `supporter_names=True`,
currently just PSS), in a private chat with the bot, only. Everyone else —
students, HF-only members, deactivated or removed members, groups, channels —
gets **no reply at all**, identical to any unrecognised command. This is
deliberately stricter than R13's `/register`/`/available`/`/unavailable`, which
DO reply (with the same neutral "unknown command" text) when refused: `/name`
replies with nothing, because the bot has no unknown-command handler at all
(main.py registers no fallback for an unmatched `/command`, and the plain-text
`MessageHandler` is filtered on `~filters.COMMAND`), so a silent `/name`
handler is indistinguishable from `/name` not existing. `tests/test_boot.py`
guards that premise directly (it asserts the handler set is exactly the ones
listed and that no catch-all command handler exists) — if a catch-all is ever
added, `/name` must be routed through it or this guarantee breaks silently.

**Usage:**
- `/name` — shows the name currently shown to students, and how to change or
  reset it.
- `/name <name>` — sets an override.
- `/name reset` — clears the override, back to the Telegram first name.

**Validation rules** (enforced by `src/supporter_names.name_problem`, checked
in this order — the first violation wins, so the reply is always the most
specific, most actionable one):
1. One line — no newline, carriage return or other line-break character.
2. No invisible, zero-width, bidi-override or other formatting/control/
   surrogate/private-use/unassigned characters.
3. 1–32 characters after collapsing whitespace (leading/trailing trimmed,
   internal runs collapsed to one space).
4. No `@` (can't imitate a username or email).
5. Nothing that looks like a web address (`http://`, `www.`, or a
   letter/digit + `.` + 2+ letters — put a space after a full stop to avoid
   this, e.g. "Dr. Who" not "Dr.Who").
6. No run of 5+ digits (optionally separated by spaces/dots/hyphens/
   apostrophes) — can't look like a phone number.
7. Letters, numbers, spaces, emoji/symbol characters and only `- ' .` as
   punctuation (parentheses are deliberately excluded — that's what makes the
   " (2)" collision suffix unforgeable). Combining-mark stacks of 3+ are
   rejected as Zalgo text.
8. Not purely numeric (people pick from the list by typing a number).
9. At least one letter.
10. Not a word or button label the bot itself uses (`RESERVED_NAME_KEYS` —
    e.g. "busy", "cancel", "anyone").
11. Not the same as, or a lookalike of (case/whitespace/accent-insensitive,
    with common homoglyph folding — Cyrillic/Greek look-alikes, 0/o, 1/l,
    "rn"/"m", "vv"/"w"), another active PSS supporter's current listed name.
    This check is roster-aware and lives in `config.name_taken_by_other`, not
    in `name_problem` itself (`src/supporter_names.py` is deliberately pure —
    no `config` import — so its rules stay independently testable).

Each rejection gets a specific, friendly message (the `name_*` keys in
`MESSAGES`) — never a generic "invalid name".

**Collisions** between two supporters who end up with the same listed name
(automatic-vs-automatic, automatic-vs-chosen, or chosen-vs-chosen) are shown
as "Alex (1)", "Alex (2)", ... in ascending Telegram-id order
(`src.supporter_names.disambiguate`), computed fresh over active members every
time a list is rendered — never stored.

**No usable name:** a supporter with neither a captured Telegram first name
nor a `/name` override is left off the picker list entirely (not shown as
"(busy)" — this is the one case that omits rather than marks). Their id is
logged at INFO on every render that would otherwise have included them
(`omitted_supporters`).

**Logging:** every name change — automatic capture or `/name` — is logged at
INFO with the supporter's Telegram id and the old and new name (`old first ->
new first (listed as old listed -> new listed)`; see `_record_first_name_on`
and `_write_display_name` in `src/bot/handlers.py`).

**Persistence — `peer_supporters` fields:**
- `telegram_first_name`, `telegram_first_name_at` — captured automatically,
  refreshed on every private contact.
- `display_name` — the `/name` override (or admin `set-profile` override); the
  chosen name if set, otherwise absent/empty.
- `display_name_set_at` — when the override was last changed.

**No opt-out from the list except deactivation.** There is no per-supporter
"don't list me" flag; `/unavailable` marks a supporter `(busy)` on the list
(still visible, still not pickable), it does not remove them from it. Only an
admin deactivating the member (`admins --action deactivate`) removes them from
the roster the list is built from. See §8 — this is a known, not-yet-decided,
open item.

**Roster-refresh staleness window applies here too.** After a deactivation (or
an `/available`/`/unavailable` toggle), the in-memory roster can be up to 5
minutes stale (`AUTHORIZED_MEMBER_REFRESH_SECONDS`) before it reflects the
change on the picker list — same caveat as R10. `/name set` and `/name reset`
are NOT subject to this in the other direction: they always check Mongo first
(`_write_display_name`), so a stale in-memory roster can never let a `/name`
change through for someone the database says is no longer an active member —
it's refused (silently, the same "missing" path as a non-member) rather than
partially applied.

**Every reason for "not free" reads the same.** Picking a busy entry (a stale
button, or typing the number of a `(busy)` row) always replies
`"{name} isn't free right now. Please choose someone else, or send your
request to anyone instead."` — whether the reason is `/unavailable`, an
already-in-progress conversation, an already-held directed request, a pending
ask that hasn't resolved yet, or simply an unreachable chat (`has_started_bot`
unset/stale). The requester is never told which.

**"Send to anyone" is safe to tap twice.** A repeat or stale tap on
"Any available peer supporter" / "Send to anyone instead" (a double tap, or
an older fork/list/next-step message) tells the student where their request
stands (`queue_status`) and never posts to the channel twice.
`route_to_open_queue` returns a string outcome, not a bool, precisely so
that "already open" can't be mistaken for a fault: `channel_error` is
reserved for a real channel-post failure, and in that case the buttons are
left live so the same tap retries. On success the fork's keyboard is
stripped. A stale "A specific peer supporter" / "Choose someone else" button
on a request that's already in the channel shows the same status instead of
a list, so it can never invite a choice that can no longer happen.

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
python tests/test_member_profiles.py     # supporter profiles + availability rules
python tests/test_supporter_names.py     # supporter names: rules, labels, capture, /name
python tests/test_directed_requests.py   # the PSS directed-support flow
python tests/test_release_and_end.py     # requester-only /end, and /release
python tests/test_registration.py        # self-service /register + admin approval
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

### Branch `feat/pss-specific-or-anyone` (NOT pushed)

Adds the "specific or anyone" fork to the PSS track (HF untouched, byte-identical)
and supporter `/name`: after describing their request, a PSS requester now
chooses "A specific peer supporter" (a numbered, deterministically-ordered list
of every active PSS supporter with a usable name, busy ones marked `(busy)` and
unpickable) or "Any available peer supporter" (the channel, exactly as before).
A supporter's listed name defaults to their Telegram first name, captured
automatically from their own private messages (no admin step), and they can
change or reset it themselves with `/name` — silent to everyone who isn't an
active PSS supporter in a private chat, indistinguishable from an unrecognised
command. The whole fork is gated by `PSS_DIRECTED_ENABLED`, which defaults to
**false**, so merging and deploying this branch changes nothing a student sees
until an operator opts in (R11 has the enable/rollback runbook). See R10, R11,
R13, R15 and the new R16 above for the full behavioural detail.

```bash
git log --oneline 38673d9..HEAD
```

---

## 8. Open items / next steps

1. **Real PSS rollout:** replace the test channel `-1004439634374` and test member
   `8321402486` with the real PSS channel + real supporter Telegram IDs. Steps:
   set `PSS_CHANNEL_ID` to the real channel, add real ids via the `--service pss`
   CLI, remove the test id, ensure supporters are in the PSS channel. No code
   change needed.
2. ~~**Membership model decision (pending user):**~~ **DONE.** Self-service
   `/register` shipped (R13–R15): people request PSS/HF membership themselves,
   an admin approves via DM buttons, and an approved supporter is
   claim-authorized immediately.
3. **Turn PSS back off** after testing if the real roster isn't ready (see §5).
   Note this is now separate from R11's `PSS_DIRECTED_ENABLED`: turning PSS
   itself off (`PSS_ENABLED`/`PSS_CHANNEL_ID`) removes the track entirely;
   turning `PSS_DIRECTED_ENABLED` off just drops the fork back to the plain
   channel while PSS keeps running.
4. **Minor:** the deploy workflow uses actions pinned to Node 20 (GitHub
   deprecation warning). Bump `actions/checkout`, `docker/*` versions when
   convenient.
5. **Cosmetic:** startup logs a one-time `No members configured for 'pss'` warning
   before the DB roster loads — harmless ordering artifact.
6. **Decide whether `/available`, `/unavailable` and `/register` should also be
   silent to non-members,** matching `/name`'s "no reply at all" behaviour
   (R16), rather than replying with the neutral unknown-command string (R13).
   Left as-is for this branch: changing it is a behavioural change to those
   three commands for people who currently get *some* response, and wasn't in
   this feature's scope.
7. **`pk_s:<id>` callback data carries a supporter's Telegram id in plaintext**
   (pre-existing, `CB_PICK_SELECT`, `src/bot/handlers.py`). Anyone who can read
   a requester's Telegram client can see which internal id they tapped. Not
   new to this branch, but the specific-supporter picker is the first feature
   that puts a supporter id on a button a requester holds.
8. **Evan's dual HF/PSS membership** (§4) predates the "membership is
   exclusive" rule and hasn't been reconciled. Decide whether he stays HF-only,
   PSS-only, or genuinely both (which would need the exclusivity assumption
   documented elsewhere in this file, e.g. §3, revisited).
9. **Supporters cannot opt out of the picker list** once `PSS_DIRECTED_ENABLED`
   is on, short of an admin deactivating them (R16). There is no personal
   "don't list me by name" toggle — `/unavailable` still shows them as
   `(busy)`. Whether that's the right long-term answer, or PSS needs its own
   opt-out separate from HF's availability model, is undecided.

---

## 9. Memory

Durable facts are in the session memory palace (`MEMORY.md` index): see
`deployment-topology`, `hf-pss-umbrella`, `repo-vs-prod-drift`,
`bot-token-leaks-in-logs`.
