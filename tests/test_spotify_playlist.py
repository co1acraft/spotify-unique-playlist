import contextlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

import spotify_playlist as sp


class ParseEntryTests(unittest.TestCase):
    def test_new_format_track(self):
        play = sp.parse_entry(
            {
                "ts": "2024-01-02T03:04:05Z",
                "master_metadata_track_name": "Song",
                "master_metadata_album_artist_name": "Artist",
                "master_metadata_album_album_name": "Album",
                "spotify_track_uri": "spotify:track:abc",
                "ms_played": 12345,
                "skipped": False,
            }
        )
        self.assertIsNotNone(play)
        assert play is not None
        self.assertEqual(play.kind, "track")
        self.assertEqual(play.uri, "spotify:track:abc")
        self.assertEqual(play.name, "Song")
        self.assertEqual(play.artist, "Artist")
        self.assertEqual(play.album, "Album")
        self.assertEqual(play.ms_played, 12345)
        self.assertEqual(play.played_at.year, 2024)

    def test_legacy_format_track_without_uri(self):
        play = sp.parse_entry(
            {
                "endTime": "2020-05-06 07:08",
                "artistName": "Old Artist",
                "trackName": "Old Song",
                "msPlayed": 60000,
            }
        )
        self.assertIsNotNone(play)
        assert play is not None
        self.assertEqual(play.kind, "track")
        self.assertIsNone(play.uri)
        self.assertEqual(play.artist, "Old Artist")
        self.assertEqual(play.name, "Old Song")

    def test_episode(self):
        play = sp.parse_entry(
            {
                "ts": "2025-01-01T00:00:00Z",
                "episode_name": "Episode",
                "episode_show_name": "Show",
                "spotify_episode_uri": "spotify:episode:xyz",
                "ms_played": 1000,
            }
        )
        self.assertIsNotNone(play)
        assert play is not None
        self.assertEqual(play.kind, "episode")
        self.assertEqual(play.uri, "spotify:episode:xyz")
        self.assertEqual(play.artist, "Show")

    def test_local_uri_is_dropped_but_entry_kept(self):
        play = sp.parse_entry(
            {
                "ts": "2025-01-01T00:00:00Z",
                "master_metadata_track_name": "Local Song",
                "master_metadata_album_artist_name": "Artist",
                "spotify_track_uri": "spotify:local:abc",
                "ms_played": 1000,
            }
        )
        self.assertIsNotNone(play)
        assert play is not None
        self.assertIsNone(play.uri)
        self.assertEqual(play.name, "Local Song")

    def test_non_play_entry(self):
        self.assertIsNone(sp.parse_entry({"foo": "bar"}))
        self.assertIsNone(sp.parse_entry("nope"))


class LibraryTests(unittest.TestCase):
    def test_dedupes_by_uri_and_counts_plays(self):
        library = sp.Library()
        for _ in range(3):
            library.add(
                sp.Play(
                    kind="track",
                    uri="spotify:track:abc",
                    name="Song",
                    artist="Artist",
                    album="Album",
                    ms_played=1000,
                    played_at=None,
                    skipped=False,
                )
            )
        self.assertEqual(len(library.records), 1)
        self.assertEqual(library.records[0].plays, 3)
        self.assertEqual(library.records[0].ms_played, 3000)

    def test_local_play_merges_into_catalog_track(self):
        library = sp.Library()
        library.add(
            sp.Play(
                kind="track",
                uri=None,
                name="Song",
                artist="Artist",
                album="",
                ms_played=1000,
                played_at=None,
                skipped=False,
            )
        )
        library.add(
            sp.Play(
                kind="track",
                uri="spotify:track:abc",
                name="Song",
                artist="Artist",
                album="Album",
                ms_played=2000,
                played_at=None,
                skipped=False,
            )
        )
        self.assertEqual(len(library.records), 1)
        self.assertEqual(library.records[0].uri, "spotify:track:abc")
        self.assertEqual(library.records[0].plays, 2)

    def test_uri_then_local_merges(self):
        library = sp.Library()
        library.add(
            sp.Play(
                kind="track",
                uri="spotify:track:abc",
                name="Song",
                artist="Artist",
                album="Album",
                ms_played=1000,
                played_at=None,
                skipped=False,
            )
        )
        library.add(
            sp.Play(
                kind="track",
                uri=None,
                name="Song",
                artist="Artist",
                album="",
                ms_played=1000,
                played_at=None,
                skipped=False,
            )
        )
        self.assertEqual(len(library.records), 1)

    def test_same_name_different_uri_kept_separate(self):
        library = sp.Library()
        for uri in ("spotify:track:one", "spotify:track:two"):
            library.add(
                sp.Play(
                    kind="track",
                    uri=uri,
                    name="Song",
                    artist="Artist",
                    album="",
                    ms_played=1000,
                    played_at=None,
                    skipped=False,
                )
            )
        self.assertEqual(len(library.records), 2)

    def test_dedupe_by_uri_after_resolution(self):
        library = sp.Library()
        library.add(
            sp.Play(
                kind="track",
                uri=None,
                name="Song A",
                artist="Artist",
                album="",
                ms_played=1000,
                played_at=None,
                skipped=False,
            )
        )
        library.add(
            sp.Play(
                kind="track",
                uri="spotify:track:abc",
                name="Song B",
                artist="Artist",
                album="",
                ms_played=1000,
                played_at=None,
                skipped=False,
            )
        )
        library.records[0].uri = "spotify:track:abc"
        library.dedupe_by_uri()
        self.assertEqual(len(library.records), 1)
        self.assertEqual(library.records[0].plays, 2)


class BuildLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def write_json(self, name, data):
        path = self.path / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_new_export_dedupes_and_filters(self):
        self.write_json(
            "Streaming_History_Audio_2024.json",
            [
                {
                    "ts": "2024-01-01T00:00:00Z",
                    "master_metadata_track_name": "A",
                    "master_metadata_album_artist_name": "X",
                    "spotify_track_uri": "spotify:track:a",
                    "ms_played": 60000,
                },
                {
                    "ts": "2024-01-02T00:00:00Z",
                    "master_metadata_track_name": "A",
                    "master_metadata_album_artist_name": "X",
                    "spotify_track_uri": "spotify:track:a",
                    "ms_played": 30000,
                },
                {
                    "ts": "2024-01-03T00:00:00Z",
                    "episode_name": "Pod",
                    "episode_show_name": "Show",
                    "spotify_episode_uri": "spotify:episode:p",
                    "ms_played": 60000,
                },
            ],
        )
        library, stats = sp.build_library([str(self.path)])
        self.assertEqual(len(library.records), 1)
        self.assertEqual(library.records[0].plays, 2)
        self.assertEqual(library.records[0].ms_played, 90000)
        self.assertEqual(stats.episodes_skipped, 1)

    def test_min_ms_filter(self):
        self.write_json(
            "history.json",
            [
                {
                    "ts": "2024-01-01T00:00:00Z",
                    "master_metadata_track_name": "Skip",
                    "master_metadata_album_artist_name": "X",
                    "spotify_track_uri": "spotify:track:skip",
                    "ms_played": 1000,
                },
                {
                    "ts": "2024-01-01T00:00:00Z",
                    "master_metadata_track_name": "Keep",
                    "master_metadata_album_artist_name": "X",
                    "spotify_track_uri": "spotify:track:keep",
                    "ms_played": 60000,
                },
            ],
        )
        library, stats = sp.build_library([str(self.path)], min_ms_played=30000)
        self.assertEqual([r.name for r in library.records], ["Keep"])
        self.assertEqual(stats.short_skipped, 1)

    def test_include_episodes(self):
        self.write_json(
            "history.json",
            [
                {
                    "ts": "2024-01-01T00:00:00Z",
                    "episode_name": "Pod",
                    "episode_show_name": "Show",
                    "spotify_episode_uri": "spotify:episode:p",
                    "ms_played": 60000,
                }
            ],
        )
        library, _stats = sp.build_library(
            [str(self.path)], include_episodes=True
        )
        self.assertEqual(len(library.records), 1)
        self.assertEqual(library.records[0].kind, "episode")

    def test_legacy_export(self):
        self.write_json(
            "StreamingHistory0.json",
            [
                {
                    "endTime": "2020-01-01 10:00",
                    "artistName": "Artist",
                    "trackName": "Song",
                    "msPlayed": 60000,
                },
                {
                    "endTime": "2020-01-02 10:00",
                    "artistName": "Artist",
                    "trackName": "Song",
                    "msPlayed": 60000,
                },
            ],
        )
        library, _stats = sp.build_library([str(self.path)])
        self.assertEqual(len(library.records), 1)
        self.assertIsNone(library.records[0].uri)
        self.assertEqual(library.records[0].plays, 2)

    def test_ignores_unrelated_json(self):
        self.write_json("Userdata.json", {"username": "someone"})
        self.write_json("Playlist1.json", {"playlists": []})
        self.write_json(
            "history.json",
            [
                {
                    "ts": "2024-01-01T00:00:00Z",
                    "master_metadata_track_name": "Song",
                    "master_metadata_album_artist_name": "X",
                    "spotify_track_uri": "spotify:track:a",
                    "ms_played": 1000,
                }
            ],
        )
        library, stats = sp.build_library([str(self.path)])
        self.assertEqual(len(library.records), 1)
        self.assertEqual(stats.files, 1)

    def test_zip_export(self):
        archive_path = self.path / "export.zip"
        data = [
            {
                "ts": "2024-01-01T00:00:00Z",
                "master_metadata_track_name": "Zipped",
                "master_metadata_album_artist_name": "Artist",
                "spotify_track_uri": "spotify:track:z",
                "ms_played": 1000,
            }
        ]
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("Spotify Account Data/Streaming_History_Audio.json", json.dumps(data))
            archive.writestr("Spotify Account Data/ReadMe.txt", "ignore me")
        library, _stats = sp.build_library([str(archive_path)])
        self.assertEqual(len(library.records), 1)
        self.assertEqual(library.records[0].name, "Zipped")


