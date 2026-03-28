"""Small Discord utility bot for channel creation commands and DM forwarding."""

import logging
import discord
from discord.ext import commands
import config

DISCORD_TOKEN = config.DISCORD_TOKEN
CHANNEL_ID = config.CHANNEL_ID
TARGET_USER_ID = 1013801241295454268
GUILD_ID = 1333218473014464522

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

intents = discord.Intents.default()
intents.message_content = True

client = commands.Bot(command_prefix="!", intents=intents)

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
    # then explicitly grant access back to the requesting user.
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.user: discord.PermissionOverwrite(read_messages=True)
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

@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    if isinstance(message.channel, discord.DMChannel):
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

client.run(DISCORD_TOKEN)
