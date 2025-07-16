# HeaRHtfelt Companion Bot

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
   - Add support member user IDs to `HEARTFELT_MEMBERS` in `config.py`
   - Optionally add `MONGODB_URI` for persistent storage

3. **Run:**
   ```bash
   python3 main.py
   ```

## Bot Setup

**Telegram Bot:** Create via @BotFather, add token to `.env`  
**Admin Channel:** Private channel, add bot as admin with send/edit/delete permissions  
**Members:** Add support member user IDs to `config.py` and admin channel

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
HEARTFELT_MEMBERS = [1522275008, 9876543210]  # Support member user IDs
```

## MongoDB (Optional)

Stores conversation history and analytics. Works without it (memory-only mode).

**Usage:**
```bash
python3 db_utils.py stats                    # Session statistics
python3 db_utils.py monthly                  # This month's sessions  
python3 db_utils.py transcript --session-id  # Full conversation
```

## Testing

```bash
python3 test_db_integration.py  # Test MongoDB integration
python3 main.py                 # Start bot and test user flow
```

Test flow: User sends `/help` → describe issue → check admin channel → claim → chat → `/end`

## Privacy Policy

We value your privacy. Please review our full privacy policy here:  
[Heartfelt Companion Privacy Policy](https://docs.google.com/document/d/1pWvutw151h_sypdttkwEH7hDiBwBdX-qF_xJypffn7Y/edit?usp=sharing)