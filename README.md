# Spotify Unique Playlist
> [!WARNING]
> this completely vibecoded and there may be issues
> who knows
A dependency-free Python tool that parses your Spotify streaming history and creates a playlist containing every unique song you have ever played. Requires Python 3.10 or newer; no third-party packages.

It understands both current and legacy Spotify data exports, deduplicates songs by Spotify track URI, keeps play counts and listening dates, can filter by minimum play count and artist genre, and uses the current Spotify Web API (2026 endpoints).

## Quick start

```bash
# 1. Inspect the unique songs in your export (works offline)
python3 spotify_playlist.py parse ~/Downloads/spotify_export --format csv -o unique_songs.csv

# 2. Create the playlist
python3 spotify_playlist.py create ~/Downloads/spotify_export \
  --client-id YOUR_SPOTIFY_CLIENT_ID \
  --name "Every Song I've Played"

# Optional thresholds: at least 5 plays, only rock/metal artists
python3 spotify_playlist.py create ~/Downloads/spotify_export \
  --client-id YOUR_SPOTIFY_CLIENT_ID \
  --min-streams 5 \
  --genres rock,metal \
  --name "Rock Songs I Actually Play"
```

The first `create` run opens your browser for Spotify authorization and caches the token in `~/.config/spotify-unique-playlist/token.json`.

Try it without Spotify credentials or data:

```bash
python3 spotify_playlist.py parse examples --format csv
python3 spotify_playlist.py create examples --dry-run
```

## Getting your streaming history

1. Go to <https://www.spotify.com/account/privacy/>.
2. Request **Extended streaming history**. This is strongly recommended because it includes `spotify_track_uri` values.
3. Wait for the email, then download and unzip the archive.
4. Pass the extracted folder, individual JSON files, or the original `.zip` to this tool.

The older **Account data** export also works, but it usually has no track URIs. In that case the tool searches Spotify for each unique artist/title to find the track before adding it. This is slower and can leave ambiguous songs unmatched. Use `parse` first to review what was found.

## Spotify app setup

Creating a playlist requires a Spotify app (free to create) and a user login:

1. Open the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) and create an app.
2. In the app settings, add this exact redirect URI:
   ```
   http://127.0.0.1:8888/callback
   ```
3. Copy the app's **Client ID**.
4. Development Mode apps require the app owner to have an active Spotify Premium subscription (as of February 2026). If you are using someone else's app, make sure your account is added under the app's user management.

No client secret is needed. The tool uses OAuth 2.0 Authorization Code with PKCE.

If you use a different redirect URI, pass `--redirect-uri` to both the Spotify dashboard registration and the tool. Loopback redirects are captured automatically; non-loopback redirects switch to a manual "paste the URL" flow.

## Commands

### `parse`

Parse an export and output the unique songs as JSON or CSV.

```bash
python3 spotify_playlist.py parse <inputs...> [options]
```

| Option | Description |
| --- | --- |
| `--format json\|csv` | Output format. Default: `json`. |
| `-o, --output FILE` | Write to a file instead of stdout. |
| `--include-episodes` | Include podcast episodes too. |
| `--min-ms-played MS` | Ignore plays shorter than this (e.g. `30000` to drop skips under 30s). |
| `--min-streams N` | Only keep items played at least N times. Default: `1`. |
| `--sort plays\|recent\|first\|name\|artist` | Sort order. Default: `plays`. |
| `--limit N` | Only keep the first N unique items after sorting. |

### `auth`

Authorize once and cache the token without creating anything.

```bash
python3 spotify_playlist.py auth --client-id YOUR_CLIENT_ID
```

### `create`

Create one or more playlists from the export.

```bash
python3 spotify_playlist.py create <inputs...> --client-id YOUR_CLIENT_ID [options]
```

| Option | Description |
| --- | --- |
| `--name NAME` | Playlist name. Default: `Every Song I've Played`. |
| `--description TEXT` | Playlist description. |
| `--public` / `--private` | Visibility. Default: private. |
| `--append-to PLAYLIST_ID` | Add to an existing playlist instead of creating a new one. |
| `--max-tracks N` | Items per playlist. Default and maximum: `10000`; larger libraries are split into numbered parts. |
| `--min-streams N` | Only include items played at least N times. Default: `1`. |
| `--genres GENRE[,GENRE...]` | Only include tracks whose artist genres match one of these. May be repeated. |
| `--exclude-genres GENRE[,GENRE...]` | Exclude tracks whose artist genres match one of these. May be repeated. |
| `--genre-cache FILE` | Cache for track/artist genre lookups. Default: `genres.json` next to the token cache. |
| `--genre-delay SECONDS` | Delay between genre lookup requests. Default: `0.1`. |
| `--dry-run` | Show the plan without writing to Spotify. Genre and URI lookups still run when needed for accurate counts. |
| `--no-resolve` | Do not search for tracks missing a URI in the export. |
| `--search-delay SECONDS` | Delay between search requests during URI resolution. Default: `0.2`. |
| `--client-id ID` | Spotify app client ID. Can also use `SPOTIFY_CLIENT_ID`. |
| `--redirect-uri URI` | OAuth redirect URI. Default: `http://127.0.0.1:8888/callback`. |
| `--token-cache FILE` | Token cache location. |
| `--no-browser` | Print the authorization URL instead of opening a browser. |
| `--include-episodes` | Include podcast episodes. |
| `--min-ms-played MS` | Ignore short plays. |
| `--sort ...` / `--limit N` | Same as `parse`. |

