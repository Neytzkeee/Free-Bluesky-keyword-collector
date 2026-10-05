# Bluesky Keyword Collector

A simple Python script that collects public Bluesky posts and replies based on a list of keywords.

The collector:

- searches for recent public posts that match each keyword;
- collects replies from the posts found;
- saves the results in CSV files;
- runs every 3 minutes by default;
- ignores posts and replies that were already collected.

No Bluesky account, API key, or access token is required.

## Requirements

- Python 3.10 or newer
- Internet connection

No external Python packages are required.

## How to use

1. Open `keywords.txt` and add one keyword per line.
2. Open PowerShell in the project folder.
3. Run:

```powershell
python .\bluesky_collector.py
```

The first collection starts immediately. The script then repeats the search every 3 minutes. Press `Ctrl+C` to stop it.

## Output files

- `bluesky_posts.csv`: collected posts.
- `bluesky_comentarios.csv`: collected replies.
- `bluesky_estado.json`: IDs of previously collected records, used to prevent duplicates.

## Useful options

Run only once:

```powershell
python .\bluesky_collector.py --run-once
```

Collect up to 100 posts per keyword:

```powershell
python .\bluesky_collector.py --limit-per-keyword 100
```

Collect posts without replies:

```powershell
python .\bluesky_collector.py --skip-comments
```

Change the interval to 10 minutes:

```powershell
python .\bluesky_collector.py --interval-minutes 10
```

Remove usernames, display names, links, and direct identifiers from the CSV output:

```powershell
python .\bluesky_collector.py --omit-identifiers
```

To see every available option, run:

```powershell
python .\bluesky_collector.py --help
```

## Responsible use

Use public data responsibly and follow the Bluesky Terms of Service, applicable privacy laws, and your institution's research guidelines.
