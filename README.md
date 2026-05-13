# Steam Library Report Generator

Small local tool that generates an HTML report from installed Steam games.

## Quick start

### Windows

Double-click:

```txt
run.bat
```

Or run:

```bash
python generate_report.py
```

### Linux / macOS

```bash
python3 generate_report.py
```

The script generates this file in the same folder:

```txt
steam_library_report.html
```

## What it scans

```txt
steamapps/libraryfolders.vdf
steamapps/appmanifest_*.acf
userdata/*/config/localconfig.vdf
```

## Features

- Detects the Steam installation automatically
- Scans multiple Steam library folders
- Lists installed games
- Shows total storage used
- Shows the largest installed games
- Shows local playtime data when available
- Generates a standalone HTML dashboard
- Can optionally export JSON

## If Steam is not detected

Run with the Steam path manually:

```bash
python generate_report.py --steam "C:\Program Files (x86)\Steam"
```

## Optional JSON export

```bash
python generate_report.py --json
```

This also creates:

```txt
steam_library_report.json
```
