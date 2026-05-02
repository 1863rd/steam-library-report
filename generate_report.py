#!/usr/bin/env python3
"""
Steam Library Report
A one-command local Steam library report generator.

Run:
  python generate_report.py

It scans your local Steam installation, reads libraryfolders.vdf and appmanifest_*.acf,
then generates steam_library_report.html next to this script and opens it in your browser.
No login and no external Python dependency.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import os
import platform
import re
import sys
import webbrowser
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

APP_NAME = "Steam Library Report"
REPORT_NAME = "steam_library_report.html"


# -----------------------------
# VDF / ACF parser
# -----------------------------
_TOKEN_RE = re.compile(r'"((?:\\.|[^"\\])*)"|([{}])')
_COMMENT_RE = re.compile(r'//.*?$|/\*.*?\*/', re.MULTILINE | re.DOTALL)


def _unescape_vdf_string(value: str) -> str:
    return (
        value.replace(r'\\', '\\')
        .replace(r'\"', '"')
        .replace(r'\n', '\n')
        .replace(r'\t', '\t')
    )


def _tokens(text: str) -> List[str]:
    text = _COMMENT_RE.sub('', text)
    out: List[str] = []
    for match in _TOKEN_RE.finditer(text):
        if match.group(2):
            out.append(match.group(2))
        else:
            out.append(_unescape_vdf_string(match.group(1)))
    return out


def parse_vdf_text(text: str) -> Dict[str, Any]:
    toks = _tokens(text)
    pos = 0

    def parse_object() -> Dict[str, Any]:
        nonlocal pos
        result: Dict[str, Any] = {}
        while pos < len(toks):
            token = toks[pos]
            if token == '}':
                pos += 1
                break
            if token == '{':
                # malformed standalone object; skip it safely
                pos += 1
                nested = parse_object()
                if isinstance(nested, dict):
                    result.update(nested)
                continue

            key = token
            pos += 1
            if pos >= len(toks):
                result[key] = ""
                break

            value = toks[pos]
            if value == '{':
                pos += 1
                result[key] = parse_object()
            else:
                pos += 1
                result[key] = value
        return result

    return parse_object()


def parse_vdf_file(path: Path) -> Dict[str, Any]:
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return parse_vdf_text(path.read_text(encoding=encoding, errors="replace"))
        except UnicodeError:
            continue
    return parse_vdf_text(path.read_text(errors="replace"))


# -----------------------------
# Data model
# -----------------------------
@dataclass
class SteamGame:
    appid: str
    name: str
    install_dir: str
    library_path: str
    manifest_path: str
    size_bytes: int = 0
    last_updated: Optional[int] = None
    last_played: Optional[int] = None
    playtime_minutes: Optional[int] = None
    playtime_2weeks_minutes: Optional[int] = None

    @property
    def store_url(self) -> str:
        return f"https://store.steampowered.com/app/{self.appid}/"

    @property
    def header_url(self) -> str:
        return f"https://cdn.cloudflare.steamstatic.com/steam/apps/{self.appid}/header.jpg"


# -----------------------------
# Steam path detection
# -----------------------------
def _candidate_paths() -> List[Path]:
    system = platform.system().lower()
    candidates: List[Path] = []

    if system == "windows":
        # Registry is the most reliable on Windows.
        try:
            import winreg  # type: ignore

            registry_locations = [
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
            ]
            for hive, key_path, value_name in registry_locations:
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        value, _ = winreg.QueryValueEx(key, value_name)
                        if value:
                            candidates.append(Path(str(value)))
                except OSError:
                    pass
        except Exception:
            pass

        for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(env_name)
            if base:
                candidates.append(Path(base) / "Steam")
        candidates.extend([
            Path("C:/Program Files (x86)/Steam"),
            Path("C:/Program Files/Steam"),
        ])

    elif system == "darwin":
        candidates.append(Path.home() / "Library/Application Support/Steam")
    else:
        candidates.extend([
            Path.home() / ".steam/steam",
            Path.home() / ".local/share/Steam",
            Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        ])

    # Environment override.
    if os.environ.get("STEAM_DIR"):
        candidates.insert(0, Path(os.environ["STEAM_DIR"]))

    # De-duplicate while preserving order.
    seen = set()
    unique: List[Path] = []
    for p in candidates:
        try:
            resolved_key = str(p.expanduser()).lower()
        except Exception:
            resolved_key = str(p).lower()
        if resolved_key not in seen:
            unique.append(p.expanduser())
            seen.add(resolved_key)
    return unique


def find_steam_root(user_path: Optional[str] = None) -> Optional[Path]:
    if user_path:
        p = Path(user_path).expanduser()
        if (p / "steamapps" / "libraryfolders.vdf").exists():
            return p
        if (p / "libraryfolders.vdf").exists():
            return p.parent
        return p if p.exists() else None

    for candidate in _candidate_paths():
        if (candidate / "steamapps" / "libraryfolders.vdf").exists():
            return candidate
    return None


# -----------------------------
# Library / manifest scanning
# -----------------------------
def _norm_steam_path(raw: str) -> Path:
    # Steam stores Windows paths with escaped backslashes in VDF.
    return Path(raw.replace("\\\\", "\\")).expanduser()


def get_library_paths(steam_root: Path) -> List[Path]:
    library_file = steam_root / "steamapps" / "libraryfolders.vdf"
    paths: List[Path] = [steam_root]

    if not library_file.exists():
        return paths

    try:
        parsed = parse_vdf_file(library_file)
    except Exception:
        return paths

    root = parsed.get("libraryfolders", parsed)
    if not isinstance(root, dict):
        return paths

    for _, value in root.items():
        if isinstance(value, dict):
            raw_path = value.get("path")
            if raw_path:
                paths.append(_norm_steam_path(str(raw_path)))
        elif isinstance(value, str):
            # Old Steam format: "1" "D:\\SteamLibrary"
            paths.append(_norm_steam_path(value))

    unique: List[Path] = []
    seen = set()
    for path in paths:
        key = str(path).lower()
        if key not in seen and (path / "steamapps").exists():
            unique.append(path)
            seen.add(key)
    return unique


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(str(value)))
    except Exception:
        return default


def _parse_manifest(manifest_path: Path, library_path: Path) -> Optional[SteamGame]:
    try:
        parsed = parse_vdf_file(manifest_path)
    except Exception:
        return None

    state = parsed.get("AppState", parsed)
    if not isinstance(state, dict):
        return None

    appid = str(state.get("appid", "")).strip()
    name = str(state.get("name", "Unknown Game")).strip()
    install_dir = str(state.get("installdir", "")).strip()

    if not appid or not appid.isdigit():
        # Fallback from filename: appmanifest_730.acf
        match = re.search(r"appmanifest_(\d+)\.acf", manifest_path.name)
        if match:
            appid = match.group(1)

    if not appid:
        return None

    size_bytes = _to_int(state.get("SizeOnDisk") or state.get("sizeondisk"), 0)
    last_updated = _to_int(state.get("LastUpdated") or state.get("lastupdated"), 0) or None

    return SteamGame(
        appid=appid,
        name=name or f"App {appid}",
        install_dir=install_dir,
        library_path=str(library_path),
        manifest_path=str(manifest_path),
        size_bytes=size_bytes,
        last_updated=last_updated,
    )


def scan_installed_games(library_paths: Iterable[Path]) -> List[SteamGame]:
    games: List[SteamGame] = []
    for library_path in library_paths:
        steamapps = library_path / "steamapps"
        if not steamapps.exists():
            continue
        for manifest in sorted(steamapps.glob("appmanifest_*.acf")):
            game = _parse_manifest(manifest, library_path)
            if game:
                games.append(game)
    games.sort(key=lambda game: game.name.lower())
    return games


# -----------------------------
# Optional local playtime parsing
# -----------------------------
def _find_apps_dicts(node: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(node, dict):
        if "apps" in node and isinstance(node["apps"], dict):
            yield node["apps"]
        for value in node.values():
            yield from _find_apps_dicts(value)


def get_local_playtime(steam_root: Path) -> Dict[str, Dict[str, int]]:
    """Best-effort parsing of userdata/*/config/localconfig.vdf.

    Steam's localconfig.vdf may contain app entries with Playtime, Playtime2wks and LastPlayed.
    This is not guaranteed for every account/game, so absence is normal.
    """
    result: Dict[str, Dict[str, int]] = {}
    userdata = steam_root / "userdata"
    if not userdata.exists():
        return result

    for user_dir in userdata.iterdir():
        localconfig = user_dir / "config" / "localconfig.vdf"
        if not localconfig.exists():
            continue
        try:
            parsed = parse_vdf_file(localconfig)
        except Exception:
            continue

        for apps in _find_apps_dicts(parsed):
            for appid, values in apps.items():
                if not str(appid).isdigit() or not isinstance(values, dict):
                    continue
                entry = result.setdefault(str(appid), {})
                for source_key, target_key in (
                    ("Playtime", "playtime_minutes"),
                    ("playtime2wks", "playtime_2weeks_minutes"),
                    ("Playtime2wks", "playtime_2weeks_minutes"),
                    ("LastPlayed", "last_played"),
                ):
                    value = _to_int(values.get(source_key), 0)
                    if value:
                        entry[target_key] = max(entry.get(target_key, 0), value)
    return result


def merge_playtime(games: List[SteamGame], playtime: Dict[str, Dict[str, int]]) -> None:
    for game in games:
        data = playtime.get(game.appid, {})
        if data.get("playtime_minutes"):
            game.playtime_minutes = data["playtime_minutes"]
        if data.get("playtime_2weeks_minutes"):
            game.playtime_2weeks_minutes = data["playtime_2weeks_minutes"]
        if data.get("last_played"):
            game.last_played = data["last_played"]


# -----------------------------
# Formatting helpers
# -----------------------------
def human_size(num_bytes: int) -> str:
    if num_bytes <= 0:
        return "Unknown"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(num_bytes)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.1f} {units[idx]}" if idx else f"{int(value)} {units[idx]}"


def human_minutes(minutes: Optional[int]) -> str:
    if minutes is None:
        return "Unknown"
    hours = minutes / 60
    if hours < 1:
        return f"{minutes} min"
    if hours < 100:
        return f"{hours:.1f} h"
    return f"{hours:,.0f} h".replace(",", " ")


def fmt_date(timestamp: Optional[int]) -> str:
    if not timestamp:
        return "Unknown"
    try:
        return _dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
    except Exception:
        return "Unknown"


def safe(value: Any) -> str:
    return html.escape(str(value), quote=True)


# -----------------------------
# Report generation
# -----------------------------
def build_report_html(games: List[SteamGame], library_paths: List[Path], steam_root: Optional[Path]) -> str:
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    total_size = sum(game.size_bytes for game in games)
    known_size_count = sum(1 for game in games if game.size_bytes > 0)
    top_largest = sorted(games, key=lambda game: game.size_bytes, reverse=True)[:8]
    most_played = sorted(
        [game for game in games if game.playtime_minutes is not None],
        key=lambda game: game.playtime_minutes or 0,
        reverse=True,
    )[:8]

    games_payload = []
    for game in games:
        payload = asdict(game)
        payload.update(
            {
                "size_human": human_size(game.size_bytes),
                "playtime_human": human_minutes(game.playtime_minutes),
                "playtime_2weeks_human": human_minutes(game.playtime_2weeks_minutes),
                "last_updated_human": fmt_date(game.last_updated),
                "last_played_human": fmt_date(game.last_played),
                "store_url": game.store_url,
                "header_url": game.header_url,
            }
        )
        games_payload.append(payload)

    games_json = json.dumps(games_payload, ensure_ascii=False)

    if not games:
        empty_state = f"""
        <section class="empty-state">
          <h2>No Steam games found</h2>
          <p>No installed Steam games were found on this machine.</p>
          <p class="muted">Detected Steam root: <code>{safe(steam_root or 'Not found')}</code></p>
          <p>Try running:</p>
          <pre>python generate_report.py --steam "C:\\Program Files (x86)\\Steam"</pre>
        </section>
        """
    else:
        empty_state = ""

    def mini_card(game: SteamGame, value: str) -> str:
        return f"""
        <a class="mini-card" href="{safe(game.store_url)}" target="_blank" rel="noreferrer">
          <img src="{safe(game.header_url)}" alt="{safe(game.name)}" loading="lazy" onerror="this.style.display='none'">
          <span>{safe(game.name)}</span>
          <strong>{safe(value)}</strong>
        </a>
        """

    largest_html = "".join(mini_card(game, human_size(game.size_bytes)) for game in top_largest) or "<p class='muted'>No size data found.</p>"
    played_html = "".join(mini_card(game, human_minutes(game.playtime_minutes)) for game in most_played) or "<p class='muted'>No local playtime data found.</p>"

    libraries_html = "".join(f"<li><code>{safe(path)}</code></li>" for path in library_paths)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Steam Library Report</title>
  <style>
    :root {{
      --bg: #020203;
      --bg-2: #050506;
      --bg-3: #08080b;
      --panel: rgba(10, 10, 14, 0.94);
      --panel-2: rgba(13, 10, 14, 0.88);
      --border: rgba(255, 77, 77, 0.18);
      --text: #f3f4f8;
      --muted: #a9acb8;
      --red: #ff4d4d;
      --red-dark: #9b111e;
      --red-soft: rgba(255, 77, 77, 0.12);
      --shadow: 0 18px 60px rgba(0, 0, 0, 0.58);
      --radius: 22px;
    }}

    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: linear-gradient(135deg, #020203 0%, #050506 50%, #08080b 100%);
      overflow-x: hidden;
    }}

    body::before {{
      content: "";
      position: fixed;
      inset: 0;
      background-image:
        linear-gradient(rgba(255,255,255,0.012) 1px, transparent 1px),
        linear-gradient(90deg, rgba(255,255,255,0.012) 1px, transparent 1px);
      background-size: 42px 42px;
      mask-image: linear-gradient(to bottom, black, transparent 85%);
      pointer-events: none;
    }}

    a {{ color: inherit; text-decoration: none; }}
    code {{ color: #ffd0d0; }}

    .shell {{
      position: relative;
      width: min(1180px, calc(100% - 32px));
      margin: 0 auto;
      padding: 36px 0 54px;
    }}

    .hero {{
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 24px;
      align-items: stretch;
      margin-bottom: 24px;
    }}

    .hero-card, .card {{
      background: linear-gradient(145deg, var(--panel), rgba(11, 12, 18, 0.88));
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      backdrop-filter: blur(14px);
    }}

    .hero-card {{
      padding: 34px;
      overflow: hidden;
      position: relative;
    }}

    .hero-card::after {{ display: none; }}

    h1 {{
      margin: 0 0 10px;
      font-size: clamp(38px, 7vw, 76px);
      line-height: 0.92;
      letter-spacing: -0.06em;
    }}

    h1 span {{
      color: var(--red);
      text-shadow: none;
    }}

    .subtitle {{
      max-width: 680px;
      color: var(--muted);
      font-size: 16px;
      line-height: 1.7;
      margin: 0;
    }}

    .meta {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 22px;
    }}

    .pill {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 9px 12px;
      border: 1px solid var(--border);
      border-radius: 999px;
      color: #f7caca;
      background: rgba(255, 255, 255, 0.035);
      font-size: 13px;
    }}

    .stats-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }}

    .stat {{
      padding: 20px;
      min-height: 120px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      background: linear-gradient(145deg, var(--panel-2), rgba(13, 17, 23, 0.85));
      border: 1px solid var(--border);
      border-radius: 18px;
    }}

    .stat small {{ color: var(--muted); text-transform: uppercase; letter-spacing: 0.12em; font-weight: 700; font-size: 11px; }}
    .stat strong {{ font-size: 30px; letter-spacing: -0.04em; }}

    .section-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 24px;
      margin-bottom: 24px;
    }}

    .card {{ padding: 22px; }}
    .card h2 {{ margin: 0 0 16px; font-size: 18px; letter-spacing: -0.02em; }}
    .muted {{ color: var(--muted); }}

    .mini-list {{ display: grid; gap: 10px; }}
    .mini-card {{
      display: grid;
      grid-template-columns: 104px 1fr auto;
      gap: 12px;
      align-items: center;
      padding: 10px;
      border-radius: 16px;
      background: rgba(255, 255, 255, 0.035);
      border: 1px solid rgba(255, 255, 255, 0.06);
      transition: 160ms ease;
    }}
    .mini-card:hover {{ transform: translateY(-2px); border-color: var(--border); background: rgba(255, 77, 77, 0.07); }}
    .mini-card img {{ width: 104px; aspect-ratio: 16 / 7; object-fit: cover; border-radius: 10px; background: #111; }}
    .mini-card span {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .mini-card strong {{ color: #ffd0d0; font-size: 13px; }}

    .toolbar {{
      display: grid;
      grid-template-columns: 1fr 180px;
      gap: 12px;
      margin-bottom: 16px;
    }}

    input, select {{
      width: 100%;
      border: 1px solid var(--border);
      background: rgba(0, 0, 0, 0.28);
      color: var(--text);
      border-radius: 14px;
      padding: 13px 14px;
      outline: none;
      font: inherit;
    }}

    input:focus, select:focus {{ box-shadow: 0 0 0 3px rgba(255, 77, 77, 0.12); }}

    .games-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
    }}

    .game-card {{
      border-radius: 18px;
      overflow: hidden;
      border: 1px solid rgba(255, 255, 255, 0.065);
      background: rgba(255, 255, 255, 0.035);
      transition: 160ms ease;
    }}
    .game-card:hover {{ transform: translateY(-3px); border-color: var(--border); }}
    .game-card img {{ width: 100%; aspect-ratio: 16 / 7; object-fit: cover; display: block; background: #101116; }}
    .game-body {{ padding: 13px; }}
    .game-title {{ font-weight: 800; line-height: 1.3; margin-bottom: 10px; }}
    .game-details {{ display: grid; gap: 6px; color: var(--muted); font-size: 12px; }}
    .game-details b {{ color: #ffd0d0; font-weight: 700; }}

    .footer {{
      text-align: center;
      color: var(--muted);
      margin-top: 18px;
      font-size: 13px;
    }}

    .empty-state {{
      background: rgba(255, 77, 77, 0.08);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 24px;
      margin-bottom: 24px;
    }}

    ul {{ margin: 0; padding-left: 20px; color: var(--muted); }}
    li {{ margin: 8px 0; }}
    pre {{ overflow: auto; background: #0d1117; padding: 14px; border-radius: 12px; }}

    @media (max-width: 920px) {{
      .hero, .section-grid {{ grid-template-columns: 1fr; }}
      .games-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
    }}

    @media (max-width: 640px) {{
      .shell {{ width: min(100% - 20px, 1180px); padding-top: 20px; }}
      .hero-card, .card {{ padding: 18px; }}
      .stats-grid {{ grid-template-columns: 1fr; }}
      .games-grid {{ grid-template-columns: 1fr; }}
      .toolbar {{ grid-template-columns: 1fr; }}
      .mini-card {{ grid-template-columns: 86px 1fr; }}
      .mini-card strong {{ grid-column: 2; }}
      .mini-card img {{ width: 86px; }}
    }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="hero-card">
        <h1>Steam<span>Library</span></h1>
        <p class="subtitle">A local report generator that scans installed Steam games, storage usage, library folders and optional playtime data from this computer.</p>
        <div class="meta">
          <span class="pill">Generated: {safe(now)}</span>
          <span class="pill">Steam root: {safe(steam_root or 'Not found')}</span>
        </div>
      </div>

      <div class="stats-grid">
        <div class="stat"><small>Installed games</small><strong>{len(games)}</strong></div>
        <div class="stat"><small>Total size</small><strong>{safe(human_size(total_size))}</strong></div>
        <div class="stat"><small>Libraries</small><strong>{len(library_paths)}</strong></div>
        <div class="stat"><small>Known sizes</small><strong>{known_size_count}</strong></div>
      </div>
    </section>

    {empty_state}

    <section class="section-grid">
      <div class="card">
        <h2>Largest installed games</h2>
        <div class="mini-list">{largest_html}</div>
      </div>
      <div class="card">
        <h2>Most played locally</h2>
        <div class="mini-list">{played_html}</div>
      </div>
    </section>

    <section class="card">
      <h2>Installed library</h2>
      <div class="toolbar">
        <input id="search" type="search" placeholder="Search a game...">
        <select id="sort">
          <option value="name">Sort by name</option>
          <option value="size">Sort by size</option>
          <option value="playtime">Sort by playtime</option>
          <option value="updated">Sort by last update</option>
        </select>
      </div>
      <div id="games" class="games-grid"></div>
    </section>

    <section class="card" style="margin-top: 24px;">
      <h2>Detected library folders</h2>
      <ul>{libraries_html}</ul>
    </section>

    <p class="footer">local report · installed games · storage overview</p>
  </main>

  <script>
    const games = {games_json};
    const grid = document.querySelector('#games');
    const searchInput = document.querySelector('#search');
    const sortSelect = document.querySelector('#sort');

    function bySort(mode) {{
      return (a, b) => {{
        if (mode === 'size') return (b.size_bytes || 0) - (a.size_bytes || 0);
        if (mode === 'playtime') return (b.playtime_minutes || 0) - (a.playtime_minutes || 0);
        if (mode === 'updated') return (b.last_updated || 0) - (a.last_updated || 0);
        return a.name.localeCompare(b.name);
      }};
    }}

    function render() {{
      const query = searchInput.value.trim().toLowerCase();
      const mode = sortSelect.value;
      const filtered = games
        .filter(game => game.name.toLowerCase().includes(query) || game.appid.includes(query))
        .sort(bySort(mode));

      grid.innerHTML = filtered.map(game => `
        <a class="game-card" href="${{game.store_url}}" target="_blank" rel="noreferrer">
          <img src="${{game.header_url}}" alt="${{escapeHtml(game.name)}}" loading="lazy" onerror="this.style.display='none'">
          <div class="game-body">
            <div class="game-title">${{escapeHtml(game.name)}}</div>
            <div class="game-details">
              <span><b>Size:</b> ${{game.size_human}}</span>
              <span><b>Playtime:</b> ${{game.playtime_human}}</span>
              <span><b>Updated:</b> ${{game.last_updated_human}}</span>
              <span><b>AppID:</b> ${{game.appid}}</span>
            </div>
          </div>
        </a>
      `).join('') || `<p class="muted">No game matches your search.</p>`;
    }}

    function escapeHtml(str) {{
      return String(str).replace(/[&<>'"]/g, char => ({{
        '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;'
      }}[char]));
    }}

    searchInput.addEventListener('input', render);
    sortSelect.addEventListener('change', render);
    render();
  </script>
</body>
</html>"""


def write_report(html_text: str, output: Path) -> Path:
    output.write_text(html_text, encoding="utf-8")
    return output


def export_json(games: List[SteamGame], output: Path) -> Path:
    payload = [asdict(game) for game in games]
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


# -----------------------------
# CLI
# -----------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="generate_report",
        description="Generate a local Steam library HTML report.",
    )
    parser.add_argument("--steam", help="Path to your Steam installation folder, if auto-detection fails.")
    parser.add_argument("--output", help=f"Output HTML file. Default: {REPORT_NAME} next to this script.")
    parser.add_argument("--json", action="store_true", help="Also export steam_library_report.json next to the HTML report.")
    parser.add_argument("--no-open", action="store_true", help="Do not automatically open the generated report.")
    args = parser.parse_args(argv)

    script_dir = Path(__file__).resolve().parent
    output_path = Path(args.output).expanduser() if args.output else script_dir / REPORT_NAME
    if not output_path.is_absolute():
        output_path = (Path.cwd() / output_path).resolve()

    print("\nSteam Library Report")
    print("----------------")
    print("Scanning local Steam files...\n")

    steam_root = find_steam_root(args.steam)
    if steam_root:
        print(f"Steam root: {steam_root}")
        library_paths = get_library_paths(steam_root)
        games = scan_installed_games(library_paths)
        merge_playtime(games, get_local_playtime(steam_root))
    else:
        print("Steam root: not found")
        library_paths = []
        games = []

    html_text = build_report_html(games, library_paths, steam_root)
    write_report(html_text, output_path)

    if args.json:
        json_path = output_path.with_suffix(".json")
        export_json(games, json_path)
        print(f"JSON exported: {json_path}")

    print(f"Games found: {len(games)}")
    print(f"Report generated: {output_path}")

    if not args.no_open:
        try:
            webbrowser.open(output_path.as_uri())
            print("Opening report in your browser...")
        except Exception:
            print("Could not open browser automatically. Open the HTML file manually.")

    print("\nDone.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