class OutputTests(unittest.TestCase):
    def test_csv_and_json(self):
        record = sp.Record(
            kind="track",
            uri="spotify:track:abc",
            name="Song, with comma",
            artist="Artist",
            album="Album",
            plays=2,
            ms_played=120000,
        )
        csv_text = sp.records_to_csv([record])
        self.assertIn("uri,kind,name,artist,album,plays", csv_text)
        self.assertIn('"Song, with comma"', csv_text)
        parsed = json.loads(sp.records_to_json([record]))
        self.assertEqual(parsed[0]["name"], "Song, with comma")
        self.assertEqual(parsed[0]["minutes"], 2.0)

    def test_sort_orders(self):
        records = [
            sp.Record("track", "spotify:track:b", "B", "Y", "", plays=1),
            sp.Record("track", "spotify:track:a", "A", "X", "", plays=5),
        ]
        self.assertEqual([r.name for r in sp.sort_records(records, "plays")], ["A", "B"])
        self.assertEqual([r.name for r in sp.sort_records(records, "name")], ["A", "B"])
        self.assertEqual([r.name for r in sp.sort_records(records, "artist")], ["A", "B"])


class ThresholdTests(unittest.TestCase):
    def test_min_streams(self):
        records = [
            sp.Record("track", "spotify:track:a", "A", "X", "", plays=1),
            sp.Record("track", "spotify:track:b", "B", "X", "", plays=3),
            sp.Record("track", "spotify:track:c", "C", "X", "", plays=2),
        ]
        kept, skipped = sp.filter_min_streams(records, 2)
        self.assertEqual([r.name for r in kept], ["B", "C"])
        self.assertEqual(skipped, 1)

    def test_min_streams_one_keeps_everything(self):
        records = [sp.Record("track", "spotify:track:a", "A", "X", "", plays=1)]
        kept, skipped = sp.filter_min_streams(records, 1)
        self.assertEqual(kept, records)
        self.assertEqual(skipped, 0)

    def test_split_genres(self):
        self.assertEqual(
            sp.split_genres(["rock, pop", " hip hop ", "rock"]),
            ["rock", "pop", "hip hop", "rock"],
        )
        self.assertEqual(sp.split_genres(None), [])

    def test_genre_matching(self):
        self.assertTrue(sp.genre_matches(["classic rock", "album rock"], ["rock"]))
        self.assertTrue(sp.genre_matches(["hip hop"], ["hip-hop"]))
        self.assertTrue(sp.genre_matches(["rock"], ["classic rock"]))
        self.assertTrue(sp.genre_matches(["children's music"], ["childrens music"]))
        self.assertFalse(sp.genre_matches(["pop"], ["rock"]))
        self.assertFalse(sp.genre_matches([], ["rock"]))


