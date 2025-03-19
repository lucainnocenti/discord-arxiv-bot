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
            # Default to 1 days ago if file exists but can't be read
            return datetime.now() - timedelta(days=1)
    else:
        # create the file if 1 day ago as date, if it doesn't exist
        newdate = datetime.now() - timedelta(days=1)
        with open(LAST_CHECK_FILE, 'w') as f:
            f.write(newdate.isoformat())
        return newdate

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
        # Add date range to the query
        combined_query += f' AND submittedDate:[{last_check_date.strftime("%Y%m%d")} TO 99999999]'
        logging.info(f"Constructed combined query: {combined_query}")

        search = arxiv.Search(
            query=combined_query,
            max_results=50,  # Adjust max_results as needed
            sort_by=arxiv.SortCriterion.SubmittedDate,
            sort_order=arxiv.SortOrder.Ascending,
        )
        logging.info("Performing combined search on arXiv...")

        try:
            loop = asyncio.get_event_loop()
            results = await loop.run_in_executor(None, lambda: list(client.results(search)))
            logging.info(f"Found {len(results)} papers with the combined query.")
            
            papers_posted = 0
            for result in results:
                arxiv_id = result.entry_id
                if arxiv_id in posted_papers:
                    logging.info(f"Already posted '{result.title}', skipping.")
                    continue

                result_authors = [a.name for a in result.authors]

                # Determine which of the target authors are present in this result (order as in TARGET_AUTHORS)
                target_authors_in_result = [
                    name for name in TARGET_AUTHORS 
                    if any(name.lower() in author.lower() for author in result_authors)
                ]

                # Check if the paper's first author is one of our target authors
                first_author = result_authors[0] if result_authors else ""
                target_first_author = None
                for target in TARGET_AUTHORS:
                    if target.lower() in first_author.lower():
                        target_first_author = target
                        break

                # Reorder the list so that if the first author is a target, it appears first
                if target_first_author and target_first_author in target_authors_in_result:
                    target_authors_in_result.remove(target_first_author)
                    target_authors_in_result.insert(0, target_first_author)

                # Build the string to be printed
                if len(target_authors_in_result) == 1:
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
                
                # Check if one of our target authors is the first author
                first_author = result_authors[0] if result_authors else ""
                first_author_is_target = False
                target_first_author = None
                
                # Find which target author (if any) is the first author
                for target in TARGET_AUTHORS:
                    if target.lower() in first_author.lower():
                        first_author_is_target = True
                        target_first_author = target
                        break
                
                # Create the message with target authors (without special annotations)
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
