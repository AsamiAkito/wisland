import unittest

import httpx

from wisland.services.lyrics import LyricsService


TRACK = {"app_name": "Spotify", "title": "富士山下", "artist": "Eason Chan",
         "album": "What's Going On...?", "duration": 259}


class LyricsTests(unittest.TestCase):
    def fetch(self, media, netease, records):
        def respond(request):
            if request.url.host == "music.163.com":
                if isinstance(netease, int):
                    return httpx.Response(netease)
                return httpx.Response(200, json=netease)
            self.assertEqual(request.url.host, "lrclib.net")
            self.assertEqual(request.url.params["track_name"], TRACK["title"])
            self.assertEqual(request.url.params["artist_name"], TRACK["artist"])
            self.assertIsNotNone(records, "LRCLIB unexpectedly queried")
            return httpx.Response(200, json=records)

        service = LyricsService()
        service._client.close()
        with httpx.Client(transport=httpx.MockTransport(respond)) as service._client:
            return service._fetch(media)

    def test_netease_id_retains_translation(self):
        lyrics = self.fetch(dict(TRACK, song_id="123"),
                            {"lrc": {"lyric": "[00:07]Song"}, "tlyric": {"lyric": "[00:07]翻译"}}, None)
        self.assertEqual(lyrics.at(8), (0, "Song", "翻译"))

    def test_fallback_selects_synced_recording(self):
        record = {"trackName": TRACK["title"], "artistName": TRACK["artist"], "albumName": TRACK["album"],
                  "duration": 259, "syncedLyrics": "[00:12]Matched"}
        records = [dict(record, syncedLyrics=None), dict(record, artistName="Cover", syncedLyrics="[00:00]Cover"),
                   dict(record, duration=299, syncedLyrics="[00:00]Live"), record]
        for media, response in [(TRACK, {"result": {"songs": []}}), (TRACK, 503),
                                (dict(TRACK, song_id="123"), {"uncollected": True})]:
            with self.subTest(response=response):
                lyrics = self.fetch(media, response, records)
                self.assertIsNotNone(lyrics)
                self.assertEqual(lyrics.at(13), (0, "Matched", ""))

    def test_unrelated_netease_hit_does_not_mask_lrclib(self):
        # A song of equal length alone is insufficient evidence to fetch its lyrics.
        response = {"result": {"songs": [{"id": 2, "name": "Different title", "duration": 259000}]},
                    "lrc": {"lyric": "[00:01]Wrong song"}}
        self.assertIsNone(self.fetch(TRACK, response, []))

    def test_new_timeline_retries_matching(self):
        self.assertNotEqual(LyricsService.key(dict(TRACK, duration=0)), LyricsService.key(TRACK))
