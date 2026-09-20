#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

# CLIX: A Plex Terminal Media Player
# Browse and play Plex media from the command line
#
# Developed by Jereme Hancock
# https://github.com/jeremehancock/CLIX
#
# MIT License
#
# Copyright (c) 2024 Jereme Hancock
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from __future__ import annotations

import getopt
import locale
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import TypeVar
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

VERSION = "1.4.0"
SCRIPT_DIR = Path(__file__).resolve().parent
T = TypeVar("T")


class ClixError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    url: str
    token: str = field(repr=False)
    path: Path

    @classmethod
    def load(cls) -> Config:
        path = Path(
            os.environ.get("CLIX_CONFIG")
            or (Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "clix/config")
        )
        # The existing config is a trusted Bash file, including shell expansions.
        result = subprocess.run(
            [
                "bash",
                "-c",
                """
PLEX_URL="http://localhost:32400"
PLEX_TOKEN=""
if [[ -r "$1" ]]; then source "$1" >&2 || exit; fi
printf '%s\\0%s' "${CLIX_PLEX_URL:-$PLEX_URL}" "${CLIX_PLEX_TOKEN:-$PLEX_TOKEN}"
""",
                "clix",
                str(path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode or "\0" not in result.stdout:
            raise ClixError(f"Could not load configuration: {path}")
        url, token = result.stdout.split("\0", 1)
        if any(c in token for c in "\r\n\0"):
            raise ClixError("Invalid Plex token")
        return cls(url.rstrip("/"), token, path)


def clear() -> None:
    subprocess.run(["clear"], check=False)


def pause() -> None:
    input("Press Enter to continue...")


def fzf_menu(
    entries: list[tuple[str, T]], header: str, *, disabled: bool = False
) -> tuple[str, T] | None:
    # Hidden row IDs preserve identity even when two items have the same title.
    args = [
        "fzf",
        "--expect=ctrl-c",
        "--ansi",
        "--color=fg:#eeeeee,bg:-1,fg+:#eeeeee:bold,bg+:-1,hl:#ff0000:bold,hl+:#ff0000:bold,"
        "pointer:#ff0000,prompt:#ff0000,header:#666666,info:#666666,separator:#444444,gutter:-1",
        "--pointer=›",
        "--layout=default",
        "--no-scrollbar",
        "--border=none",
        "--prompt=> ",
        f"--header={header.lower()}",
        "--delimiter=\t",
        "--with-nth=2..",
    ]
    if disabled:
        args.append("--disabled")
    labels = [" ".join(label.lower().split()) for label, _ in entries]
    width = min(max((len(label) for label in labels), default=0), 48)
    rows = []
    for i, ((_, item), label) in enumerate(zip(entries, labels)):
        details = ""
        if isinstance(item, ET.Element):
            genres = ", ".join(genre.get("tag", "") for genre in item.findall("Genre"))
            details = ". ".join(
                value for value in (item.get("year", ""), genres, item.get("summary", "")) if value
            )
        if details:
            label = f"{label:<{width}}  \033[90m{' '.join(details.lower().split())}\033[0m"
        rows.append(f"{i}\t{label}\n")
    result = subprocess.run(args, input="".join(rows), text=True, stdout=subprocess.PIPE)
    key, _, selection = result.stdout.partition("\n")
    if key == "ctrl-c":
        raise KeyboardInterrupt
    if result.returncode in (1, 130):
        return None
    if result.returncode:
        raise ClixError(f"fzf exited with status {result.returncode}")
    if not selection.strip():
        return None
    try:
        return entries[int(selection.split("\t", 1)[0])]
    except (ValueError, IndexError) as exc:
        raise ClixError("Could not read fzf selection") from exc


def choose(labels: list[str], header: str) -> str:
    selected = fzf_menu([(label, label) for label in labels], header)
    return selected[0] if selected else ""


def empty_menu(header: str) -> None:
    fzf_menu([("< Go back", None)], header, disabled=True)
    clear()


def number(item: ET.Element, name: str) -> int:
    return int(item.get(name) or 0)


def format_time(milliseconds: int) -> str:
    total = milliseconds // 1000
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def natural_key(value: str) -> list:
    return [
        int(part) if part.isdigit() else locale.strxfrm(part) for part in re.split(r"(\d+)", value)
    ]


def episode_title(item: ET.Element) -> str:
    return (
        f"{item.get('grandparentTitle', '')} - "
        f"S{number(item, 'parentIndex'):02}E{number(item, 'index'):02} - "
        f"{item.get('title', '')}"
    )


def progress_label(item: ET.Element, *, next_up: bool = False) -> str:
    offset = number(item, "viewOffset")
    if offset:
        return f"  [{format_time(offset)} / {format_time(number(item, 'duration'))}]"
    return "  [next up]" if next_up else ""


def safe_name(value: str) -> str:
    value = value.replace("/", "*").replace('"', "").replace(":", "-")
    value = value.replace("\0", "").replace("\n", " ").replace("\r", " ")
    return "_" if value in ("", ".", "..") else value


class Plex:
    def __init__(self, config: Config):
        self.config = config
        self.headers = {
            "X-Plex-Token": config.token,
            "X-Plex-Client-Identifier": f"clix-{socket.gethostname()}",
            "Accept": "application/xml",
        }
        self.cache: dict[str, tuple[float, ET.Element]] = {}

    def request(self, path: str, *, auth: bool = True) -> bytes:
        try:
            request = Request(self.config.url + path, headers=self.headers if auth else {})
            with urlopen(request, timeout=10) as response:
                return response.read()
        except (OSError, ValueError, URLError) as exc:
            # Do not include response bodies or headers, which may contain credentials.
            raise ClixError("Error: No response from Plex server.") from exc

    def query(self, path: str, *, cached: bool = False) -> ET.Element:
        saved = self.cache.get(path)
        if cached and saved and time.monotonic() - saved[0] < 3600:
            return saved[1]
        try:
            root = ET.fromstring(self.request(path))
        except ET.ParseError as exc:
            raise ClixError("Error: Invalid response from Plex server.") from exc
        if root.tag != "MediaContainer":
            raise ClixError("Error: Invalid response from Plex server.")
        if cached:
            self.cache[path] = (time.monotonic(), root)
        return root

    def check_connection(self) -> None:
        print("Checking Plex server connection...")
        if not self.config.url or not self.config.token:
            raise ClixError(
                "Error: Plex URL or token not set\n"
                f"Set PLEX_URL and PLEX_TOKEN in {self.config.path}"
            )
        try:
            if not self.request("/identity", auth=False):
                raise ClixError("Empty identity response")
        except ClixError as exc:
            raise ClixError(
                f"Error: Could not connect to Plex server at {self.config.url}\n"
                "Please check if:\n1. The Plex server URL is correct\n"
                "2. The Plex server is running\n3. Your network connection is working"
            ) from exc
        try:
            libraries = self.query("/library/sections", cached=True)
        except ClixError as exc:
            raise ClixError(
                "Error: Invalid Plex token or unauthorized access\n"
                "The server is reachable, but the provided token does not have proper access permissions\n"
                "Please check your Plex token and try again"
            ) from exc
        server = self.query("/")
        print(
            f"Successfully connected to Plex server: {server.get('friendlyName') or server.get('title', 'Unknown')}"
        )
        print(f"Found {len(libraries.findall('Directory'))} available libraries")
        time.sleep(2)

    def library_contents(self, library: ET.Element) -> list[ET.Element]:
        path = f"/library/sections/{library.get('key')}/all"
        saved = self.cache.get(path)
        if saved and time.monotonic() - saved[0] < 3600:
            return list(saved[1])
        first = self.query(path + "?X-Plex-Container-Start=0&X-Plex-Container-Size=1")
        total = number(first, "totalSize") or number(first, "size")
        if not total:
            return []
        print(f"Retrieving contents of library: {library.get('title', '')}", file=sys.stderr)
        print(f"Total items: {total}", file=sys.stderr)
        clear()
        contents = ET.Element("MediaContainer")
        start = 0
        while start < total:
            page = self.query(
                path
                + "?"
                + urlencode(
                    {
                        "X-Plex-Container-Start": start,
                        "X-Plex-Container-Size": 50,
                    }
                )
            )
            items = [item for item in page if item.tag in ("Video", "Directory")]
            if not items:
                break
            contents.extend(items)
            start += len(items)
            current = min(start, total)
            percent = current * 100 // total
            print(
                f"\rRetrieving items: [{'#' * (percent // 2):50}] {percent}% ({current}/{total})",
                end="",
                file=sys.stderr,
                flush=True,
            )
        print(file=sys.stderr)
        clear()
        self.cache[path] = (time.monotonic(), contents)
        return list(contents)

    def metadata(self, key: str) -> ET.Element:
        root = self.query(f"/library/metadata/{key}")
        item = next((child for child in root if child.tag in ("Video", "Track")), None)
        if item is None:
            raise ClixError("Error: Could not retrieve media metadata.")
        return item

    def stream_url(self, item: ET.Element) -> str:
        part = item.find(".//Part")
        key = part.get("key", "") if part is not None else ""
        if not key.startswith("/") or key.startswith("//"):
            raise ClixError("Error: Could not retrieve stream URL.")
        return self.config.url + key

    def report_progress(self, key: str, position: str | None, duration: int) -> None:
        params = {"identifier": "com.plexapp.plugins.library"}
        if position is None:
            path = "/:/scrobble"
            params["key"] = key
        else:
            path = "/:/timeline"
            params.update(
                ratingKey=key,
                key=f"/library/metadata/{key}",
                state="stopped",
                time=str(int(float(position)) * 1000),
                duration=str(duration),
            )
        self.request(path + "?" + urlencode(params))


class Clix:
    def __init__(self, config: Config):
        self.plex = Plex(config)
        self.downloads = SCRIPT_DIR / "downloads"

    def browse_library(self, kind: str) -> None:
        library_name, item_name, plex_type = {
            "movie": ("Movie", "Movie", "movie"),
            "show": ("TV Show", "TV Show", "show"),
            "music": ("Music", "Artist", "artist"),
        }[kind]
        libraries = [
            (item.get("title", ""), item)
            for item in self.plex.query("/library/sections", cached=True)
            if item.get("type") == plex_type
        ]
        if not libraries:
            print(
                f"No { {'movie': 'movie', 'show': 'TV show', 'music': 'music'}[kind] } libraries found."
            )
            return
        while True:
            selected = (
                libraries[0]
                if len(libraries) == 1
                else fzf_menu(
                    libraries,
                    f"Select {library_name} Library",
                )
            )
            if selected is None:
                return
            items = self.plex.library_contents(selected[1])
            if items:
                self.browse_items(items, item_name)
            else:
                empty_menu("Library Empty")
            if len(libraries) == 1:
                return

    def browse_items(
        self, items: list[ET.Element], name: str, context: str = "", show_key: str = ""
    ) -> None:
        while True:
            entries = []
            if show_key:
                on_deck = self.plex.query(f"/library/metadata/{show_key}?includeOnDeck=1").find(
                    ".//OnDeck/Video"
                )
                if on_deck is not None:
                    label = (
                        f"Continue: S{number(on_deck, 'parentIndex'):02}E{number(on_deck, 'index'):02}"
                        f" - {on_deck.get('title', '')}{progress_label(on_deck)}"
                    )
                    entries.append((label, on_deck))
            for item in items:
                label = item.get("title", "")
                if name in ("Episode", "Track"):
                    label = f"{item.get('index', '')}. {label}"
                entries.append((label, item))
            selected = fzf_menu(entries, f"{context}Select {name}")
            if selected is None:
                return
            label, item = selected
            key = item.get("ratingKey", "")
            kind = item.get("type", "")
            if kind in ("movie", "episode", "track"):
                self.handle_media(key, "music" if kind == "track" else kind, label)
                continue
            children = list(self.plex.query(f"/library/metadata/{key}/children", cached=True))
            if name == "TV Show":
                children = [
                    child
                    for child in children
                    if child.get("type") == "season" and child.get("title") != "All episodes"
                ]
                children.sort(key=lambda child: natural_key(child.get("title", "")))
                self.browse_items(children, "Season", f"TV Show: {label}\n", key)
            elif name == "Season":
                self.browse_items(children, "Episode", f"{context}Season: {label}\n")
            elif name == "Artist":
                self.browse_items(children, "Album", f"Artist: {label}\n")
            elif name == "Album":
                self.browse_items(children, "Track", f"{context}Album: {label}\n")

    def continue_watching(self) -> None:
        while True:
            try:
                root = self.plex.query(
                    "/hubs/continueWatching?X-Plex-Container-Start=0&X-Plex-Container-Size=50"
                )
            except ClixError:
                root = self.plex.query("/library/onDeck")
            entries = []
            for item in root.iter("Video"):
                if not item.get("ratingKey"):
                    continue
                label = (
                    episode_title(item) if item.get("type") == "episode" else item.get("title", "")
                )
                entries.append((label + progress_label(item, next_up=True), item))
            if not entries:
                empty_menu("Nothing in progress")
                return
            selected = fzf_menu(entries, "Continue Watching")
            if selected is None:
                clear()
                return
            item = selected[1]
            self.handle_media(
                item.get("ratingKey", ""), item.get("type", ""), item.get("title", "")
            )

    def media_path(self, item: ET.Element, kind: str, title: str) -> tuple[Path, str]:
        if kind == "movie":
            name = re.sub(r" \(\d{4}\)$", "", title)
            if item.get("year"):
                name += f" ({item.get('year')})"
            return self.downloads / "movies", name
        parent = item.get("grandparentTitle", "")
        collection = item.get("parentTitle", "")
        directory = self.downloads / ("shows" if kind == "episode" else "music")
        directory = directory / safe_name(parent) / safe_name(collection)
        name = (
            episode_title(item)
            if kind == "episode"
            else (f"{parent} - {collection} - {number(item, 'index'):02} - {item.get('title', '')}")
        )
        return directory, name

    def handle_media(self, key: str, kind: str, title: str) -> None:
        item = self.plex.metadata(key)
        if kind == "episode":
            title = episode_title(item)
        elif kind == "music":
            title = f"{item.get('grandparentTitle', '')} - {item.get('parentTitle', '')} - {title}"
        directory, filename = self.media_path(item, kind, title)
        extensions = {".mp3", ".flac", ".m4a"} if kind == "music" else {".mkv", ".mp4", ".avi"}
        local_file = next(
            (
                path
                for path in directory.rglob("*")
                if path.is_file()
                and path.suffix.lower() in extensions
                and path.stem == safe_name(filename)
            ),
            None,
        )
        options = ["Play from Plex", "Download"]
        if local_file:
            options.insert(0, "Play Local File")
        offset = number(item, "viewOffset") if kind != "music" else 0
        if offset:
            options.insert(0, f"Resume from {format_time(offset)}")
        action = choose(options + ["Cancel"], f"Select Action for: {title}")
        if not action or action == "Cancel":
            return
        clear()
        if action == "Play Local File":
            self.play_local(local_file, title)
        elif action == "Download":
            self.download_media(item, kind, title)
        else:
            print(f"Playing {kind}: {title}")
            self.play_stream(item, title, offset // 1000 if action.startswith("Resume from") else 0)
            clear()

    def run_player(self, args: list[str]) -> int:
        # mpv handles Ctrl-C itself; CLIX must survive it to return to the menu.
        previous = signal.signal(signal.SIGINT, lambda *_: None)
        try:
            return subprocess.run(["mpv", *args], check=False).returncode
        finally:
            signal.signal(signal.SIGINT, previous)

    def play_local(self, path: Path, title: str) -> None:
        self.run_player([f"--title={title}", str(path)])
        clear()

    def play_stream(self, item: ET.Element, title: str, start: int) -> None:
        url = self.plex.stream_url(item)
        with tempfile.TemporaryDirectory(prefix="clix-") as temporary:
            watch_dir = Path(temporary) / "watch_later"
            watch_dir.mkdir()
            config = Path(temporary) / "mpv.conf"
            token = self.plex.config.token
            # mpv's length-prefixed quoting keeps arbitrary token characters literal.
            header = f"X-Plex-Token: {token}"
            config.write_text(f"http-header-fields=%{len(header.encode())}%{header}\n")
            args = [
                f"--include={config}",
                f"--title={title}",
                "--no-resume-playback",
                "--save-position-on-quit",
                f"--watch-later-dir={watch_dir}",
            ]
            if start > 0:
                args.append(f"--start={start}")
            status = self.run_player([*args, url])
            position = None
            for path in watch_dir.iterdir():
                for line in path.read_text().splitlines():
                    if line.startswith("start="):
                        position = line.removeprefix("start=")
            if item.tag != "Track" and (position is not None or status == 0):
                self.plex.report_progress(
                    item.get("ratingKey", ""), position, number(item, "duration")
                )

    def download_media(self, item: ET.Element, kind: str, title: str) -> None:
        answer = input("Do you want to proceed with the download? [y/N] ")
        clear()
        if answer.lower() != "y":
            print("Download cancelled.")
            pause()
            return
        url = self.plex.stream_url(item)
        extension = Path(urlsplit(url).path).suffix or ".mp4"
        directory, filename = self.media_path(item, kind, title)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / safe_name(filename + extension)
        display = (filename + extension).replace('"', "").replace(":", "-")
        print(f"Downloading: {display}\nDestination: {destination}", flush=True)
        token = self.plex.config.token.replace("\\", "\\\\").replace('"', '\\"')
        # curl keeps its existing progress bar; credentials travel over stdin.
        result = subprocess.run(
            ["curl", "-#", "-L", "--fail", "--config", "-", "-o", str(destination), url],
            input=f'header = "X-Plex-Token: {token}"\n',
            text=True,
        )
        print("Download completed successfully!" if result.returncode == 0 else "Download failed!")
        if result.returncode == 0:
            pause()

    def downloads_menu(self) -> None:
        while choice := choose(
            ["Movies", "TV Shows", "Music"], "Downloads Menu"
        ):
            folder, levels, empty = {
                "Movies": ("movies", ("Movie",), "movies"),
                "TV Shows": ("shows", ("TV Show", "Season", "Episode"), "TV shows"),
                "Music": ("music", ("Artist", "Album", "Track"), "music"),
            }[choice]
            directory = self.downloads / folder
            if not directory.is_dir() or not any(directory.iterdir()):
                empty_menu(f"No downloaded {empty} found")
            else:
                self.browse_downloads(directory, levels)

    def browse_downloads(
        self,
        directory: Path,
        levels: tuple[str, ...],
        parents: tuple[str, ...] = (),
        context: str = "",
    ) -> None:
        name = levels[0]
        while True:
            paths = (
                [path for path in directory.rglob("*") if path.is_file()]
                if len(levels) == 1
                else [path for path in directory.iterdir() if path.is_dir()]
            )
            if not paths:
                empty_menu(f"No {name.lower()}s found")
                return
            if name in ("Movie", "TV Show"):
                paths.sort(key=lambda path: locale.strxfrm((path.name.split()[1:2] or [""])[0]))
            elif name in ("Season", "Episode", "Track"):
                paths.sort(key=lambda path: natural_key(path.name))
            else:
                paths.sort(key=lambda path: locale.strxfrm(path.name))
            entries = []
            for path in paths:
                label = path.stem if len(levels) == 1 else path.name
                pattern = {"Episode": r"S\d+E(\d+) - (.+)$", "Track": r"- (\d+) - (.+)$"}.get(name)
                match = re.search(pattern, label) if pattern else None
                label = f"{int(match[1])}. {match[2]}" if match else label.replace("*", "/")
                entries.append((label, path))
            selected = fzf_menu(
                entries, f"{context}Select Downloaded {name}"
            )
            if selected is None:
                return
            label, path = selected
            if len(levels) > 1:
                self.browse_downloads(
                    path, levels[1:], (*parents, label), f"{context}{name}: {label}\n"
                )
            else:
                title = " - ".join((*parents, label))
                if name == "Episode":
                    match = re.search(r"S(\d{2})E(\d{2}) - (.+)$", path.stem)
                    if match:
                        title = f"{parents[0]} - S{match[1]}E{match[2]} - {match[3]}"
                self.play_local(path, title)

    def run(self) -> None:
        self.plex.check_connection()
        for name in ("movies", "shows", "music"):
            (self.downloads / name).mkdir(parents=True, exist_ok=True)
        clear()
        print("-" * 73)
        print(f"CLIX v{VERSION}")
        print("Tip: Press ESC to go back to previous menu, or select Help for more info")
        print("-" * 73)
        time.sleep(2)
        clear()
        labels = [
            "Continue Watching",
            "----------",
            "Movies",
            "TV Shows",
            "Music",
            "Downloads",
            "----------",
            "Help",
            "----------",
            "Quit",
        ]
        while True:
            choice = choose(labels, "Select Media Type")
            try:
                if not choice or choice == "Quit":
                    if not choice:
                        clear()
                    return
                if choice == "Continue Watching":
                    self.continue_watching()
                elif choice in ("Movies", "TV Shows", "Music"):
                    self.browse_library(
                        {"Movies": "movie", "TV Shows": "show", "Music": "music"}[choice]
                    )
                elif choice == "Downloads":
                    self.downloads_menu()
                elif choice == "Help":
                    clear()
                    subprocess.run(["less", "-R"], input=HELP, text=True)
                    clear()
            except ClixError as exc:
                print(exc, file=sys.stderr)
                pause()
                clear()


HELP = f"""CLIX v{VERSION} - Guide

OPTIONS:
    -h          Show this help message
    -v          Show version information

NAVIGATION:
    ↑/↓         Move up/down in menus
    Enter       Select current item
    ESC         Go back to previous menu, or exit from the main menu
    Ctrl+C      Exit the program or Exit from Music track
    Type to search   Fuzzy finding in any menu

MENU STRUCTURE:
    1. Main Menu
        - Continue Watching
        - Movies
        - TV Shows
        - Music
        - Downloads
        - Help
        - Quit

    2. Library Selection
        → Select your preferred library

        If there is only one library of the selected
        library type it will be auto selected

    3. Media Selection
        Movies: Select movie from list
        TV Shows: Select show → season → episode
                  (a show's next-up episode is listed
                   above its seasons)
        Music: Select artist → album → track

RESUMING:
    Anything partly watched offers "Resume from ..."
    alongside playing from the start. Quitting mpv with q
    saves your position back to Plex; watching to the end
    marks the item watched.

DEPENDENCIES:
    Required: uv, bash, curl, fzf, mpv, less, clear

Press q to return to main menu
"""


def main() -> int:
    try:
        options, _ = getopt.getopt(sys.argv[1:], "hv")
    except getopt.GetoptError as exc:
        print(f"Invalid Option: -{exc.opt}", file=sys.stderr)
        print(HELP)
        return 1
    for option, _ in options:
        if option == "-h":
            print(HELP, end="")
        elif option == "-v":
            print(f"CLIX v{VERSION}")
        return 0
    missing = [
        name for name in ("bash", "curl", "fzf", "mpv", "less", "clear") if not shutil.which(name)
    ]
    if missing:
        print(
            f"Missing required dependencies: {' '.join(missing)}\nPlease install them and try again."
        )
        return 1
    locale.setlocale(locale.LC_ALL, "")
    Clix(Config.load()).run()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        clear()
        sys.exit(130)
    except EOFError:
        sys.exit(0)
    except (ClixError, OSError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
