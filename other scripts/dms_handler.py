"""Small Discord utility bot: channel creation commands, DM forwarding, and
in-character replies to DMs and channel messages addressed to the bot
(banter.py), each conversation also forwarded to the operator.

Separate from the paper bot; it only shares config.py, one folder up.
"""

import logging
import os
import sys

import discord
from discord.ext import commands

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from banter import Banter, webhook_id_from_url

DISCORD_TOKEN = config.DISCORD_TOKEN
# DMs to the bot are forwarded to this user.
TARGET_USER_ID = config.DMS_FORWARD_TO_USER_ID
# Slash commands are registered on this server only.
GUILD_ID = config.DMS_GUILD_ID

intents = discord.Intents.default()
intents.message_content = True

client = commands.Bot(command_prefix="!", intents=intents)



async def send_to_operator(text: str):
    target_user = await client.fetch_user(TARGET_USER_ID)
    await target_user.send(text, allowed_mentions=discord.AllowedMentions.none())


# Replies need a Claude API key; without one they're off and DMs are only forwarded.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY") or getattr(config, "ANTHROPIC_API_KEY", None)
banter = Banter(
    client,
    ANTHROPIC_API_KEY,
    GUILD_ID,
    # Replies to the paper announcements count as replies to the bot.
    webhook_ids=[
        webhook_id_from_url(getattr(config, name, None))
        for name in ("DISCORD_WEBHOOK_URL", "DISCORD_TEST_WEBHOOK_URL")
    ],
    # Every conversation with the bot, in DMs or channels, is summarised to the operator.
    forward=send_to_operator,
    operator_id=TARGET_USER_ID,
    # Channels where the bot may chime in unprompted; elsewhere only when addressed.
    **({"casual_channels": config.CASUAL_CHANNELS} if hasattr(config, "CASUAL_CHANNELS") else {}),
) if ANTHROPIC_API_KEY else None

@client.tree.command(
    name="create_channel",
    description="Creates a new text channel with the specified name in the specified category",
    guild=discord.Object(id=GUILD_ID)
)
async def create_channel(interaction: discord.Interaction, name: str, category: discord.CategoryChannel):
    # Slash commands can be invoked in DMs, so guard against missing guild state
    # before attempting any server-side channel creation.
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    if category is None:
        await interaction.response.send_message("Category not found.", ephemeral=True)
        return

    try:
        new_channel = await interaction.guild.create_text_channel(name=name, category=category)
        await interaction.response.send_message(
            f"Channel **{new_channel.name}** created in category **{category.name}** successfully!"
        )
    except Exception as error:
        await interaction.response.send_message(f"Error creating channel: {error}", ephemeral=True)

@client.tree.command(
    name="create_private_channel",
    description="Creates a new private text channel accessible only to you",
    guild=discord.Object(id=GUILD_ID)
)
async def create_private_channel(interaction: discord.Interaction, name: str, category: discord.CategoryChannel):
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    # Override the default role to hide the channel from the server at large,
    # then explicitly grant access back to the requesting user and to the bot
    # itself, so it can answer there too.
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.user: discord.PermissionOverwrite(read_messages=True),
        interaction.guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }

    try:
        new_channel = await interaction.guild.create_text_channel(
            name=name,
            category=category,
            overwrites=overwrites
        )
        await interaction.response.send_message(
            f"Private channel **{new_channel.name}** created in category **{category.name}** successfully!"
        )
    except Exception as error:
        await interaction.response.send_message(f"Error creating channel: {error}", ephemeral=True)

@client.event
async def on_ready():
    try:
        # Explicit guild sync keeps command registration scoped to the target
        # server instead of relying on slower global propagation.
        synced = await client.tree.sync(guild=discord.Object(id=GUILD_ID))
        logging.info("Synced %s command(s)", len(synced))
    except Exception as e:
        logging.exception("Error syncing commands: %s", e)

    logging.info("Bot is ready. Logged in as %s", client.user)
    if banter is None:
        logging.info("ANTHROPIC_API_KEY is not set: replies are off; DMs are only forwarded.")

@client.event
async def on_message(message: discord.Message):
    # Before the bot check: banter tracks the bot's own posts too.
    if banter is not None:
        await banter.on_message(message)

    if message.author.bot:
        return

    # With banter on, it forwards each DM conversation together with the bot's reply.
    if isinstance(message.channel, discord.DMChannel) and banter is None:
        try:
            # Forward DMs to a fixed operator account so users can message the
            # bot privately without the messages being lost in a DM inbox.
            target_user = await client.fetch_user(TARGET_USER_ID)
            forwarded_message = (
                f"**Forwarded DM**\n"
                f"From: **{message.author}** (ID: {message.author.id})\n"
                f"Content: {message.content}"
            )
            await target_user.send(forwarded_message)
            logging.info("Forwarded DM from %s to %s", message.author, target_user)
        except Exception:
            logging.exception("Error forwarding DM")

    await client.process_commands(message)

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    client.run(DISCORD_TOKEN)
