# Care Network Bot

Anonymous Telegram bot connecting people who want to talk with members of the
Care Network. Anonymity is maintained in both directions: the requester sees
their helper's track label ("Hearhtfelt Member" or "Peer Supporter"), and the
helper sees only "RHesident #1234".

## Features

- Anonymous queue system with per-track channel management (HF and PSS)
- Per-track timers: how long a request waits, and how long an idle conversation lives
- Restart durability: queued requests and in-flight conversations survive a redeploy
- Optional MongoDB storage for conversation history
- Queue cancellation and session management
- Graceful fallback to memory-only mode

## Setup

1. **Install:**
   ```bash
   pip3 install -r requirements.txt
   cp .env.example .env
   ```

2. **Configure:**
   - Add `BOT_TOKEN` and `ADMIN_CHANNEL_ID` to `.env`
   - Optionally add `MONGODB_URI` for persistent storage (enables dynamic admin updates)
   - (Optional) Update `DEFAULT_HEARTFELT_MEMBERS` in `config.py` as a fallback list when MongoDB is unavailable. Runtime admin updates happen via MongoDB, so you no longer need to edit the file or restart the bot.

3. **Run:**
   ```bash
   python3 main.py
   ```

## Bot Setup

**Telegram Bot:** Create via @BotFather, add token to `.env`  
**Admin Channel:** Private channel, add bot as admin with send/edit/delete permissions  
**Members:** Add support member Telegram IDs to the `heartfelt_members` MongoDB collection (via `python3 -m src.database.utils admins --action add --telegram-id ...`) and ensure they are in the admin channel

## How It Works

**Users:** `/chat` → describe issue → wait in queue → anonymous chat → `/end`  
**Support Members:** Monitor the track's channel → click "Claim" → anonymous chat → `/end`

**Commands:** `/start` `/chat` `/status` `/cancel` `/end`

`/help` is kept alive as an alias for `/chat` (posters and existing users still
reach for it), but `/chat` is the primary command and the only one advertised in
the Telegram command menu.

### Timers

| Track | Request waits in channel | Idle conversation closes | Warning sent |
|---|---|---|---|
| HF | 60 min | 30 min idle | 25 min idle (5 min lead) |
| PSS | 24 h | 24 h idle | 23 h idle (60 min lead) |

## Configuration

**.env file:**
```bash
BOT_TOKEN=your_bot_token_here
ADMIN_CHANNEL_ID=-1001234567890
MONGODB_URI=mongodb://localhost:27017/heartfelt_bot  # Optional
REGISTRATION_ADMIN_IDS=111111111,222222222  # optional; unset = /register disabled
```

`REGISTRATION_ADMIN_IDS` is a comma-separated list of Telegram **user** ids (positive
numbers; a negative id is a channel id and is rejected and logged at boot). Unset means
the `/register` feature is completely inert. Read once at import, so changing it needs a
container **recreate**, not a restart. See "Joining the support team" below.

**config.py:**
```python
DEFAULT_HEARTFELT_MEMBERS = [1522275008, 9876543210]
# Runtime authorized members are synced from the MongoDB `heartfelt_members` collection.
```

## MongoDB (Optional)

Stores conversation history and analytics. Works without it (memory-only mode).

**Usage:**
```bash
python3 -m src.database.utils stats                    # Session statistics
python3 -m src.database.utils monthly                  # This month's sessions  
python3 -m src.database.utils transcript --session-id  # Full conversation
python3 -m src.database.utils admins --action list     # View Heartfelt admins
python3 -m src.database.utils registrations --action list --status pending   # /register requests
python3 -m src.database.utils admins --action add --telegram-id 123456789
python3 -m src.database.utils admins --action remove --telegram-id 123456789
```

When the bot is running inside Docker, execute the same commands in the container:

```bash
docker exec heartfelt-bot python3 -m src.database.utils admins --action list
docker exec heartfelt-bot python3 -m src.database.utils admins --action add --telegram-id 123456789
docker exec heartfelt-bot python3 -m src.database.utils admins --action remove --telegram-id 123456789
```

Replace `heartfelt-bot` with your container name if it differs. Changes propagate automatically within the refresh interval configured by `AUTHORIZED_MEMBER_REFRESH_SECONDS` (default 60 seconds).

## Joining the support team

Share this link with prospective supporters:

```
https://t.me/hearhtfelt_companion_bot?start=register
```

Tapping it opens the bot and starts registration directly. `/register` is
deliberately **not** in the Telegram command menu -- that menu is what someone
who opened the bot in distress sees, and a recruitment prompt does not belong
beside "Request support". The link is the only discovery route, and it falls
back to the ordinary welcome while `REGISTRATION_ADMIN_IDS` is unset, so it
cannot reveal the feature before you switch it on.

