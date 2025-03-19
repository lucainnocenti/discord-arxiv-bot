import discord
import asyncio
import arxiv
import os
import sys
import logging   ### ADDED
import config
from datetime import datetime, timedelta

# ----------------------------------------------------------------------------
# Logging Setup
# ----------------------------------------------------------------------------
### ADDED/CHANGED
# This sets up logging so that it logs INFO and above to both a file (bot.log)
# and also to the console. Adjust logging level/format as desired.
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler("bot.log"),    # log to file
        logging.StreamHandler(sys.stdout)  # also log to console
    ]
)
# ----------------------------------------------------------------------------

DISCORD_TOKEN = config.DISCORD_TOKEN
CHANNEL_ID = config.CHANNEL_ID
LAST_CHECK_FILE = "last_check_date.txt"

# List of Authors to Track
TARGET_AUTHORS = [
    # Staff
    'Angelo Carollo',
    'Francesco Ciccarello',
    'Umberto De Giovannini',
    'Gabriele Lo Monaco',
    'Salvatore Lorenzo',
    'Daniele Militello',
    'Anna Napoli',
    'Massimo Palma',
    'Mauro Paternostro',
    'Davide Valenti',
    'Luca Innocenti',
    # Postdoctoral Researchers
    'Alessandro Candeloro',
    'Silvia Casulleras Guardia',
    'Diana Chisholm',
    'Paolo Erdman',
    'Andris Erglis',
    'Luca Leonforte',
    'Daniele Morrone',
    'Zhengwei Nie',
    'Sofia Sgroi',
    'Xuejian Sun',
    # PhD Students
    'Simone Artini',
    'Duilio De Santis',
    'Enrico Di Benedetto',
    'Giovanni Di Fresco',
    'Subhojit Pal',
    'Claudio Pellitteri',
    'Marcel Augusto Pinto',
    'Alessandro Romancino',
    'Giovanni Luca Sferrazza',
    'Marco Vetrano',
    'Sujan Vijayaraj'
]

# Avoid reposting the same paper
posted_papers = set()

def get_last_check_date():
    if os.path.exists(LAST_CHECK_FILE):
        try:
            with open(LAST_CHECK_FILE, 'r') as f:
                date_str = f.read().strip()
                return datetime.fromisoformat(date_str)
        except Exception as e:
            logging.error(f"Error reading last check date: {e}")
            # Default to 7 days ago if file exists but can't be read
            return datetime.now() - timedelta(days=7)
    else:
        # Default to 7 days ago if file doesn't exist
        return datetime.now() - timedelta(days=7)

def save_last_check_date():
    try:
        with open(LAST_CHECK_FILE, 'w') as f:
            f.write(datetime.now().isoformat())
        logging.info(f"Saved current date to {LAST_CHECK_FILE}")
    except Exception as e:
        logging.error(f"Error saving last check date: {e}")

class ArxivBot(discord.Client):
    async def on_ready(self):
        logging.info(f"Logged in as {self.user}")
        await self.check_arxiv_once()
        await self.close()  # Close the bot after checking once

    async def check_arxiv_once(self):
        await self.wait_until_ready()
        channel = self.get_channel(CHANNEL_ID)
        if channel is None:
            logging.error("ERROR: Could not get channel. Please check CHANNEL_ID!")
            return
        
        logging.info(f"Posting to channel: {channel}")

        # Get the last check date
        last_check_date = get_last_check_date()
        logging.info(f"Checking papers published since: {last_check_date}")

        client = arxiv.Client()

        # Build a combined query string using OR operator for all target authors
        combined_query = f'cat:quant-ph AND (' + ' OR '.join(f'au:"{author}"' for author in TARGET_AUTHORS) + ')'
        logging.info(f"Constructed combined query: {combined_query}")

        search = arxiv.Search(
            query=combined_query,
            max_results=50,  # Adjust max_results as needed
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Descending,
        )
        logging.info("Performing combined search on arXiv...")

        try:
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(None, lambda: list(client.results(search)))
            logging.info(f"Found {len(results)} papers with the combined query.")
            
            # Sort results by publication date (oldest to newest)
            results = sorted(results, key=lambda r: r.published.replace(tzinfo=None))
            
            papers_posted = 0
            for result in results:
                published_naive = result.published.replace(tzinfo=None)
                # Only consider papers published after the last check date
                if published_naive <= last_check_date:
                    logging.info(
                        f"Skipping '{result.title}' (published {result.published}) "
                        "because it is older than last check date."
                    )
                    continue

                arxiv_id = result.entry_id
                if arxiv_id in posted_papers:
                    logging.info(f"Already posted '{result.title}', skipping.")
                    continue

                result_authors = [a.name for a in result.authors]
                # Determine which of the target authors are present in this result
                target_authors_in_result = [
                    name for name in TARGET_AUTHORS 
                    if any(name.lower() in author.lower() for author in result_authors)
                ]

                if not target_authors_in_result:
                    # Should not happen due to the query but just in case
                    continue
                elif len(target_authors_in_result) == 1:
                    target_authors_str = target_authors_in_result[0]
                else:
                    target_authors_str = (
                        ", ".join(target_authors_in_result[:-1])
                        + " and "
                        + target_authors_in_result[-1]
                    )
                
                posted_papers.add(arxiv_id)
                title = result.title
                link = result.entry_id  # Typically the paper URL
                published_str = result.published.strftime('%Y-%m-%d %H:%M:%S')
                message = (
                    f"📄 **New paper by {target_authors_str}:**\n"
                    f"**Title:** {title}\n"
                    f"**Authors:** {', '.join(result_authors)}\n"
                    f"**Published:** {published_str}\n"
                    f"🔗 <{link}>"
                )
                logging.info(
                    f"Sending message for paper: '{title}' with target authors: {target_authors_str}."
                )
                await channel.send(message)
                papers_posted += 1
            
            logging.info(f"Posted {papers_posted} new papers.")
            
            # Save the current date as the last check date
            save_last_check_date()
            
        except Exception as e:
            logging.exception(f"Error during combined query processing: {e}")

async def main():
    intents = discord.Intents.default()
    bot = ArxivBot(intents=intents)
    try:
        await bot.start(DISCORD_TOKEN)
    except KeyboardInterrupt:
        logging.info("Shutdown requested...")
    finally:
        # Ensure proper cleanup
        if not bot.is_closed():
            logging.info("Closing bot connection...")
            await bot.close()
        
        # Give the event loop time to complete pending tasks
        pending = asyncio.all_tasks(asyncio.get_event_loop())
        for task in pending:
            if task is not asyncio.current_task():
                try:
                    logging.info("Cancelling pending task...")
                    task.cancel()
                    await task
                except asyncio.CancelledError:
                    pass
        
        # For Discord.py 2.0+, explicitly close the aiohttp session
        if hasattr(bot, 'http') and hasattr(bot.http, '_session'):
            if not bot.http._session.closed:
                logging.info("Closing aiohttp session...")
                await bot.http._session.close()

if __name__ == "__main__":
    asyncio.run(main())
