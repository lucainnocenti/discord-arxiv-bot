import discord
from discord.ext import commands
import config

# Replace with your bot's token
DISCORD_TOKEN = config.DISCORD_TOKEN
CHANNEL_ID = config.CHANNEL_ID  # (Not used in this snippet, but retained from your config)

# Replace with the Discord ID of the user you want to forward DMs to
TARGET_USER_ID = 1013801241295454268

# Set up intents; note that message content must be enabled in the Discord Developer Portal
intents = discord.Intents.default()
intents.message_content = True  # Allows access to message content in messages
intents.dm_messages = True      # Enables handling DM messages

client = commands.Bot(command_prefix="!", intents=intents)

# --- Slash Command: Create Channel ---
@client.tree.command(
    name="create_channel",
    description="Creates a new text channel with the specified name in the specified category",
    guild=discord.Object(id=1333218473014464522)
)
async def create_channel(interaction: discord.Interaction, name: str, category: discord.CategoryChannel):
    # Ensure the command is used within a guild.
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return
    
    # Retrieve the category from the guild using its ID.
    # Ensure you have added CATEGORY_ID to your config.
    # category = interaction.guild.get_channel(config.PHYSICS_CATEGORY_ID)
    if category is None:
        await interaction.response.send_message("Category not found.", ephemeral=True)
        return


    try:
        # Create the channel inside the specified category.
        new_channel = await interaction.guild.create_text_channel(name=name, category=category)
        await interaction.response.send_message(
            f"Channel **{new_channel.name}** created in category **{category.name}** successfully!"
        )
    except Exception as error:
        await interaction.response.send_message(f"Error creating channel: {error}", ephemeral=True)

@client.tree.command(
    name="create_private_channel",
    description="Creates a new private text channel accessible only to you",
    guild=discord.Object(id=1333218473014464522)
)
async def create_private_channel(interaction: discord.Interaction, name: str, category: discord.CategoryChannel):
    # Ensure the command is used within a guild.
    if interaction.guild is None:
        await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
        return

    # Define permission overwrites:
    # Deny read_messages for the @everyone role and allow it for the command invoker.
    overwrites = {
        interaction.guild.default_role: discord.PermissionOverwrite(read_messages=False),
        interaction.user: discord.PermissionOverwrite(read_messages=True)
    }

    try:
        # Create the channel with the specified name, category, and overwrites.
        new_channel = await interaction.guild.create_text_channel(name=name, category=category, overwrites=overwrites)
        await interaction.response.send_message(
            f"Private channel **{new_channel.name}** created in category **{category.name}** successfully!"
        )
    except Exception as error:
        await interaction.response.send_message(f"Error creating channel: {error}", ephemeral=True)



# --- Event: on_ready ---
@client.event
async def on_ready():
    try:
        # Sync the slash commands with Discord.
        synced = await client.tree.sync(guild=discord.Object(id=1333218473014464522))
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Error syncing commands: {e}")
    print(f'Bot is ready. Logged in as {client.user}')

# --- Event: on_message ---
@client.event
async def on_message(message: discord.Message):
    # Ignore messages sent by bots.
    if message.author.bot:
        return

    # If the message is a DM, forward it to the target user.
    if isinstance(message.channel, discord.DMChannel):
        try:
            target_user = await client.fetch_user(TARGET_USER_ID)
            forwarded_message = (
                f"**Forwarded DM**\n"
                f"From: **{message.author}** (ID: {message.author.id})\n"
                f"Content: {message.content}"
            )
            await target_user.send(forwarded_message)
            print(f"Forwarded DM from {message.author} to {target_user}")
        except Exception as e:
            print(f"Error forwarding DM: {e}")

client.run(DISCORD_TOKEN)