Prospective supporters send `/register` to the bot in a private chat. The bot DMs
every id on `REGISTRATION_ADMIN_IDS` a card with the applicant's name, username and
Telegram id, and one Approve button per track plus "Not now". The first tap wins;
the others are told it has already been handled.

`/register` is deliberately **not** in the bot's command menu. That menu is the
surface a person in distress sees, and a recruitment command does not belong on it,
so discovery is out-of-band (a poster, a briefing, a committee handover).

Two things to know before switching it on:

- **Every admin id must have sent the bot a private message first.** Telegram
  forbids a bot messaging a user who has never messaged it, and there is no stored
  flag for admins -- the send attempt IS the check. If nobody can be reached the
  applicant is told honestly that the request was not submitted, and an ERROR naming
  the ids appears in `docker logs`.
- **Approval is only half the job.** An approved supporter can claim from their
  channel immediately, but has no display name, so they are invisible in the picker
  until somebody runs
  `admins --action set-profile --service pss --telegram-id <id> --display-name "<name>"`.
  That is deliberate, and it is the staged-rollout lever for the whole picker feature.

## Deployment

### Build & Push Docker Image

1. Ensure Docker Desktop/Engine is running and you are logged into your container registry (e.g. Docker Hub):
   ```bash
   docker login
   ```
2. Create an amd64-compatible image from the project root (works on both Apple Silicon and x86 hosts):
   ```bash
   # Build with a version tag (example v1.0.0). Replace USERNAME and VERSION as needed.
   docker buildx build --platform linux/amd64 -t USERNAME/heartfelt-bot:v1.0.0 --load .
   ```
3. Tag the image for your registry account (replace `USERNAME` and `VERSION` with your values):
   ```bash
   # Tag the versioned image and also create a 'latest' tag
   docker tag USERNAME/heartfelt-bot:v1.0.0 USERNAME/heartfelt-bot:latest
   ```
4. Push the image so the droplet can pull it:
   ```bash
   # Push both the versioned tag and the 'latest' tag
   docker push USERNAME/heartfelt-bot:v1.0.0
   docker push USERNAME/heartfelt-bot:latest
   ```

Simple one-liner: Build & push in one go (recommended for CI / multi-arch)

```bash
# Build and push a multi-arch image with a version tag and 'latest'.
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t USERNAME/heartfelt-bot:1.0.0 \
  -t USERNAME/heartfelt-bot:latest \
  --push .
```

### Deploy on DigitalOcean Droplet

1. SSH into DigitalOcean droplet (raffleshalldevs@gmail.com) as **root**
   ```bash
   ssh root@<DROPLET_IP>
   ```
2. Log in to your container registry (if the repo is private):
   ```bash
   docker login
   ```
3. Pull the newest image (replace `USERNAME` with your handle):
   ```bash
   docker pull USERNAME/heartfelt-bot:latest
   ```
4. Stop and remove the old container (this does **not** touch your image or `.env`):
   ```bash
   docker stop heartfelt-bot
   docker rm heartfelt-bot
   ```
5. Run the updated container, wiring in your existing `~/heartfelt-bot/.env`:
   ```bash
   docker run -d \
     --name heartfelt-bot \
     --restart unless-stopped \
     --env-file ~/heartfelt-bot/.env \
     felixlmao/heartfelt-bot:latest
   ```

6. View logs
   ```bash
   docker logs heartfelt-bot
   ```

## Testing

No pytest required -- every suite is a standalone script.

This is exactly the set of files the CI `test` job runs, in the same order, so
"run the tests locally" and "what the gate runs" can never diverge:

```bash
python tests/test_dependency_pins.py     # installed versions == requirements.txt pins
python tests/test_boot.py                # PTB API surface + main() boot ordering
python tests/test_timeutil.py            # aware-UTC helpers
python tests/test_service_config.py      # service registry + timer invariants
python tests/test_copy.py                # requester-facing copy guards
python tests/test_pss_flow.py            # full HF+PSS flow with a fake bot
python tests/test_member_profiles.py     # supporter profiles + availability rules
python tests/test_directed_requests.py   # the PSS directed-support flow
python tests/test_release_and_end.py     # requester-only /end, and /release
python tests/test_registration.py        # self-service /register + admin approval
python tests/test_per_service_timers.py  # per-track queue/session expiry
python tests/test_restore.py             # restart durability
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

On Windows, set `PYTHONIOENCODING=utf-8` first or the emoji in the test output
will raise `UnicodeEncodeError` from the console codec. CI sets it too.

Manual test flow: user sends `/chat` → describe issue → check the track's channel
→ claim → chat → `/end`

## Privacy Policy

We value your privacy. Please review our full privacy policy here:  
[Hearhtfelt Companion Privacy Policy](https://docs.google.com/document/d/1pWvutw151h_sypdttkwEH7hDiBwBdX-qF_xJypffn7Y/edit?usp=sharing)
