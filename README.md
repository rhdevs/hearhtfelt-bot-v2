# Heartfelt Anonymous Helpline Bot

A Telegram bot that connects users seeking help with trained support members through completely anonymous conversations.

## 🎯 Overview

This bot creates a safe, anonymous space where users can request support and be connected with heartfelt support team members. All conversations are completely anonymous - users see "Heartfelt Member" and support members see "Anonymous User #1234".

## ✨ Features

- **Complete Anonymity**: No personal information shared between parties
- **Queue System**: Fair first-come-first-served queue with admin channel management
- **Queue Cancellation**: Users can leave the queue if they don't want to wait
- **Session Management**: Secure anonymous conversations with proper cleanup
- **Safety Logging**: Developer-only logs for safety and oversight
- **Error Handling**: Graceful degradation and helpful error messages

## 🚀 Quick Start

### Prerequisites

- Python 3.8+
- Telegram Bot Token (from @BotFather)
- Private Telegram Channel for admin notifications

### Installation

1. **Clone and install dependencies:**
   ```bash
   git clone <repository-url>
   cd hearhtfelt-telegram-bot
   pip3 install -r requirements.txt
   ```

2. **Configure the bot:**
   ```bash
   # Copy and edit environment file
   cp .env.example .env
   # Add your bot token and channel ID to .env
   ```

3. **Set up authorized members:**
   ```python
   # Edit config.py and add heartfelt member user IDs
   HEARTFELT_MEMBERS = [
       1522275008,  # Add actual user IDs here
   ]
   ```

4. **Run the bot:**
   ```bash
   python main.py
   ```

## 📋 Setup Instructions

### 1. Create Telegram Bot

1. Message @BotFather on Telegram
2. Create new bot with `/newbot`
3. Copy the bot token
4. Add token to `.env` file

### 2. Create Admin Channel

1. Create a private Telegram channel
2. Add your bot as administrator
3. Grant permissions: Send Messages, Delete Messages
4. Get channel ID (forward message to @userinfobot)
5. Add channel ID to `.env` file

### 3. Configure Heartfelt Members

1. Get Telegram user IDs of support team members
2. Add IDs to `HEARTFELT_MEMBERS` list in `config.py`
3. Add these members to the admin channel

## 🎮 How It Works

### For Users Seeking Help:

1. **Start conversation:** `/start`
2. **Request help:** `/help`
3. **Describe issue:** Type your message
4. **Wait in queue:** Check position with `/status`
5. **Cancel if needed:** `/cancel` to leave queue
6. **Chat anonymously:** When connected to support member
7. **End conversation:** `/end` when finished

### For Heartfelt Members:

1. **Monitor admin channel:** Queue notifications appear automatically
2. **Claim conversations:** Click "📞 Claim" button
3. **Chat anonymously:** Support the user
4. **End conversation:** `/end` when finished

## 🤖 Bot Commands

| Command | Description | Who Can Use |
|---------|-------------|-------------|
| `/start` | Welcome message and instructions | Everyone |
| `/help` | Request support from heartfelt team | Users |
| `/status` | Check queue position or conversation status | Users |
| `/cancel` | Leave the queue if waiting | Users in queue |
| `/end` | End current conversation | Anyone in conversation |

## 📁 Project Structure

```
heartfelt-telegram-bot/
├── main.py                 # Bot initialization and startup
├── bot_handlers.py         # Command and message handlers  
├── session_manager.py      # Anonymous conversation management
├── queue_manager.py        # Admin channel queue system
├── config.py              # Configuration and in-memory storage
├── requirements.txt       # Python dependencies
├── .env                   # Environment variables
├── CLAUDE.md              # Development guidance
└── README.md              # This file
```

## 🔧 Configuration

### Environment Variables (.env)

```bash
# Telegram Bot Token from @BotFather
BOT_TOKEN=your_bot_token_here

# Private channel ID for queue notifications
ADMIN_CHANNEL_ID=-1001234567890
```

### Authorized Members (config.py)

```python
HEARTFELT_MEMBERS = [
    1522275008,  # First heartfelt member
    9876543210,  # Second heartfelt member
    # Add more user IDs as needed
]
```

## 🧪 Testing

### Basic User Flow Test:

1. **Start bot:** `python main.py`
2. **User interaction:**
   - Send `/start` to bot
   - Send `/help` and describe issue
   - Check `/status`
   - Try `/cancel` to test cancellation
3. **Heartfelt member interaction:**
   - Check admin channel for queue notification
   - Click "Claim" button
   - Start anonymous conversation
   - Use `/end` to finish

### Error Testing:

- Test with bot not in admin channel
- Test claiming by unauthorized users
- Test cancellation at different stages

## 🔒 Privacy & Security

- **Complete Anonymity**: Real identities never shared between users and heartfelt members
- **Safety Logs**: Developer-only logs store real user IDs for safety oversight
- **Session Isolation**: Each conversation is completely separate
- **Automatic Cleanup**: Sessions and queue entries cleaned up properly

## 🛠️ Development

### Adding New Features:

1. Update `bot_handlers.py` for new commands
2. Add messages to `config.py`
3. Register handlers in `main.py`
4. Test thoroughly with both user and heartfelt member accounts

### Common Issues:

- **"Chat not found" error:** Bot needs admin access to channel
- **Queue not appearing:** Check bot permissions and channel ID
- **Commands not working:** Verify handler registration in main.py

## 📊 Monitoring

The bot logs important events:

- Startup and channel access validation
- Queue operations and session management
- Error conditions and recovery
- Safety logs for developer oversight

## 🤝 Contributing

1. Fork the repository
2. Create feature branch
3. Test thoroughly
4. Submit pull request

## 📄 License

This project is intended for use by heartfelt support organizations. Please use responsibly and maintain user privacy and safety.

## 🆘 Support

For technical issues:
1. Check bot logs for error messages
2. Verify configuration in `.env` and `config.py`
3. Test bot permissions in admin channel
4. Review setup instructions above

For feature requests or bugs, please create an issue in the repository.# hearhtfelt-bot
