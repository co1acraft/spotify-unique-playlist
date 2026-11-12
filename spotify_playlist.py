#!/usr/bin/env python3
"""Build a Spotify playlist containing every unique song from a streaming history export."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import http.server
import io
import json
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

API_BASE = "https://api.spotify.com/v1"
ACCOUNTS_BASE = "https://accounts.spotify.com"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = ("playlist-modify-public", "playlist-modify-private")
BATCH_SIZE = 100
PLAYLIST_MAX = 10_000
SORT_CHOICES = ("plays", "recent", "first", "name", "artist")
TRACK_URI_PREFIX = "spotify:track:"
EPISODE_URI_PREFIX = "spotify:episode:"


class SpotifyError(RuntimeError):
    pass


@dataclass
class Play:
    kind: str
    uri: str | None
    name: str
    artist: str
    album: str
    ms_played: int
    played_at: datetime | None
    skipped: bool


@dataclass
class Record:
    kind: str
    uri: str | None
    name: str
    artist: str
    album: str
    plays: int = 0
    ms_played: int = 0
    skipped: int = 0
    first_played: datetime | None = None
    last_played: datetime | None = None

    def merge(self, other: "Record") -> None:
        self.plays += other.plays
        self.ms_played += other.ms_played
        self.skipped += other.skipped
        if other.first_played is not None and (
            self.first_played is None or other.first_played < self.first_played
        ):
            self.first_played = other.first_played
        if other.last_played is not None and (
            self.last_played is None or other.last_played > self.last_played
        ):
            self.last_played = other.last_played
        if not self.uri and other.uri:
            self.uri = other.uri
        if not self.artist and other.artist:
            self.artist = other.artist
        if not self.album and other.album:
            self.album = other.album
        if not self.name and other.name:
            self.name = other.name


def normalize(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", (value or "").strip()).casefold()
    return re.sub(r"\s+", " ", text)


def fallback_key(kind: str, artist: str, name: str) -> tuple[str, str, str]:
    return (kind, normalize(artist), normalize(name))


class Library:
    def __init__(self) -> None:
        self.records: list[Record] = []
        self._by_uri: dict[str, Record] = {}
        self._by_fallback: dict[tuple[str, str, str], Record] = {}

    def add(self, play: Play) -> None:
        key = fallback_key(play.kind, play.artist, play.name)
        record: Record | None = None

        if play.uri:
            record = self._by_uri.get(play.uri)
            if record is None:
                candidate = self._by_fallback.get(key)
                if candidate is not None and candidate.uri is None:
                    record = candidate
                    record.uri = play.uri
                    self._by_uri[play.uri] = record
                else:
                    record = Record(
                        kind=play.kind,
                        uri=play.uri,
                        name=play.name,
                        artist=play.artist,
                        album=play.album,
                    )
                    self.records.append(record)
                    self._by_uri[play.uri] = record
        else:
            record = self._by_fallback.get(key)
            if record is None:
                record = Record(
                    kind=play.kind,
                    uri=None,
                    name=play.name,
                    artist=play.artist,
                    album=play.album,
                )
                self.records.append(record)

        self._by_fallback.setdefault(key, record)
        record.plays += 1
        record.ms_played += max(play.ms_played, 0)
        if play.skipped:
            record.skipped += 1
        if play.played_at is not None:
            if record.first_played is None or play.played_at < record.first_played:
                record.first_played = play.played_at
            if record.last_played is None or play.played_at > record.last_played:
                record.last_played = play.played_at
        if not record.name and play.name:
            record.name = play.name
        if not record.artist and play.artist:
            record.artist = play.artist
        if not record.album and play.album:
            record.album = play.album

    def dedupe_by_uri(self) -> None:
        merged: dict[str, Record] = {}
        output: list[Record] = []
        for record in self.records:
            if record.uri and record.uri in merged:
                merged[record.uri].merge(record)
            else:
                output.append(record)
                if record.uri:
                    merged[record.uri] = record
        self.records = output
        self._by_uri = merged
        self._by_fallback = {}
        for record in output:
            self._by_fallback.setdefault(
                fallback_key(record.kind, record.artist, record.name), record
            )


def parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _first(entry: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = entry.get(key)
        if value not in (None, ""):
            return value
    return None


def parse_entry(entry: Any) -> Play | None:
    if not isinstance(entry, dict):
        return None

    track_uri = _first(entry, "spotify_track_uri")
    episode_uri = _first(entry, "spotify_episode_uri")
    episode_name = _first(entry, "episode_name")
    show_name = _first(entry, "episode_show_name")

    track_name = _first(entry, "master_metadata_track_name", "trackName")
    artist = _first(entry, "master_metadata_album_artist_name", "artistName")
    album = _first(entry, "master_metadata_album_album_name", "albumName")

    if episode_uri or (episode_name and not track_name):
        kind = "episode"
        name = episode_name or track_name
        uri = episode_uri
        artist = show_name or artist
        album = show_name or album
    else:
        kind = "track"
        name = track_name
        uri = track_uri

    if isinstance(uri, str):
        prefix = EPISODE_URI_PREFIX if kind == "episode" else TRACK_URI_PREFIX
        if not uri.startswith(prefix):
            uri = None
    else:
        uri = None

    if not name and not uri:
        return None

    ms_value = _first(entry, "ms_played", "msPlayed", "ms_played_forward")
    try:
        ms_played = int(ms_value) if ms_value is not None else 0
    except (TypeError, ValueError):
        ms_played = 0
    if ms_played < 0:
        ms_played = 0

    return Play(
        kind=kind,
        uri=uri,
        name=str(name or ""),
        artist=str(artist or ""),
        album=str(album or ""),
        ms_played=ms_played,
        played_at=parse_timestamp(_first(entry, "ts", "endTime", "end_time")),
        skipped=bool(entry.get("skipped")),
    )


def _extract_entries(document: Any) -> list[Any]:
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        for key in ("items", "data", "history", "streamingHistory"):
            value = document.get(key)
            if isinstance(value, list):
                return value
    return []


def iter_json_documents(inputs: Iterable[str]) -> Iterator[tuple[str, Any]]:
    seen: set[str] = set()
    for raw in inputs:
        path = Path(raw).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"No such file or directory: {raw}")
        if path.is_dir():
            files = sorted(
                item
                for item in path.rglob("*")
                if item.is_file() and item.suffix.lower() == ".json"
            )
            for file_path in files:
                key = str(file_path.resolve())
                if key in seen:
                    continue
                seen.add(key)
                with file_path.open("r", encoding="utf-8-sig") as handle:
                    yield str(file_path), json.load(handle)
        elif path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                for name in archive.namelist():
                    if not name.lower().endswith(".json"):
                        continue
                    if name.startswith("__MACOSX") or name.endswith("/"):
                        continue
                    key = f"{path.resolve()}::{name}"
                    if key in seen:
                        continue
                    seen.add(key)
                    with archive.open(name) as handle:
                        yield key, json.loads(handle.read().decode("utf-8-sig"))
        else:
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            with path.open("r", encoding="utf-8-sig") as handle:
                yield str(path), json.load(handle)


@dataclass
class ParseStats:
    files: int = 0
    entries: int = 0
    plays_used: int = 0
    invalid: int = 0
    short_skipped: int = 0
    episodes_skipped: int = 0
    earliest: datetime | None = None
    latest: datetime | None = None


def build_library(
    inputs: Iterable[str],
    min_ms_played: int = 0,
    include_episodes: bool = False,
) -> tuple[Library, ParseStats]:
    library = Library()
    stats = ParseStats()

    for _label, document in iter_json_documents(inputs):
        entries = _extract_entries(document)
        if not entries:
            continue
        stats.files += 1
        for entry in entries:
            stats.entries += 1
            play = parse_entry(entry)
            if play is None:
                stats.invalid += 1
                continue
            if play.kind == "episode" and not include_episodes:
                stats.episodes_skipped += 1
                continue
            if play.ms_played < min_ms_played:
                stats.short_skipped += 1
                continue
            library.add(play)
            stats.plays_used += 1
            if play.played_at is not None:
                if stats.earliest is None or play.played_at < stats.earliest:
                    stats.earliest = play.played_at
                if stats.latest is None or play.played_at > stats.latest:
                    stats.latest = play.played_at

    return library, stats


def _epoch(value: datetime | None) -> float:
    return value.timestamp() if value is not None else 0.0


def sort_records(records: Iterable[Record], sort: str) -> list[Record]:
    records = list(records)
    if sort == "plays":
        return sorted(
            records,
            key=lambda r: (-r.plays, -r.ms_played, normalize(r.artist), normalize(r.name)),
        )
    if sort == "recent":
        return sorted(records, key=lambda r: (-_epoch(r.last_played), -r.plays))
    if sort == "first":
        return sorted(
            records,
            key=lambda r: (
                _epoch(r.first_played) if r.first_played is not None else float("inf"),
                normalize(r.name),
            ),
        )
    if sort == "name":
        return sorted(records, key=lambda r: (normalize(r.name), normalize(r.artist)))
    if sort == "artist":
        return sorted(records, key=lambda r: (normalize(r.artist), normalize(r.name)))
    raise ValueError(f"Unknown sort order: {sort}")


def format_date(value: datetime | None) -> str:
    return value.date().isoformat() if value is not None else ""


def record_to_dict(record: Record) -> dict[str, Any]:
    return {
        "uri": record.uri,
        "kind": record.kind,
        "name": record.name,
        "artist": record.artist,
        "album": record.album,
        "plays": record.plays,
        "ms_played": record.ms_played,
        "minutes": round(record.ms_played / 60_000, 2),
        "skipped": record.skipped,
        "first_played": record.first_played.isoformat() if record.first_played else None,
        "last_played": record.last_played.isoformat() if record.last_played else None,
    }


def records_to_json(records: Iterable[Record]) -> str:
    return json.dumps([record_to_dict(record) for record in records], indent=2, ensure_ascii=False)


def records_to_csv(records: Iterable[Record]) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "uri",
            "kind",
            "name",
            "artist",
            "album",
            "plays",
            "ms_played",
            "minutes",
            "skipped",
            "first_played",
            "last_played",
        ]
    )
    for record in records:
        writer.writerow(
            [
                record.uri or "",
                record.kind,
                record.name,
                record.artist,
                record.album,
                record.plays,
                record.ms_played,
                f"{record.ms_played / 60_000:.2f}",
                record.skipped,
                format_date(record.first_played),
                format_date(record.last_played),
            ]
        )
    return output.getvalue()


def print_summary(library: Library, stats: ParseStats, stream: Any = sys.stderr) -> None:
    tracks = [r for r in library.records if r.kind == "track"]
    episodes = [r for r in library.records if r.kind == "episode"]
    without_uri = sum(1 for r in library.records if not r.uri)

    print(
        f"Parsed {stats.plays_used:,} plays from {stats.files:,} file(s).",
        file=stream,
    )
    line = f"Unique songs: {len(tracks):,}"
    if episodes:
        line += f" (+{len(episodes):,} episode(s))"
    if stats.episodes_skipped:
        line += f"; skipped {stats.episodes_skipped:,} podcast episode play(s)"
    print(line + ".", file=stream)
    if stats.short_skipped:
        print(f"Skipped {stats.short_skipped:,} play(s) below the minimum duration.", file=stream)
    if stats.invalid:
        print(f"Ignored {stats.invalid:,} entry/entries that were not playable tracks.", file=stream)
    if stats.earliest and stats.latest:
        print(
            f"Listening range: {format_date(stats.earliest)} to {format_date(stats.latest)}.",
            file=stream,
        )
    if without_uri:
        print(
            f"{without_uri:,} item(s) have no Spotify URI and can be resolved by search "
            "when creating the playlist.",
            file=stream,
        )


def default_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "spotify-unique-playlist"


def default_token_path() -> Path:
    return default_config_dir() / "token.json"


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(temporary, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def http_call(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 30.0,
) -> tuple[int, bytes, dict[str, str]]:
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def _error_detail(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")[:500]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        if error:
            return str(error)
        if payload.get("error_description"):
            return str(payload["error_description"])
    return str(payload)[:500]


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def exchange_code(
    client_id: str,
    code: str,
    redirect_uri: str,
    verifier: str,
) -> dict[str, Any]:
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
    ).encode("ascii")
    try:
        status, raw, _headers = http_call(
            f"{ACCOUNTS_BASE}/api/token",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=payload,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SpotifyError(f"Token exchange failed: {error}") from error
    if status != 200:
        raise SpotifyError(f"Token exchange failed ({status}): {_error_detail(raw)}")
    token = json.loads(raw.decode("utf-8"))
    token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60
    return token


def refresh_token(client_id: str, refresh: str) -> dict[str, Any]:
    payload = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        }
    ).encode("ascii")
    try:
        status, raw, _headers = http_call(
            f"{ACCOUNTS_BASE}/api/token",
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=payload,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SpotifyError(f"Token refresh failed: {error}") from error
    if status != 200:
        raise SpotifyError(f"Token refresh failed ({status}): {_error_detail(raw)}")
    token = json.loads(raw.decode("utf-8"))
    token["expires_at"] = time.time() + int(token.get("expires_in", 3600)) - 60
    if not token.get("refresh_token"):
        token["refresh_token"] = refresh
    return token


class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        expected = getattr(self.server, "callback_path", "/callback")
        if parsed.path != expected:
            self.send_error(404)
            return
        self.server.auth_result = urllib.parse.parse_qs(parsed.query)
        body = (
            "<!doctype html><html><body style='font-family:sans-serif;padding:2rem'>"
            "<h2>Spotify authorization received</h2>"
            "<p>You can close this tab and return to the terminal.</p>"
            "</body></html>"
        )
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
        self.server.done.set()

    def log_message(self, format: str, *args: Any) -> None:
        return


def _manual_redirect(redirect_uri: str) -> dict[str, list[str]]:
    print(
        "Paste the full URL you were redirected to after approving access.",
        file=sys.stderr,
    )
    pasted = input("Redirect URL: ").strip()
    if not pasted:
        raise SpotifyError("No redirect URL supplied.")
    parsed = urllib.parse.urlparse(pasted)
    if not parsed.query and redirect_uri.rstrip("/") not in pasted:
        parsed = urllib.parse.urlparse(redirect_uri + "?" + pasted)
    return urllib.parse.parse_qs(parsed.query)


def authorize(client_id: str, redirect_uri: str, no_browser: bool) -> dict[str, Any]:
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(SCOPES),
            "state": state,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
        }
    )
    auth_url = f"{ACCOUNTS_BASE}/authorize?{query}"

    parsed_redirect = urllib.parse.urlparse(redirect_uri)
    host = parsed_redirect.hostname or ""
    port = parsed_redirect.port or (443 if parsed_redirect.scheme == "https" else 80)
    callback_path = parsed_redirect.path or "/callback"
    loopback = host in ("127.0.0.1", "localhost") and not no_browser
    bind_host = "127.0.0.1"

    result: dict[str, list[str]] | None = None
    if loopback:
        try:
            server = http.server.HTTPServer((bind_host, port), OAuthCallbackHandler)
        except OSError as error:
            print(
                f"Could not listen on {bind_host}:{port} ({error}); falling back to manual entry.",
                file=sys.stderr,
            )
        else:
            server.callback_path = callback_path
            server.auth_result = None
            server.done = threading.Event()
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            print(f"Open this URL to authorize:\n{auth_url}\n", file=sys.stderr)
            webbrowser.open(auth_url)
            if not server.done.wait(timeout=300):
                server.shutdown()
                raise SpotifyError("Timed out waiting for Spotify authorization.")
            result = server.auth_result
            server.shutdown()

    if result is None:
        print(f"Open this URL to authorize:\n{auth_url}\n", file=sys.stderr)
        if not no_browser:
            webbrowser.open(auth_url)
        result = _manual_redirect(redirect_uri)

    if result is None:
        raise SpotifyError("Spotify authorization was not completed.")
    if result.get("error"):
        raise SpotifyError(f"Spotify authorization failed: {result['error'][0]}")
    if result.get("state", [""])[0] != state:
        raise SpotifyError("Authorization state mismatch; please try again.")
    if not result.get("code"):
        raise SpotifyError("Spotify did not return an authorization code.")

    return exchange_code(client_id, result["code"][0], redirect_uri, verifier)


class SpotifyClient:
    def __init__(
        self,
        client_id: str,
        redirect_uri: str,
        token_cache: Path,
        no_browser: bool = False,
    ) -> None:
        self.client_id = client_id
        self.redirect_uri = redirect_uri
        self.token_cache = token_cache
        self.no_browser = no_browser
        self.token: dict[str, Any] = {}
        self._load_token()

    def _load_token(self) -> None:
        cached = load_json_file(self.token_cache)
        if cached.get("client_id") and cached.get("client_id") != self.client_id:
            return
        self.token = cached

    def _save_token(self) -> None:
        payload = dict(self.token)
        payload["client_id"] = self.client_id
        save_json_file(self.token_cache, payload)

    def _authenticate(self) -> None:
        token = authorize(self.client_id, self.redirect_uri, self.no_browser)
        token["client_id"] = self.client_id
        token["redirect_uri"] = self.redirect_uri
        self.token = token
        self._save_token()

    def _refresh(self) -> bool:
        refresh = self.token.get("refresh_token")
        if not refresh:
            return False
        try:
            token = refresh_token(self.client_id, refresh)
        except SpotifyError:
            return False
        token["client_id"] = self.client_id
        token["redirect_uri"] = self.redirect_uri
        self.token = token
        self._save_token()
        return True

    def access_token(self) -> str:
        if self.token.get("access_token") and float(self.token.get("expires_at", 0)) > time.time():
            return str(self.token["access_token"])
        if self._refresh():
            return str(self.token["access_token"])
        self._authenticate()
        return str(self.token["access_token"])

    def force_refresh(self) -> None:
        if not self._refresh():
            self._authenticate()

    def api(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        retries: int = 6,
    ) -> Any:
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)

        last_error = "request failed"
        for attempt in range(retries):
            headers = {"Authorization": f"Bearer {self.access_token()}"}
            body = None
            if payload is not None:
                body = json.dumps(payload).encode("utf-8")
                headers["Content-Type"] = "application/json"

            try:
                status, raw, response_headers = http_call(
                    url, method=method, headers=headers, body=body
                )
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last_error = str(error)
                wait = min(2**attempt, 30)
                time.sleep(wait)
                continue

            if status == 429:
                retry_after = response_headers.get("Retry-After", "1")
                try:
                    wait = float(retry_after)
                except ValueError:
                    wait = 1.0
                print(f"\nRate limited by Spotify; waiting {wait:.0f}s...", file=sys.stderr)
                time.sleep(wait + 0.5)
                continue
            if status == 401 and attempt < 2:
                self.force_refresh()
                continue
            if 500 <= status < 600:
                wait = min(2**attempt, 30)
                time.sleep(wait)
                last_error = _error_detail(raw)
                continue
            if 200 <= status < 300:
                return json.loads(raw.decode("utf-8")) if raw else None

            raise SpotifyError(
                f"Spotify API {method} {path} failed ({status}): {_error_detail(raw)}"
            )

        raise SpotifyError(f"Spotify API {method} {path} failed: {last_error}")

    def create_playlist(self, name: str, public: bool, description: str) -> dict[str, Any]:
        return self.api(
            "POST",
            "/me/playlists",
            payload={"name": name, "public": public, "description": description},
        )

    def add_items(self, playlist_id: str, uris: list[str]) -> list[str]:
        failed: list[str] = []
        for start in range(0, len(uris), BATCH_SIZE):
            batch = uris[start : start + BATCH_SIZE]
            try:
                self.api("POST", f"/playlists/{playlist_id}/items", payload={"uris": batch})
            except SpotifyError as error:
                message = str(error)
                if "(400)" not in message and "(404)" not in message:
                    raise
                for uri in batch:
                    try:
                        self.api(
                            "POST",
                            f"/playlists/{playlist_id}/items",
                            payload={"uris": [uri]},
                        )
                    except SpotifyError:
                        failed.append(uri)
        return failed

    def search(self, query: str, kind: str, limit: int = 10) -> list[dict[str, Any]]:
        data = self.api(
            "GET",
            "/search",
            params={"q": query, "type": kind, "limit": min(limit, 10)},
        )
        if not isinstance(data, dict):
            return []
        section = data.get(kind + "s") or {}
        items = section.get("items") if isinstance(section, dict) else None
        return items if isinstance(items, list) else []


def split_genres(values: Iterable[str] | None) -> list[str]:
    if not values:
        return []
    genres: list[str] = []
    for value in values:
        for part in value.split(","):
            cleaned = part.strip()
            if cleaned:
                genres.append(cleaned)
    return genres


def normalize_genre(value: str | None) -> str:
    text = normalize(value).replace("-", " ")
    text = text.replace("'", "").replace("\u2019", "")
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def genre_matches(record_genres: Iterable[str], wanted: Iterable[str]) -> bool:
    record_values = [normalize_genre(genre) for genre in record_genres]
    wanted_values = [normalize_genre(genre) for genre in wanted]
    for target in wanted_values:
        if not target:
            continue
        for value in record_values:
            if not value:
                continue
            if target == value or target in value or value in target:
                return True
    return False


def filter_min_streams(records: list[Record], min_streams: int) -> tuple[list[Record], int]:
    if min_streams <= 1:
        return records, 0
    kept = [record for record in records if record.plays >= min_streams]
    return kept, len(records) - len(kept)


@dataclass
class GenreFilterStats:
    kept: int = 0
    excluded_allow: int = 0
    excluded_exclude: int = 0
    unknown: int = 0
    tracks_fetched: int = 0
    artists_fetched: int = 0


def _track_id(uri: str | None) -> str | None:
    if isinstance(uri, str) and uri.startswith(TRACK_URI_PREFIX):
        return uri[len(TRACK_URI_PREFIX) :]
    return None


def apply_genre_filters(
    client: SpotifyClient,
    records: list[Record],
    include_genres: list[str],
    exclude_genres: list[str],
    cache_path: Path,
    delay: float,
) -> tuple[list[Record], GenreFilterStats]:
    cache = load_json_file(cache_path)
    track_cache = cache.get("tracks")
    artist_cache = cache.get("artists")
    if not isinstance(track_cache, dict):
        track_cache = {}
    if not isinstance(artist_cache, dict):
        artist_cache = {}
    track_cache = dict(track_cache)
    artist_cache = dict(artist_cache)

    stats = GenreFilterStats()
    kept: list[Record] = []
    pending_save = 0
    total = len(records)

    if total:
        print(
            f"Checking genres for {total:,} track(s) via Spotify artist data...",
            file=sys.stderr,
        )

    for index, record in enumerate(records, start=1):
        genres: list[str] = []

        if record.kind == "track" and record.uri:
            track_id = _track_id(record.uri)
            artist_ids = track_cache.get(track_id) if track_id else None
            if not isinstance(artist_ids, list):
                artist_ids = []
                if track_id:
                    try:
                        data = client.api(
                            "GET", f"/tracks/{urllib.parse.quote(track_id, safe='')}"
                        )
                    except SpotifyError as error:
                        print(
                            f"\nCould not look up track {track_id}: {error}",
                            file=sys.stderr,
                        )
                    else:
                        raw_artists = data.get("artists") if isinstance(data, dict) else []
                        for artist in raw_artists or []:
                            if isinstance(artist, dict) and artist.get("id"):
                                artist_ids.append(str(artist["id"]))
                    track_cache[track_id] = artist_ids
                    stats.tracks_fetched += 1
                    pending_save += 1
                    if delay > 0:
                        time.sleep(delay)

            for artist_id in artist_ids:
                artist_genres = artist_cache.get(artist_id)
                if not isinstance(artist_genres, list):
                    artist_genres = []
                    try:
                        data = client.api(
                            "GET", f"/artists/{urllib.parse.quote(str(artist_id), safe='')}"
                        )
                    except SpotifyError as error:
                        print(
                            f"\nCould not look up artist {artist_id}: {error}",
                            file=sys.stderr,
                        )
                    else:
                        raw_genres = data.get("genres") if isinstance(data, dict) else []
                        artist_genres = [str(genre) for genre in raw_genres or []]
                    artist_cache[artist_id] = artist_genres
                    stats.artists_fetched += 1
                    pending_save += 1
                    if delay > 0:
                        time.sleep(delay)
                genres.extend(str(genre) for genre in artist_genres)

        record_genres = list(dict.fromkeys(genres))
        allowed = not include_genres or genre_matches(record_genres, include_genres)
        excluded = bool(exclude_genres) and genre_matches(record_genres, exclude_genres)

        if include_genres and not allowed:
            stats.excluded_allow += 1
            if not record_genres:
                stats.unknown += 1
        elif exclude_genres and excluded:
            stats.excluded_exclude += 1
        else:
            kept.append(record)
            stats.kept += 1

        if index % 25 == 0 or index == total:
            print(
                f"\rChecked genres: {index:,}/{total:,} "
                f"(kept {stats.kept:,}, excluded {stats.excluded_allow + stats.excluded_exclude:,})",
                end="",
                file=sys.stderr,
                flush=True,
            )

        if pending_save >= 50:
            save_json_file(
                cache_path,
                {"version": 1, "tracks": track_cache, "artists": artist_cache},
            )
            pending_save = 0

    if total:
        print(file=sys.stderr)
    if track_cache or artist_cache:
        save_json_file(
            cache_path,
            {"version": 1, "tracks": track_cache, "artists": artist_cache},
        )

    return kept, stats


def choose_track_result(
    results: list[dict[str, Any]], name: str, artist: str
) -> str | None:
    target_name = normalize(name)
    target_artist = normalize(artist)
    best: tuple[int, str] | None = None
    for item in results:
        uri = item.get("uri")
        item_name = normalize(item.get("name"))
        if not isinstance(uri, str) or not uri.startswith(TRACK_URI_PREFIX):
            continue
        artists = item.get("artists") or []
        artist_names = [normalize(a.get("name")) for a in artists if isinstance(a, dict)]
        if target_name == item_name:
            name_score = 2
        elif target_name and (target_name in item_name or item_name in target_name):
            name_score = 1
        else:
            continue
        if target_artist and target_artist in artist_names:
            artist_score = 2
        elif target_artist and any(target_artist in a or a in target_artist for a in artist_names):
            artist_score = 1
        else:
            artist_score = 0
        score = name_score + artist_score
        if artist_score == 0:
            continue
        if best is None or score > best[0]:
            best = (score, uri)
    return best[1] if best else None


def resolve_missing_uris(
    client: SpotifyClient,
    records: list[Record],
    cache_path: Path,
    delay: float,
) -> tuple[list[Record], list[Record], int]:
    cache = load_json_file(cache_path)
    unresolved: list[Record] = []
    resolved_count = 0
    pending = [r for r in records if not r.uri and r.kind == "track"]

    if pending:
        print(
            f"Resolving {len(pending):,} track(s) without Spotify URIs via search...",
            file=sys.stderr,
        )

    for index, record in enumerate(pending, start=1):
        key = f"{normalize(record.artist)}|{normalize(record.name)}"
        cached_uri = cache.get(key)
        if isinstance(cached_uri, str) and cached_uri.startswith(TRACK_URI_PREFIX):
            record.uri = cached_uri
            resolved_count += 1
            continue
        query = " ".join(part for part in (record.name, record.artist) if part).strip()
        if not query:
            continue
        try:
            results = client.search(query, "track", limit=10)
        except SpotifyError as error:
            print(f"\nSearch failed for {record.name!r}: {error}", file=sys.stderr)
            unresolved.append(record)
            continue
        uri = choose_track_result(results, record.name, record.artist)
        if uri is None:
            unresolved.append(record)
        else:
            record.uri = uri
            cache[key] = uri
            resolved_count += 1
        if index % 25 == 0 or index == len(pending):
            print(
                f"\rResolved {resolved_count:,}/{index:,} searched "
                f"({len(unresolved):,} unmatched)",
                end="",
                file=sys.stderr,
                flush=True,
            )
        if delay > 0:
            time.sleep(delay)
    if pending:
        print(file=sys.stderr)
    if cache:
        save_json_file(cache_path, cache)

    for record in records:
        if not record.uri:
            unresolved.append(record)
    seen: set[int] = set()
    unique_unresolved: list[Record] = []
    for record in unresolved:
        if id(record) not in seen:
            seen.add(id(record))
            unique_unresolved.append(record)
    return records, unique_unresolved, resolved_count


def chunks(items: list[Record], size: int) -> Iterator[list[Record]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def add_with_progress(client: SpotifyClient, playlist_id: str, uris: list[str]) -> list[str]:
    failed: list[str] = []
    total = len(uris)
    done = 0
    for start in range(0, total, BATCH_SIZE):
        batch = uris[start : start + BATCH_SIZE]
        try:
            failed.extend(client.add_items(playlist_id, batch))
        except SpotifyError as error:
            print(f"\nCould not add batch starting at {start}: {error}", file=sys.stderr)
            failed.extend(batch)
        done += len(batch)
        print(
            f"\rAdding items: {done:,}/{total:,}",
            end="",
            file=sys.stderr,
            flush=True,
        )
    if total:
        print(file=sys.stderr)
    return failed


def playlist_url(playlist: dict[str, Any]) -> str:
    external = playlist.get("external_urls") or {}
    url = external.get("spotify")
    if isinstance(url, str) and url:
        return url
    return f"https://open.spotify.com/playlist/{playlist.get('id', '')}"


def cmd_parse(args: argparse.Namespace) -> int:
    library, stats = build_library(
        args.inputs,
        min_ms_played=args.min_ms_played,
        include_episodes=args.include_episodes,
    )
    records = sort_records(library.records, args.sort)
    records, below_threshold = filter_min_streams(records, args.min_streams)
    if args.limit is not None:
        records = records[: args.limit]
    print_summary(library, stats)
    if below_threshold:
        print(
            f"Skipped {below_threshold:,} item(s) below --min-streams {args.min_streams}.",
            file=sys.stderr,
        )

    content = records_to_csv(records) if args.format == "csv" else records_to_json(records)
    if args.output and args.output != "-":
        output_path = Path(args.output).expanduser()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")
        print(f"Wrote {len(records):,} unique item(s) to {output_path}.", file=sys.stderr)
    else:
        sys.stdout.write(content)
        if not content.endswith("\n"):
            sys.stdout.write("\n")
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    client_id = args.client_id or os.environ.get("SPOTIFY_CLIENT_ID")
    if not client_id:
        raise SpotifyError("Set SPOTIFY_CLIENT_ID or pass --client-id.")
    client = SpotifyClient(
        client_id=client_id,
        redirect_uri=args.redirect_uri,
        token_cache=Path(args.token_cache).expanduser(),
        no_browser=args.no_browser,
    )
    client.access_token()
    print(f"Authorized. Token cached at {Path(args.token_cache).expanduser()}.")
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    library, stats = build_library(
        args.inputs,
        min_ms_played=args.min_ms_played,
        include_episodes=args.include_episodes,
    )
    records = sort_records(library.records, args.sort)
    if args.limit is not None:
        records = records[: args.limit]
    print_summary(library, stats)

    if not records:
        raise SpotifyError("No unique tracks or episodes were found in the supplied files.")

    include_genres = split_genres(args.genres)
    exclude_genres = split_genres(args.exclude_genres)
    genre_requested = bool(include_genres or exclude_genres)
    online_needed = not args.dry_run or genre_requested

    token_cache = Path(args.token_cache).expanduser()
    client: SpotifyClient | None = None
    if online_needed:
        client_id = args.client_id or os.environ.get("SPOTIFY_CLIENT_ID")
        if not client_id:
            raise SpotifyError("Set SPOTIFY_CLIENT_ID or pass --client-id.")
        client = SpotifyClient(
            client_id=client_id,
            redirect_uri=args.redirect_uri,
            token_cache=token_cache,
            no_browser=args.no_browser,
        )

    unresolved = [r for r in records if not r.uri]
    if client is not None:
        missing_before = [r for r in records if not r.uri]
        if missing_before and not args.no_resolve:
            resolve_cache = token_cache.parent / "resolved_tracks.json"
            records, unresolved, resolved_count = resolve_missing_uris(
                client, records, resolve_cache, args.search_delay
            )
            library.dedupe_by_uri()
            records = sort_records(library.records, args.sort)
            if args.limit is not None:
                records = records[: args.limit]
            print(
                f"Resolved {resolved_count:,} track(s) by search; "
                f"{len(unresolved):,} could not be matched.",
                file=sys.stderr,
            )
        else:
            unresolved = [r for r in records if not r.uri]

    records, below_threshold = filter_min_streams(records, args.min_streams)
    if below_threshold:
        print(
            f"Skipped {below_threshold:,} item(s) below --min-streams {args.min_streams}.",
            file=sys.stderr,
        )
    if not records:
        raise SpotifyError(f"No items met --min-streams {args.min_streams}.")

    if genre_requested:
        if client is None:
            raise SpotifyError("Genre filtering requires Spotify authorization.")
        genre_cache = (
            Path(args.genre_cache).expanduser()
            if args.genre_cache
            else token_cache.parent / "genres.json"
        )
        records, genre_stats = apply_genre_filters(
            client,
            records,
            include_genres,
            exclude_genres,
            genre_cache,
            args.genre_delay,
        )
        print(
            f"Genre filter: kept {genre_stats.kept:,} of "
            f"{genre_stats.kept + genre_stats.excluded_allow + genre_stats.excluded_exclude:,} "
            f"track(s); excluded {genre_stats.excluded_allow:,} not matching selected genres "
            f"and {genre_stats.excluded_exclude:,} matching excluded genres.",
            file=sys.stderr,
        )
        if genre_stats.unknown:
            print(
                f"{genre_stats.unknown:,} track(s) had no genre data and were excluded by "
                "the genre allowlist.",
                file=sys.stderr,
            )
        if not records:
            raise SpotifyError("No tracks matched the genre filters.")
        kept_ids = {id(record) for record in records}
        unresolved = [record for record in unresolved if id(record) in kept_ids]

    if args.dry_run:
        playable = [r for r in records if r.uri]
        missing = [r for r in records if not r.uri]
        total_parts = 1 if args.append_to else max(1, -(-len(records) // args.max_tracks))
        print(
            f"Dry run: would add {len(records):,} unique item(s) "
            f"({len(playable):,} with URIs, {len(missing):,} needing search) "
            f"across {total_parts} playlist(s).",
            file=sys.stderr,
        )
        for record in records[:10]:
            print(
                f"  {record.name} — {record.artist} "
                f"({record.plays} play(s), {record.uri or 'no URI'})",
                file=sys.stderr,
            )
        if not genre_requested:
            print(
                "Genre lookups and URI resolution were skipped for this offline dry run.",
                file=sys.stderr,
            )
        return 0

    playable = [r for r in records if r.uri]
    if not playable:
        raise SpotifyError(
            "No Spotify URIs are available. Use a newer export, or allow search resolution."
        )
    if unresolved:
        preview = ", ".join(f"{r.name} — {r.artist}" for r in unresolved[:5])
        print(
            f"Warning: skipping {len(unresolved):,} unmatched item(s): {preview}"
            + (" ..." if len(unresolved) > 5 else ""),
            file=sys.stderr,
        )

    if args.append_to:
        playlist_id = args.append_to
        print(f"Adding {len(playable):,} item(s) to playlist {playlist_id}...", file=sys.stderr)
        failed = add_with_progress(client, playlist_id, [r.uri for r in playable if r.uri])
        print(f"Playlist: https://open.spotify.com/playlist/{playlist_id}")
        if failed:
            print(f"{len(failed)} item(s) could not be added.", file=sys.stderr)
            return 1
        return 0

    base_name = args.name or "Every Song I've Played"
    groups = list(chunks(playable, args.max_tracks))
    total_parts = len(groups)
    failures = 0
    urls: list[str] = []

    for part_index, group in enumerate(groups, start=1):
        name = base_name if total_parts == 1 else f"{base_name} ({part_index}/{total_parts})"
        description = args.description
        if total_parts > 1:
            description = f"{description} Part {part_index} of {total_parts}."
        print(
            f"Creating playlist {name!r} ({'public' if args.public else 'private'})...",
            file=sys.stderr,
        )
        playlist = client.create_playlist(name, args.public, description)
        playlist_id = playlist.get("id")
        if not playlist_id:
            raise SpotifyError("Spotify did not return a playlist ID.")
        uris = [record.uri for record in group if record.uri]
        failed = add_with_progress(client, str(playlist_id), uris)
        failures += len(failed)
        url = playlist_url(playlist)
        urls.append(url)
        print(f"Playlist: {url}", file=sys.stderr)

    print("\nDone.")
    for url in urls:
        print(url)
    if failures:
        print(f"{failures} item(s) could not be added.", file=sys.stderr)
        return 1
    return 0


def add_common_data_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "inputs",
        nargs="+",
        help="Streaming history JSON file(s), a directory, or a .zip export.",
    )
    parser.add_argument(
        "--include-episodes",
        action="store_true",
        help="Include podcast episodes as well as songs.",
    )
    parser.add_argument(
        "--min-ms-played",
        type=int,
        default=0,
        metavar="MS",
        help="Ignore plays shorter than this many milliseconds (default: 0).",
    )
    parser.add_argument(
        "--min-streams",
        type=int,
        default=1,
        metavar="N",
        help="Only include items played at least N times (default: 1).",
    )
    parser.add_argument(
        "--sort",
        choices=SORT_CHOICES,
        default="plays",
        help="Order used when building the playlist (default: plays).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Only consider the first N unique items after sorting (before genre lookups).",
    )


def add_auth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--client-id",
        default=os.environ.get("SPOTIFY_CLIENT_ID"),
        help="Spotify app client ID (or set SPOTIFY_CLIENT_ID).",
    )
    parser.add_argument(
        "--redirect-uri",
        default=DEFAULT_REDIRECT_URI,
        help=f"Registered redirect URI (default: {DEFAULT_REDIRECT_URI}).",
    )
    parser.add_argument(
        "--token-cache",
        default=str(default_token_path()),
        help="Where to cache OAuth tokens.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Print the authorization URL instead of opening a browser.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spotify_playlist.py",
        description=(
            "Parse Spotify streaming history and create a playlist containing every unique song."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse_parser = subparsers.add_parser(
        "parse",
        help="Parse an export and print or save the unique songs.",
    )
    add_common_data_args(parse_parser)
    parse_parser.add_argument(
        "--format",
        choices=("json", "csv"),
        default="json",
        help="Output format (default: json).",
    )
    parse_parser.add_argument(
        "--output",
        "-o",
        help="Write to this file instead of stdout (use - for stdout).",
    )
    parse_parser.set_defaults(func=cmd_parse)

    auth_parser = subparsers.add_parser(
        "auth",
        help="Authorize with Spotify and cache a token.",
    )
    add_auth_args(auth_parser)
    auth_parser.set_defaults(func=cmd_auth)

    create_parser = subparsers.add_parser(
        "create",
        help="Create Spotify playlist(s) from an export.",
    )
    add_common_data_args(create_parser)
    add_auth_args(create_parser)
    create_parser.add_argument(
        "--name",
        default=None,
        help="Playlist name (default: Every Song I've Played).",
    )
    create_parser.add_argument(
        "--description",
        default="Created from my Spotify streaming history.",
        help="Playlist description.",
    )
    create_parser.add_argument(
        "--public",
        dest="public",
        action="store_true",
        help="Make the playlist public.",
    )
    create_parser.add_argument(
        "--private",
        dest="public",
        action="store_false",
        help="Make the playlist private (default).",
    )
    create_parser.set_defaults(public=False)
    create_parser.add_argument(
        "--append-to",
        metavar="PLAYLIST_ID",
        help="Append to an existing playlist instead of creating one.",
    )
    create_parser.add_argument(
        "--max-tracks",
        type=int,
        default=PLAYLIST_MAX,
        metavar="N",
        help="Max items per playlist; larger libraries are split (default: 10000).",
    )
    create_parser.add_argument(
        "--genres",
        action="append",
        metavar="GENRE[,GENRE...]",
        help=(
            "Only include tracks whose artist genres match one of these. "
            "May be repeated or comma-separated."
        ),
    )
    create_parser.add_argument(
        "--exclude-genres",
        action="append",
        metavar="GENRE[,GENRE...]",
        help="Exclude tracks whose artist genres match one of these.",
    )
    create_parser.add_argument(
        "--genre-cache",
        default=None,
        help="Where to cache track/artist genre lookups (default: next to the token cache).",
    )
    create_parser.add_argument(
        "--genre-delay",
        type=float,
        default=0.1,
        metavar="SECONDS",
        help="Delay between genre lookup requests (default: 0.1).",
    )
    create_parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Show what would be created without writing to Spotify. Genre and URI "
            "lookups still run when needed for accurate counts."
        ),
    )
    create_parser.add_argument(
        "--no-resolve",
        action="store_true",
        help="Do not search Spotify for tracks missing a URI in the export.",
    )
    create_parser.add_argument(
        "--search-delay",
        type=float,
        default=0.2,
        metavar="SECONDS",
        help="Delay between search requests when resolving URIs (default: 0.2).",
    )
    create_parser.set_defaults(func=cmd_create)

    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "max_tracks", None) is not None and not 1 <= args.max_tracks <= PLAYLIST_MAX:
        parser.error(f"--max-tracks must be between 1 and {PLAYLIST_MAX}")
    if getattr(args, "limit", None) is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if getattr(args, "min_streams", None) is not None and args.min_streams < 1:
        parser.error("--min-streams must be at least 1")
    if getattr(args, "genre_delay", None) is not None and args.genre_delay < 0:
        parser.error("--genre-delay cannot be negative")
    try:
        return int(args.func(args))
    except (SpotifyError, FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
