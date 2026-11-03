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

Other flags: `--days N` (look back N days instead of 7), `--rss` (use only
the RSS feed of the latest announcement, skipping the API search),
`--nomembersync` (skip the weekly website check), `--forcemembersync` (run it
now).

## What a run does

1. **Member sync (weekly):** reads `MEMBERS_PAGE_URL` and adds current members
   who aren't tracked yet to `tracked_authors.json` with `"source": "website"`,
   then sends a Telegram message if configured. It never removes anyone.
2. **New papers:** asks the arXiv API for quant-ph papers by tracked authors
   submitted in the last 7 days and posts those not in the registry yet. The
   window overlaps earlier runs on purpose, so a late arXiv mailing or a few
   days offline don't lose papers. If the API search fails (fully or for some
   authors), the RSS feed of the latest announcement is read as a fallback.
3. **Publication updates:** for every posted paper not yet announced as
   published, checks arXiv for a DOI or journal reference, falling back to a
   Crossref title search, and posts a short "published" message.

## Files next to the bot

| File | What it is |
|---|---|
| `config.py` | Webhook URLs, Crossref email, Telegram, DM-bot settings |
| `tracked_authors.json` | Tracked authors: name, extra spellings, Discord ID for mentions, `manual`/`website` source |
| `posted_papers.txt` | The registry: one JSON line per paper with its `posted`/`published` flags |
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
| `arxiv_source.py` | arXiv API: author search over the window (RSS fallback), refresh by ID |
| `rss_source.py` | Parses the arXiv RSS feed of the latest announcement |
| `publication_check.py` | Decides whether tracked papers are now published |
| `crossref.py` | Paced Crossref client, matching rules, citation formatting |
| `registry.py` | The paper registry (load once, save atomically) |
| `discord_formatter.py` / `discord_webhook.py` | Message text / delivery |
| `member_sync.py`, `tracked_authors.py` | Website member sync, author list |
| `dms_handler.py` | Separate Discord bot for DM forwarding and channel commands |

Run the tests with `python -m pytest`.
