# Copy to config.py and fill in. config.py is git-ignored: keep it private.
# Every value can also be set as an environment variable of the same name.

# --- Paper announcements (bot.py) ---

# Incoming webhooks (Channel settings > Integrations > Webhooks > Copy Webhook URL).
# Anyone with a URL can post in that channel, so treat them like passwords.
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."
DISCORD_TEST_WEBHOOK_URL = "https://discord.com/api/webhooks/..."  # used with --testchannel

# Contact email sent with Crossref requests (puts the bot in Crossref's
# 'polite' pool, which is less likely to be rate limited). None to omit.
CROSSREF_MAILTO = None

# Group people page read by the weekly member sync. None disables the sync.
MEMBERS_PAGE_URL = None

# Telegram message when the member sync adds an author. None disables it.
TELEGRAM_BOT_TOKEN = None
TELEGRAM_CHAT_ID = None

# Claude API key (console.anthropic.com). Used only when an abstract is too
# long to fit in the Discord message: Claude condenses it instead of cutting
# it off. None to always truncate.
ANTHROPIC_API_KEY = None

# --- DM forwarding and channel commands (other scripts/dms_handler.py) ---
DISCORD_TOKEN = "bot token from the Discord developer portal"
DMS_GUILD_ID = 0             # server where the slash commands are registered
DMS_FORWARD_TO_USER_ID = 0   # user who receives DMs and bot conversations
# Channels where the bot may react or reply unprompted; elsewhere only when addressed.
# CASUAL_CHANNELS = ("general", "random", "lunch", "bot-testing-ground")