Spotify limits a normal playlist to 10,000 items. If your library has more unique songs, the tool automatically creates `Name (1/3)`, `Name (2/3)`, etc. Use `--limit` if you only want the top N most-played songs.

## Stream and genre thresholds

```bash
# Only songs played at least 5 times
python3 spotify_playlist.py create ~/Downloads/spotify_export \
  --client-id YOUR_CLIENT_ID --min-streams 5

# Filter your 500 most-played songs down to rock/metal with at least 3 plays
python3 spotify_playlist.py create ~/Downloads/spotify_export \
  --client-id YOUR_CLIENT_ID --min-streams 3 --genres rock,metal --limit 500

# Exclude holiday music and children's music
python3 spotify_playlist.py create ~/Downloads/spotify_export \
  --client-id YOUR_CLIENT_ID --exclude-genres holiday,"children's music"
```

`--min-streams` counts how many times a song appears in your history after play-length and dedupe processing. Use `--min-ms-played` as well if you do not want brief skips to count.

Spotify exports do not contain genre data. When `--genres` or `--exclude-genres` is used, the tool looks up each track's artists with `GET /tracks/{id}` and `GET /artists/{id}`, then uses the artist's genre tags. Matching is case-insensitive and punctuation-insensitive, so `rock` matches `classic rock` and `hip-hop` matches `hip hop`.

Genre lookups are cached in `~/.config/spotify-unique-playlist/genres.json`. The first run can be slow for large libraries; increase `--genre-delay` if you hit rate limits. Notes:

- Genres are artist-level, not track-level. An artist may have several tags.
- With an allowlist, tracks whose artists have no genre tags are excluded.
- `--exclude-genres` only removes known matches; tracks with unknown genres are kept.
- Podcast episodes have no genre tags, so an allowlist excludes them.
- `--limit` is applied before genre lookups, so a genre-filtered run may produce fewer than N items.
- `--genres`/`--exclude-genres` are only available on `create`, because they require Spotify API access. `--dry-run` performs read-only lookups when a genre filter is set so the counts are accurate.

## How it works

- Accepts a directory, `.json` files, or a `.zip`, and ignores unrelated JSON files in Spotify exports.
- Supports the modern extended-history fields (`ts`, `master_metadata_*`, `spotify_track_uri`, `ms_played`) and legacy fields (`endTime`, `artistName`, `trackName`, `msPlayed`).
- Deduplicates by `spotify_track_uri`, so live versions, remasters, and album/single versions remain distinct.
- Merges local-file plays into a catalog track when a matching artist/title with a URI exists.
- Tracks play counts, total listening time, first/last play dates, and skip counts.
- Filters by minimum play count (`--min-streams`) and, for playlist creation, by artist genres (`--genres`/`--exclude-genres`).
- Resolves missing URIs with `GET /search` and caches successful matches in `~/.config/spotify-unique-playlist/resolved_tracks.json`.
- Caches track-to-artist and artist-to-genre lookups in `~/.config/spotify-unique-playlist/genres.json`.
- Adds items in batches of 100 with automatic `429 Retry-After` and server-error backoff. A rejected batch is retried one item at a time so one unavailable song does not block the rest.

The tool only sends track/artist search queries to Spotify when your export lacks URIs. It does not upload your history anywhere else.

## Testing

```bash
python3 -m unittest discover -s tests -t . -v
```

## Troubleshooting

- **`403 Forbidden` when creating a playlist**: confirm the app owner has Premium (Development Mode requirement), your account is allowed to use the app, and the app has the `playlist-modify-private`/`playlist-modify-public` scopes (the tool requests both).
- **`INVALID_CLIENT: Invalid redirect URI`**: the URI registered in the dashboard must exactly match `--redirect-uri`, including the port and `/callback` path.
- **Authorization seems stale**: delete `~/.config/spotify-unique-playlist/token.json` and run `auth` again.
- **Many unmatched songs**: use the Extended streaming history export, which contains Spotify URIs. You can also run `parse` and review the list before creating the playlist.
- **Rate limiting**: messages appear automatically and the tool waits as instructed by Spotify. Increase `--search-delay` or `--genre-delay` for very large libraries.
- **Genre filtering is slow**: genre data requires one request per track plus one per unique artist. The cache makes repeat runs fast; use `--limit` to test on a small subset first.
- **Songs missing from genre results**: artist genre tags are sometimes empty. With `--genres`, those tracks are excluded; run without the allowlist or use `--exclude-genres` instead.
- **Playlist stops at 10,000**: that is Spotify's hard limit. The tool splits larger libraries into numbered playlists automatically.
