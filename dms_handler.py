import discord
import os
import config

# Replace with your bot's token
DISCORD_TOKEN = config.DISCORD_TOKEN
CHANNEL_ID = config.CHANNEL_ID

# Replace with the Discord ID of the user you want to forward DMs to
TARGET_USER_ID = 1013801241295454268

# Set up intents; note that message content must be enabled in the Discord Developer Portal
intents = discord.Intents.default()
intents.message_content = True  # Allows access to message content in messages
intents.dm_messages = True      # Enables handling DM messages

client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'Bot is ready. Logged in as {client.user}')

@client.event
async def on_message(message: discord.Message):
    # Ignore messages sent by bots
    if message.author.bot:
        return

    # Check if the message was sent in a DM (i.e. not in a guild)
    if isinstance(message.channel, discord.DMChannel):
        try:
            # Fetch the target user
            target_user = await client.fetch_user(TARGET_USER_ID)
            # Construct the forwarded message with relevant details
            forwarded_message = (
                f"**Forwarded DM**\n"
                f"From: **{message.author}** (ID: {message.author.id})\n"
                f"Content: {message.content}"
            )
            # Forward the message to the target user
            await target_user.send(forwarded_message)
            print(f"Forwarded DM from {message.author} to {target_user}")
        except Exception as e:
            print(f"Error forwarding DM: {e}")

client.run(DISCORD_TOKEN)
