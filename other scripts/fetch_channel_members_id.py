import discord
import asyncio
import os

# Enable the default intents and also enable the privileged members intent.
# Without this, the bot won't be able to retrieve the full member list.
intents = discord.Intents.default()
intents.members = True  # Necessary to access guild member data

# Initialize the client with the specified intents.
client = discord.Client(intents=intents)

# Replace with your actual channel ID.
CHANNEL_ID = 1333218474218094686  # this is the general channel ID
TOKEN = os.getenv("DISCORD_TOKEN")

@client.event
async def on_ready():
    # Called once the bot has connected to Discord successfully.
    print(f"Logged in as {client.user}")

    # Retrieve the channel object using its ID.
    channel = client.get_channel(CHANNEL_ID)
    if channel is None:
        print("Channel not found. Check your CHANNEL_ID and make sure the bot is in the guild.")
        await client.close()
        return

    # For text channels, channel.members returns a list of Member objects that can view the channel.
    # This requires that the member intent is enabled and that the member cache is populated.
    members = channel.members

    # Create a dictionary mapping each member's display name to their ID.
    members_dict = {member.display_name: member.id for member in members}
    
    # Print the dictionary.
    print("Members in the channel:", members_dict)

    # Optionally, you could store these IDs or send them as a message.
    # For this example, we simply close the bot after printing.
    await client.close()

# Start the bot using the provided token.
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set in the environment.")

client.run(TOKEN)
