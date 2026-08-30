#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["textual>=5.0", "plexapi>=4.15"]
# ///
"""plextui - browse and play plex media in the terminal."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import tempfile
import time
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from plexapi.server import PlexServer
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.coordinate import Coordinate
from textual.theme import Theme
from textual.widgets import DataTable, Input, Label, Select

PLAYABLE = {"movie", "episode", "track", "clip"}
CHILDREN = {"show": "seasons", "season": "episodes", "artist": "albums", "album": "tracks"}
ANY = "\0any"

NOIR = Theme(
    name="noir",
    primary="#c8c8c8",
    secondary="#8f8f8f",
    accent="#ededed",
    foreground="#cbcbcb",
    background="#0d0d0d",
    surface="#151515",
    panel="#1e1e1e",
    boost="#ffffff0d",
    success="#a8a8a8",
    warning="#c4c4c4",
    error="#efefef",
    dark=True,
    variables={
        "block-cursor-foreground": "#0d0d0d",
        "block-cursor-background": "#cbcbcb",
        "block-cursor-text-style": "none",
        "input-selection-background": "#3a3a3a",
    },
)


def load_config() -> tuple[str, str]:
    """read plex url/token from the clix config, env wins."""
    path = Path(
        os.environ.get("CLIX_CONFIG")
        or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "clix" / "config"
    )
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            if m := re.match(r'\s*(PLEX_URL|PLEX_TOKEN)\s*=\s*"?([^"]*)"?\s*$', line):
                values[m[1]] = m[2]
    url = os.environ.get("CLIX_PLEX_URL") or values.get("PLEX_URL", "")
    token = os.environ.get("CLIX_PLEX_TOKEN") or values.get("PLEX_TOKEN", "")
    return url, token


def duration(ms: int | None) -> str:
    if not ms:
        return ""
    total = ms // 1000
    if total >= 3600:
        return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"
    return f"{total // 60}:{total % 60:02d}"


def progress_mark(item) -> str:
    if getattr(item, "viewOffset", None):
        pct = int(item.viewOffset / item.duration * 100) if item.duration else 0
        return f"{pct}%"
    if getattr(item, "viewCount", 0):
        return "✓"
    total = getattr(item, "leafCount", None)
    seen = getattr(item, "viewedLeafCount", None)
    if total is not None and seen is not None:
        return f"{total - seen} left" if total > seen else "✓"
    return ""


# columns per plex type: (heading, width, cell function)
LAYOUTS = {
    "movie": [("Title", 0, lambda i: i.title), ("Year", 6, lambda i: i.year or ""),
              ("Rating", 6, lambda i: i.contentRating or ""),
              ("Length", 8, lambda i: duration(i.duration)), ("", 8, progress_mark)],
    "show": [("Show", 0, lambda i: i.title), ("Year", 6, lambda i: i.year or ""),
             ("Seasons", 8, lambda i: i.childCount or ""), ("", 8, progress_mark)],
    "season": [("Season", 0, lambda i: i.title),
               ("Episodes", 9, lambda i: i.leafCount or ""), ("", 8, progress_mark)],
    "episode": [("#", 5, lambda i: f"{i.parentIndex}x{i.index:02d}"),
                ("Title", 0, lambda i: i.title),
                ("Length", 8, lambda i: duration(i.duration)), ("", 8, progress_mark)],
    "artist": [("Artist", 0, lambda i: i.title), ("Albums", 7, lambda i: i.childCount or "")],
    "album": [("Album", 0, lambda i: i.title), ("Year", 6, lambda i: i.year or ""),
              ("Tracks", 7, lambda i: i.leafCount or "")],
    "track": [("#", 4, lambda i: i.index or ""), ("Title", 0, lambda i: i.title),
              ("Album", 30, lambda i: i.parentTitle or ""),
              ("Length", 8, lambda i: duration(i.duration))],
}
LAYOUTS["clip"] = LAYOUTS["movie"]


def mpv_time_pos(ipc: str) -> float | None:
    """ask a running mpv where it is; the socket appears a moment after launch."""
    try:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(0.5)
            sock.connect(ipc)
            sock.sendall(b'{"command":["get_property","time-pos"]}\n')
            data = sock.recv(4096)
    except OSError:
        return None
    for line in data.splitlines():
        try:
            reply = json.loads(line)
        except ValueError:
            continue
        if reply.get("error") == "success" and isinstance(reply.get("data"), (int, float)):
            return reply["data"]
    return None


def num(item, attr: str) -> float:
    return getattr(item, attr, 0) or 0


# clickable column heading -> plex sort key
SORTS = {
    "Title": "titleSort", "Show": "titleSort", "Season": "index",
    "Artist": "titleSort", "Album": "titleSort", "Year": "year",
    "Rating": "contentRating", "Length": "duration", "#": "index",
    "Seasons": "childCount", "Albums": "childCount",
    "Episodes": "leafCount", "Tracks": "leafCount",
}
# in-memory ordering for the columns whose displayed text sorts wrong; the rest
# fall back to their own cell text, which is what the reader is comparing anyway
LOCAL = {
    "Season": lambda i: num(i, "index"),
    "Year": lambda i: num(i, "year"),
    "Length": lambda i: num(i, "duration"),
    "#": lambda i: (num(i, "parentIndex"), num(i, "index")),
    "Seasons": lambda i: num(i, "childCount"),
    "Albums": lambda i: num(i, "childCount"),
    "Episodes": lambda i: num(i, "leafCount"),
    "Tracks": lambda i: num(i, "leafCount"),
}
MARKS = {"asc": " ▲", "desc": " ▼"}


def mixed_label(item) -> str:
    """continue watching holds movies and episodes side by side."""
    if item.type == "episode":
        return f"{item.grandparentTitle} – {item.parentIndex}x{item.index:02d} {item.title}"
    return item.title


MIXED = [
    ("Title", 0, mixed_label),
    ("Year", 6, lambda i: getattr(i, "year", "") or ""),
    ("Length", 8, lambda i: duration(getattr(i, "duration", 0))),
    ("", 8, progress_mark),
]


@dataclass
class Level:
    """one rung of the browse stack."""

    title: str
    items: list = field(default_factory=list)
    section: Any = None
    parent: Any = None
    sorted_by: str = ""


class ItemTable(DataTable):
    """a click only moves the cursor; playing is Enter and Enter alone.
    prevent_default keeps the stock handler from selecting on a second click."""

    def _on_click(self, event: events.Click) -> None:
        event.prevent_default()
        meta = event.style.meta
        row, column = meta.get("row", -1), meta.get("column", -1)
        if column < 0:
            return
        if row < 0:
            col = self.ordered_columns[column]
            self.post_message(DataTable.HeaderSelected(self, col.key, column, label=col.label))
        else:
            self.cursor_coordinate = Coordinate(row, column)


class PlexTUI(App):
    CSS = """
    Screen { layers: base overlay; }
    #bar { height: 1; background: $panel; }
    #bar Select { width: 20; margin: 0 1 0 0; }
    #bar #library { width: 24; }
    #bar Select.wide { width: 26; }
    #bar Input { width: 1fr; margin: 0 1 0 0; }
    #count { width: auto; color: $text-muted; padding: 0 1; }
    #crumbs { height: 1; display: none; color: $text-muted; padding: 0 1; }
    DataTable { height: 1fr; }
    """

    BINDINGS = [
        ("escape", "back", "Back"),
        ("slash", "search", "Search"),
        ("ctrl+r", "refresh", "Reload"),
        ("ctrl+d", "toggle_dir", "Sort dir"),
        ("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.url, self.token = load_config()
        self.server: PlexServer | None = None
        self.sections: dict[str, Any] = {}
        self.stack: list[Level] = []
        self.sort_dir = "asc"
        self.sort_keys: set[str] = set()  # what the current library can sort on server-side
        self.year_field = "year"
        self.quiet = False  # suppress reloads while repopulating the bar
        self.playing = False  # mpv now runs alongside the tui, so only one at a time

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        with Horizontal(id="bar"):
            yield Select([], prompt="Library", id="library", compact=True, allow_blank=True)
            yield Select([], prompt="Genre", id="genre", compact=True, allow_blank=True)
            yield Select([], prompt="Year", id="year", compact=True, allow_blank=True)
            yield Select([], prompt="Sort", id="sort", compact=True, allow_blank=True,
                         classes="wide")
            yield Input(placeholder="filter…", id="search", compact=True)
            yield Label("", id="count")
        yield Label("", id="crumbs")
        yield ItemTable(id="items", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        self.register_theme(NOIR)
        self.theme = NOIR.name
        self.query_one("#items", DataTable).focus()
        self.connect()

    # ------------------------------------------------------------ plex calls

    @work(thread=True, exclusive=True, group="connect")
    def connect(self) -> None:
        if not self.url or not self.token:
            self.call_from_thread(self.bail, "Set PLEX_URL and PLEX_TOKEN in ~/.config/clix/config")
            return
        self.call_from_thread(self.status, "connecting…")
        try:
            server = PlexServer(self.url, self.token)
            sections = list(server.library.sections())
        except Exception as exc:
            self.call_from_thread(self.bail, f"{exc}")
            return
        self.server = server
        self.sections = {s.title: s for s in sections}
        options = [("Resume", ANY)] + [(s.title, s.title) for s in sections]
        self.call_from_thread(self.set_libraries, options)

    def set_libraries(self, options: list[tuple[str, str]]) -> None:
        select = self.query_one("#library", Select)
        self.quiet = True
        select.set_options(options)
        self.quiet = False
        select.value = options[0][1]

    @work(thread=True, exclusive=True, group="section")
    def load_section(self, title: str) -> None:
        """populate the filter bar from the server, then load items."""
        if title == ANY:
            self.call_from_thread(self.status, "loading…")
            items = self.continue_watching()
            self.call_from_thread(self.set_filters, [], [], [])
            self.call_from_thread(self.show, Level("Resume", items))
            return

        section = self.sections[title]
        self.call_from_thread(self.status, "loading filters…")
        try:
            fields = {f.filter for f in section.listFilters()}
        except Exception:
            fields = set()
        genres = self.choices(section, "genre") if "genre" in fields else []
        # decade keeps the dropdown short; not every library type offers it
        self.year_field = "decade" if "decade" in fields else "year"
        years = self.choices(section, self.year_field) if self.year_field in fields else []
        try:
            sorts = [(s.title, s.key) for s in section.listSorts()]
        except Exception:
            sorts = [("Title", "titleSort")]
        self.call_from_thread(self.set_filters, genres, years, sorts)
        self.call_from_thread(self.reload_items)

    @staticmethod
    def choices(section, field_name: str) -> list[tuple[str, str]]:
        # the key is what search() wants; decade titles ("1980s") fail its int check
        try:
            return [(c.title, c.key) for c in section.listFilterChoices(field_name)]
        except Exception:
            return []

    def set_filters(self, genres, years, sorts) -> None:
        self.sort_keys = {key for _, key in sorts}
        self.quiet = True
        for wid, options in (("#genre", genres), ("#year", years), ("#sort", sorts)):
            select = self.query_one(wid, Select)
            # sort always has a value; the filters default to no restriction
            prefix = [] if wid == "#sort" else [("any", ANY)]
            select.set_options(prefix + list(options) if options else [])
            select.disabled = not options
            if options:
                select.value = options[0][1] if wid == "#sort" else ANY
        self.quiet = False

    @work(thread=True, exclusive=True, group="items")
    def reload_items(self) -> None:
        title = self.query_one("#library", Select).value
        if title in (Select.BLANK, ANY) or title not in self.sections:
            return
        section = self.sections[title]
        filters = {}
        for widget_id, field_name in (("genre", "genre"), ("year", self.year_field)):
            value = self.query_one(f"#{widget_id}", Select).value
            if value not in (Select.BLANK, ANY):
                filters[field_name] = value
        sort = self.query_one("#sort", Select).value
        kwargs = {} if sort is Select.BLANK else {"sort": f"{sort}:{self.sort_dir}"}

        self.call_from_thread(self.status, "loading…")
        try:
            items = section.search(**filters, **kwargs)
        except Exception as exc:
            self.call_from_thread(self.status, f"error: {exc}")
            return
        self.call_from_thread(self.show, Level(section.title, items, section=section))

    def continue_watching(self) -> list:
        """the hub endpoint returns Hub wrappers; the media is one level down."""
        assert self.server
        try:
            hubs = self.server.fetchItems("/hubs/continueWatching")
            items = [item for hub in hubs for item in getattr(hub, "items", [])]
            if items:
                return items
        except Exception:
            pass
        return self.server.library.onDeck()

    @work(thread=True, exclusive=True, group="items")
    def drill(self, item) -> None:
        self.call_from_thread(self.status, "loading…")
        try:
            children = list(getattr(item, CHILDREN[item.type])())
        except Exception as exc:
            self.call_from_thread(self.status, f"error: {exc}")
            return
        self.call_from_thread(self.show, Level(item.title, children, parent=item), True)

    # -------------------------------------------------------------- rendering

    def show(self, level: Level, push: bool = False) -> None:
        if push:
            self.stack.append(level)
        else:
            self.stack = [level]
        self.query_one("#search", Input).value = ""
        self.render_level()

    @staticmethod
    def layout_for(level: Level) -> list:
        kinds = {item.type for item in level.items}
        return LAYOUTS.get(kinds.pop(), MIXED) if len(kinds) == 1 else MIXED

    def sorted_heading(self) -> str:
        """which column the current level is ordered by, server sort or local."""
        level = self.stack[-1]
        if level.sorted_by:
            return level.sorted_by
        if level.section is None:
            return ""
        key = self.query_one("#sort", Select).value
        layout = self.layout_for(level)
        return next((h for h, _, _ in layout if SORTS.get(h) == key), "")

    def render_level(self) -> None:
        level = self.stack[-1]
        table = self.query_one("#items", DataTable)
        table.clear(columns=True)

        layout = self.layout_for(level)
        current = self.sorted_heading()
        for heading, width, _ in layout:
            label = heading + (MARKS[self.sort_dir] if current and heading == current else "")
            # reserve room for the marker so sorting never shifts the columns
            table.add_column(label, width=max(width, len(heading) + 2) if width else None,
                             key=heading)

        needle = self.query_one("#search", Input).value.lower()
        shown = 0
        for index, item in enumerate(level.items):
            if needle and needle not in item.title.lower():
                continue
            table.add_row(*(str(cell(item)) for _, _, cell in layout), key=str(index))
            shown += 1

        crumbs = self.query_one("#crumbs", Label)
        crumbs.display = len(self.stack) > 1
        crumbs.update(" › ".join(lvl.title for lvl in self.stack))
        total = len(level.items)
        self.status(f"{shown}/{total}" if shown != total else str(total))
        for name in ("#genre", "#year", "#sort"):
            self.query_one(name, Select).disabled = len(self.stack) > 1

    def status(self, text: str) -> None:
        self.query_one("#count", Label).update(text)

    def bail(self, message: str) -> None:
        self.exit(message=message)

    # --------------------------------------------------------------- playback

    @work(thread=True)
    def play(self, item) -> None:
        assert self.server
        try:
            part = item.media[0].parts[0]
        except (AttributeError, IndexError):
            self.call_from_thread(self.status, "no playable part")
            self.playing = False
            return

        url = self.server.url(part.key, includeToken=False)
        watch_dir = tempfile.mkdtemp(prefix="plextui-")
        ipc = str(Path(watch_dir) / "ipc")
        cmd = [
            "mpv", f"--title={item.title}", "--no-terminal", "--no-resume-playback",
            "--save-position-on-quit", f"--watch-later-dir={watch_dir}",
            f"--input-ipc-server={ipc}",
            f"--http-header-fields=X-Plex-Token: {self.token}",
        ]
        offset = (getattr(item, "viewOffset", 0) or 0) // 1000
        if offset:
            cmd.append(f"--start={offset}")
        cmd.append(url)

        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            shutil.rmtree(watch_dir, ignore_errors=True)
            self.call_from_thread(self.status, f"mpv: {exc}")
            self.playing = False
            return
        self.follow(proc, ipc, item)

        position = self.resume_point(watch_dir)
        shutil.rmtree(watch_dir, ignore_errors=True)
        self.report(item, position, proc.returncode)
        if position:
            item.viewOffset = position * 1000
        elif proc.returncode == 0:
            item.viewOffset = 0
            item.viewCount = (getattr(item, "viewCount", 0) or 0) + 1
        self.call_from_thread(self.update_progress, item)
        self.playing = False

    def follow(self, proc, ipc: str, item) -> None:
        """mirror mpv's clock into the progress column until it exits."""
        while proc.poll() is None:
            position = mpv_time_pos(ipc)
            if position is not None:
                item.viewOffset = int(position * 1000)
                self.call_from_thread(self.update_progress, item)
            time.sleep(1)

    def update_progress(self, item) -> None:
        """repaint just the progress cell, if the item is still on screen."""
        if not self.stack:
            return
        level = self.stack[-1]
        layout = self.layout_for(level)
        column = next((n for n, (heading, _, _) in enumerate(layout) if not heading), None)
        if column is None:
            return
        table = self.query_one("#items", DataTable)
        try:
            row = table.get_row_index(str(level.items.index(item)))
        except (ValueError, KeyError):
            return
        table.update_cell_at(Coordinate(row, column), progress_mark(item))

    @staticmethod
    def resume_point(watch_dir: str) -> int | None:
        """mpv writes a watch-later file only when playback is quit early."""
        for path in Path(watch_dir).iterdir():
            if not path.is_file():  # the ipc socket lives here too
                continue
            for line in path.read_text().splitlines():
                if line.startswith("start="):
                    return int(float(line.removeprefix("start=")))
        return None

    def report(self, item, position: int | None, returncode: int) -> None:
        if item.type not in ("movie", "episode"):
            return
        try:
            if position:
                item.updateTimeline(position * 1000, state="stopped", duration=item.duration)
            elif returncode == 0:
                item.markPlayed()
        except Exception as exc:
            self.call_from_thread(self.status, f"progress not saved: {exc}")

    # ----------------------------------------------------------------- events

    @on(Select.Changed, "#library")
    def library_changed(self, event: Select.Changed) -> None:
        if not self.quiet and event.value is not Select.BLANK:
            self.load_section(str(event.value))

    @on(Select.Changed, "#genre")
    @on(Select.Changed, "#year")
    @on(Select.Changed, "#sort")
    def filter_changed(self) -> None:
        if not self.quiet:
            self.reload_items()

    @on(Input.Changed, "#search")
    def search_changed(self) -> None:
        if self.stack:
            self.render_level()

    @on(DataTable.HeaderSelected)
    def header_selected(self, event: DataTable.HeaderSelected) -> None:
        heading = str(event.column_key.value)
        if not self.stack or heading not in SORTS:
            return
        same = heading == self.sorted_heading()
        self.sort_dir = "desc" if same and self.sort_dir == "asc" else "asc"
        self.apply_sort(heading)

    def apply_sort(self, heading: str) -> None:
        """server-side when the library offers the key, in memory otherwise."""
        level = self.stack[-1]
        plex_key = SORTS[heading]
        if level.section is not None and plex_key in self.sort_keys:
            select = self.query_one("#sort", Select)
            if select.value == plex_key:
                self.reload_items()
            else:
                select.value = plex_key  # Changed reloads for us
            return
        level.sorted_by = heading
        level.items.sort(key=self.local_key(level, heading), reverse=self.sort_dir == "desc")
        self.render_level()

    def local_key(self, level: Level, heading: str):
        if heading in LOCAL:
            return LOCAL[heading]
        cell = next(c for h, _, c in self.layout_for(level) if h == heading)
        return lambda item: str(cell(item)).lower()

    @on(DataTable.RowSelected)
    def row_selected(self, event: DataTable.RowSelected) -> None:
        item = self.stack[-1].items[int(event.row_key.value)]
        if item.type in PLAYABLE:
            if self.playing:
                self.status("already playing")
                return
            self.playing = True
            self.play(item)
        elif item.type in CHILDREN:
            self.drill(item)

    # ---------------------------------------------------------------- actions

    def action_back(self) -> None:
        if len(self.stack) > 1:
            self.stack.pop()
            self.render_level()
        else:
            self.exit()

    def action_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_refresh(self) -> None:
        if len(self.stack) > 1:
            self.drill(self.stack[-1].parent)
        else:
            self.reload_items()

    def action_toggle_dir(self) -> None:
        if not self.stack:
            return
        self.sort_dir = "desc" if self.sort_dir == "asc" else "asc"
        if heading := self.sorted_heading():
            self.apply_sort(heading)
        elif self.stack[-1].section is not None:
            self.reload_items()


if __name__ == "__main__":
    PlexTUI().run()