class GenreFilterTests(unittest.TestCase):
    class FakeClient:
        def __init__(self, track_artists, artist_genres):
            self.track_artists = track_artists
            self.artist_genres = artist_genres
            self.calls = 0

        def api(self, method, path, payload=None, params=None, retries=6):
            self.calls += 1
            if path.startswith("/tracks/"):
                track_id = path.rsplit("/", 1)[-1]
                artists = self.track_artists.get(track_id, [])
                return {"artists": [{"id": artist} for artist in artists]}
            if path.startswith("/artists/"):
                artist_id = path.rsplit("/", 1)[-1]
                return {"genres": self.artist_genres.get(artist_id, [])}
            raise AssertionError(f"unexpected path: {path}")

    def make_records(self):
        return [
            sp.Record("track", "spotify:track:a", "A", "X", "", plays=5),
            sp.Record("track", "spotify:track:b", "B", "Y", "", plays=3),
            sp.Record("track", "spotify:track:c", "C", "Z", "", plays=2),
            sp.Record("track", None, "D", "W", "", plays=1),
        ]

    def make_client(self):
        return self.FakeClient(
            {"a": ["artist1"], "b": ["artist2"], "c": ["artist3"]},
            {"artist1": ["classic rock", "album rock"], "artist2": ["pop"], "artist3": []},
        )

    def test_allow_genre(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "genres.json"
            client = self.make_client()
            with contextlib.redirect_stderr(io.StringIO()):
                kept, stats = sp.apply_genre_filters(
                    client, self.make_records(), ["rock"], [], cache, delay=0
                )
            self.assertEqual([r.name for r in kept], ["A"])
            self.assertEqual(stats.kept, 1)
            self.assertEqual(stats.excluded_allow, 3)
            self.assertEqual(stats.unknown, 2)
            self.assertEqual(client.calls, 6)

    def test_exclude_genre_keeps_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "genres.json"
            client = self.make_client()
            with contextlib.redirect_stderr(io.StringIO()):
                kept, stats = sp.apply_genre_filters(
                    client, self.make_records(), [], ["pop"], cache, delay=0
                )
            self.assertEqual([r.name for r in kept], ["A", "C", "D"])
            self.assertEqual(stats.excluded_exclude, 1)

    def test_genre_cache_avoids_repeat_requests(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "genres.json"
            first = self.make_client()
            with contextlib.redirect_stderr(io.StringIO()):
                sp.apply_genre_filters(first, self.make_records(), ["rock"], [], cache, delay=0)
            self.assertGreater(first.calls, 0)

            second = self.make_client()
            with contextlib.redirect_stderr(io.StringIO()):
                kept, _stats = sp.apply_genre_filters(
                    second, self.make_records(), ["rock"], [], cache, delay=0
                )
            self.assertEqual([r.name for r in kept], ["A"])
            self.assertEqual(second.calls, 0)


class AddItemsTests(unittest.TestCase):
    def make_client(self, fake_api):
        client = object.__new__(sp.SpotifyClient)
        client.api = fake_api
        return client

    def test_batches_in_groups_of_100(self):
        calls = []

        def fake_api(method, path, payload=None, params=None, retries=6):
            calls.append(payload["uris"])
            return None

        client = self.make_client(fake_api)
        uris = [f"spotify:track:{i}" for i in range(250)]
        failed = client.add_items("playlist", uris)
        self.assertEqual([len(call) for call in calls], [100, 100, 50])
        self.assertEqual(failed, [])

    def test_retries_rejected_batch_one_by_one(self):
        calls = []

        def fake_api(method, path, payload=None, params=None, retries=6):
            batch = payload["uris"]
            if "spotify:track:bad" in batch:
                raise sp.SpotifyError("Spotify API POST failed (400): invalid uri")
            calls.append(batch)
            return None

        client = self.make_client(fake_api)
        failed = client.add_items(
            "playlist", ["spotify:track:good", "spotify:track:bad"]
        )
        self.assertEqual(failed, ["spotify:track:bad"])
        self.assertEqual(calls, [["spotify:track:good"]])


class ResolveTests(unittest.TestCase):
    def test_resolve_and_cache(self):
        class FakeClient:
            def __init__(self):
                self.queries = 0

            def search(self, query, kind, limit=10):
                self.queries += 1
                return [
                    {
                        "uri": "spotify:track:found",
                        "name": "Song",
                        "artists": [{"name": "Artist"}],
                    }
                ]

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "resolved.json"
            record = sp.Record("track", None, "Song", "Artist", "")
            client = FakeClient()
            with contextlib.redirect_stderr(io.StringIO()):
                records, unresolved, count = sp.resolve_missing_uris(
                    client, [record], cache, delay=0
                )
            self.assertEqual(records[0].uri, "spotify:track:found")
            self.assertEqual(unresolved, [])
            self.assertEqual(count, 1)
            self.assertEqual(client.queries, 1)

            second = sp.Record("track", None, "Song", "Artist", "")
            with contextlib.redirect_stderr(io.StringIO()):
                records, unresolved, count = sp.resolve_missing_uris(
                    client, [second], cache, delay=0
                )
            self.assertEqual(records[0].uri, "spotify:track:found")
            self.assertEqual(count, 1)
            self.assertEqual(client.queries, 1)

    def test_choose_track_result(self):
        results = [
            {
                "uri": "spotify:track:wrong",
                "name": "Song (Live)",
                "artists": [{"name": "Other"}],
            },
            {
                "uri": "spotify:track:right",
                "name": "Song",
                "artists": [{"name": "Artist"}],
            },
        ]
        self.assertEqual(sp.choose_track_result(results, "Song", "Artist"), "spotify:track:right")

    def test_choose_track_result_rejects_wrong_artist(self):
        results = [
            {
                "uri": "spotify:track:wrong",
                "name": "Song",
                "artists": [{"name": "Other"}],
            }
        ]
        self.assertIsNone(sp.choose_track_result(results, "Song", "Artist"))


if __name__ == "__main__":
    unittest.main()
