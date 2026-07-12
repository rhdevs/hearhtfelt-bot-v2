# Heartfelt Anonymous Support Bot

Anonymous Telegram bot connecting users with support members. Complete anonymity maintained - users see "HeaRHtfelt Member", support members see "RHesident #1234".eaRHtfelt Companion Bot

Anonymous Telegram bot connecting users with support members. Complete anonymity maintained - users see "Heartfelt Member", support members see "Anonymous User #1234".

## Features

- Anonymous queue system with admin channel management
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

**Users:** `/help` → describe issue → wait in queue → anonymous chat → `/end`  
**Support Members:** Monitor admin channel → click "Claim" → anonymous chat → `/end`

**Commands:** `/start` `/help` `/status` `/cancel` `/end`

## Configuration

**.env file:**
```bash
BOT_TOKEN=your_bot_token_here
ADMIN_CHANNEL_ID=-1001234567890
MONGODB_URI=mongodb://localhost:27017/heartfelt_bot  # Optional
```

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

```bash
python3 test_db_integration.py  # Test MongoDB integration
python3 main.py                 # Start bot and test user flow
```

Test flow: User sends `/help` → describe issue → check admin channel → claim → chat → `/end`

## Privacy Policy

We value your privacy. Please review our full privacy policy here:  
[Hearhtfelt Companion Privacy Policy](https://docs.google.com/document/d/1pWvutw151h_sypdttkwEH7hDiBwBdX-qF_xJypffn7Y/edit?usp=sharing)
