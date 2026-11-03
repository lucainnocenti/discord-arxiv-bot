# arXiv Discord bot

Posts new arXiv papers by tracked authors to a Discord channel, and later
announces when those preprints are published in a journal. Meant to run once
a day from a scheduler.

## Setup

```bash
pip install -r requirements.txt
cp config.example.py config.py                       # then fill it in
cp tracked_authors.example.json tracked_authors.json  # then list your authors
```

`config.py`, `tracked_authors.json` and the state files below are private and
git-ignored. If the bot runs on several machines, sync them between those
machines some other way (e.g. a shared cloud folder).

## Running

```bash
python bot.py                    # a normal run
python bot.py --nosend --nosave  # dry run: shows what would be posted, changes nothing
python bot.py --testchannel      # post to the test channel's webhook instead
```

Other flags: `--api` (always run the API search too), `--rss` (never run it),
`--days N` (minimum API search window, default 7), `--nomembersync` (skip the
weekly website check), `--forcemembersync` (run it now).

## What a run does

1. **Member sync (weekly):** reads `MEMBERS_PAGE_URL` and adds current members
   who aren't tracked yet to `tracked_authors.json` with `"source": "website"`,
   then sends a Telegram message if configured. It never removes anyone.
2. **New papers:** reads the combined quant-ph and cond-mat RSS
   feed (the latest daily announcement; categories in `settings.py`) and posts papers by tracked authors that aren't in the
   registry yet. The arXiv API is searched as well when something could be
   missing: the feed is empty (announcement late) or unreadable, or
   announcements were missed since the last one processed (bot offline, a
   skipped mailing, a failed run). That search starts a few days before the
   last processed announcement, at least `--days` back. An announcement is
   recorded as processed in `last_rss_announcement.txt` only once all its
   papers were posted and any gap before it was searched.
3. **Publication updates:** for every posted paper not yet announced as
   published, checks arXiv for a DOI or journal reference, falling back to a
   Crossref title search, and posts a short "published" message.

## Files next to the bot

| File | What it is |
|---|---|
| `config.py` | Webhook URLs, Crossref email, Telegram, DM-bot settings |
| `tracked_authors.json` | Tracked authors: name, extra spellings, Discord ID for mentions, `manual`/`website` source |
| `posted_papers.txt` | The registry: one JSON line per paper with its `posted`/`published` flags |
| `last_rss_announcement.txt` | Latest arXiv announcement fully processed |
| `last_member_sync.txt` | Date of the last website member sync |
| `logs/bot-<hostname>.log` | Rotating log, one per machine |
| `run.lock` | Present while a run is in progress, so two machines don't run at once |

Author names only need the full form: initials ("F. Last") and middle names
are matched automatically. Add a `variations` entry only for spellings the
matcher can't derive, such as a dropped middle name or a different surname
form. Put a website name in `ignored_website_names` to stop the sync adding it.

## Fixing the registry

```bash
python manage.py show 2605.15029
python manage.py list --awaiting                 # posted, not yet announced as published
python manage.py set 2605.15029 --unpublished    # flags can be set or cleared
```

Don't edit the registry while the bot is running.

## Code layout

| Module | Responsibility |
|---|---|
| `bot.py` | Entry point: logging, run lock, member sync, then the pipeline |
| `pipeline.py` | One run's logic; posts through a `send` function |
| `arxiv_source.py` | Finds new papers (RSS feed first, API search for gaps), tracks the last announcement processed; refreshes known papers by ID |
| `rss_source.py` | Parses the RSS feed of the latest announcement |
| `publication_check.py` | Decides whether tracked papers are now published |
| `crossref.py` | Paced Crossref client, matching rules, citation formatting |
| `registry.py` | The paper registry (load once, save atomically) |
| `discord_formatter.py` / `discord_webhook.py` | Message text / delivery |
| `tracked_authors.py` | Author list and author-name matching |
| `member_sync.py` | Website member sync |
| `notifier.py`, `utils.py` | Telegram messages; the one-date state files |

`other scripts/` holds unrelated helpers, such as `dms_handler.py`, a separate
Discord bot for DM forwarding and channel commands. Their extra dependencies
are in `other scripts/requirements.txt`.

Run the tests with `python -m pytest`.

When an empty or unreadable RSS feed triggers the API search, the bot sends a
Telegram alert using `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Healthy feeds
with no matching authors do not trigger this alert. `--rss` suppresses the API
fallback and its alert; `--nosend` previews the alert without sending it.
