"""Tests against a fake Subsonic server on 127.0.0.1 (stdlib only, no network).

    python -m unittest test_subsonic
"""
import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
USER, PASSWORD = "alice", "s3cret pass"
AUDIO = b"ID3fake-audio-bytes" * 1000

SONG = {"id": "42", "title": "Morning Lines", "artist": "The Trio", "album": "Demo", "duration": 184,
        "suffix": "flac"}


class FakeSubsonic(BaseHTTPRequestHandler):
    seen = []                 # every query, to check what went over the wire
    download_allowed = True

    def log_message(self, *a):
        pass

    def _json(self, payload, status="ok"):
        body = json.dumps({"subsonic-response": dict(payload, status=status, version="1.16.1")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, code, message):
        self._json({"error": {"code": code, "message": message}}, status="failed")

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        FakeSubsonic.seen.append(q)
        if q.get("u") != USER or q.get("t") != hashlib.md5((PASSWORD + q.get("s", "")).encode()).hexdigest():
            return self._fail(40, "Wrong username or password")
        ep = u.path.rsplit("/", 1)[-1]
        if ep == "ping.view":
            return self._json({})
        if ep == "search3.view":
            songs = [SONG] if q["query"].lower() in "morning lines" else []
            return self._json({"searchResult3": {"song": songs[int(q["songOffset"]):][:int(q["songCount"])]}})
        if ep == "getSong.view":
            return self._json({"song": SONG}) if q["id"] == "42" else self._fail(70, "Song not found")
        if ep == "getAlbum.view":
            return self._json({"album": {"id": "7", "name": "Demo", "artist": "The Trio", "song": [SONG, SONG]}})
        if ep == "getPlaylist.view":
            return self._json({"playlist": {"id": "p1", "name": "Road", "owner": "alice", "entry": [SONG]}})
        if ep in ("download.view", "stream.view"):
            if ep == "download.view" and not FakeSubsonic.download_allowed:
                # Some servers answer binary endpoints' errors in XML whatever f says.
                body = (b'<?xml version="1.0"?><subsonic-response xmlns="http://subsonic.org/restapi" '
                        b'status="failed" version="1.16.1"><error code="50" '
                        b'message="User is not authorized for the given operation."/></subsonic-response>')
                self.send_response(200)
                self.send_header("Content-Type", "text/xml; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/flac")
            self.send_header("Content-Length", str(len(AUDIO)))
            self.end_headers()
            self.wfile.write(AUDIO)
            return
        self._fail(0, "unknown endpoint")


def load_extension(settings, data):
    spec = importlib.util.spec_from_file_location("ext_subsonic_test", os.path.join(HERE, "media_plugin.py"))
    mod = importlib.util.module_from_spec(spec)
    # What the app's bridge injects before the module runs.
    mod.extension_id = "subsonic"
    mod.data_dir = lambda: data
    mod.reglage = lambda cle, defaut=None: settings.get(cle, defaut)
    spec.loader.exec_module(mod)
    return mod


class SubsonicTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSubsonic)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        FakeSubsonic.seen.clear()
        FakeSubsonic.download_allowed = True
        self.tmp = tempfile.mkdtemp()
        self.settings = {"server": self.base + "/", "user": USER, "password": PASSWORD}
        self.ext = load_extension(self.settings, self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_search_maps_songs_and_never_sends_the_password(self):
        r = self.ext.search("morning", 20, 0)
        self.assertEqual(len(r["results"]), 1)
        t = r["results"][0]
        self.assertEqual((t["sourceId"], t["title"], t["artist"], t["durationMs"], t["duration"]),
                         ("subsonic-song-42", "Morning Lines", "The Trio", 184000, "3:04"))
        self.assertEqual(t["thumbnailUrl"], "")
        q = FakeSubsonic.seen[-1]
        self.assertNotIn("p", q)
        self.assertNotIn(PASSWORD, json.dumps(FakeSubsonic.seen))
        self.assertNotIn(PASSWORD, json.dumps(r))

    def test_each_call_uses_a_fresh_salt(self):
        self.ext.search("morning")
        self.ext.search("morning")
        self.assertNotEqual(FakeSubsonic.seen[0]["s"], FakeSubsonic.seen[1]["s"])

    def test_empty_query_makes_no_request(self):
        self.assertEqual(self.ext.search("  ")["results"], [])
        self.assertEqual(FakeSubsonic.seen, [])

    def test_wrong_password_is_a_readable_error_without_url(self):
        self.settings["password"] = "nope"
        r = self.ext.search("morning")
        self.assertIn("Wrong username or password", r["error"])
        self.assertNotIn("127.0.0.1", r["error"])

    def test_unreachable_server_error_has_no_url_or_token(self):
        self.settings["server"] = "http://127.0.0.1:1"
        r = self.ext.search("morning")
        self.assertIn("Cannot reach the server", r["error"])
        self.assertNotIn("127.0.0.1", r["error"])
        self.assertNotIn("t=", r["error"])

    def test_missing_settings(self):
        self.settings["password"] = ""
        self.assertIn("Fill in", self.ext.search("x")["error"])
        self.assertEqual(FakeSubsonic.seen, [])

    def test_stream_info_song_and_album(self):
        self.assertEqual(self.ext.stream_info("subsonic-song-42")["title"], "Morning Lines")
        a = self.ext.stream_info("subsonic-album-7")
        self.assertEqual((a["_type"], a["title"], a["trackCount"], len(a["results"])), ("playlist", "Demo", 2, 2))

    def test_playlist_meta_respects_max_items(self):
        m = self.ext.playlist_meta("subsonic-album-7", max_items=1)
        self.assertEqual((m["trackCount"], len(m["results"])), (2, 1))

    def test_foreign_url_is_refused(self):
        self.assertIn("error", self.ext.fetch_audio("https://example.com/x.mp3", self.tmp))
        self.assertEqual(FakeSubsonic.seen, [])

    def test_fetch_audio_writes_the_original_file(self):
        r = self.ext.fetch_audio("subsonic-song-42", self.tmp)
        self.assertNotIn("error", r)
        self.assertTrue(os.path.isabs(r["filePath"]))
        self.assertTrue(r["filePath"].endswith("The Trio - Morning Lines [42].flac"))
        with open(r["filePath"], "rb") as f:
            self.assertEqual(f.read(), AUDIO)
        self.assertEqual(os.listdir(self.tmp), ["The Trio - Morning Lines [42].flac"])   # no .part left
        self.assertTrue(all(q.get("f") == "json" for q in FakeSubsonic.seen))
        self.assertEqual(r["format"], "flac")

    def test_fetch_audio_falls_back_to_raw_stream_when_download_is_forbidden(self):
        FakeSubsonic.download_allowed = False
        r = self.ext.fetch_audio("subsonic-song-42", self.tmp)
        self.assertNotIn("error", r)
        self.assertEqual(FakeSubsonic.seen[-1].get("format"), "raw")

    def test_pasted_navidrome_playlist_link(self):
        m = self.ext.stream_info("https://music.example.org/app/#/playlist/p1/show")
        self.assertEqual((m["_type"], m["id"], m["title"], m["artist"], m["trackCount"]),
                         ("playlist", "subsonic-playlist-p1", "Road", "alice", 1))
        self.assertEqual(FakeSubsonic.seen[-1]["id"], "p1")

    def test_ids_have_no_colon_or_slash(self):
        t = self.ext.search("morning")["results"][0]
        self.assertFalse(set(t["sourceId"]) & set(":/\\"))

    def test_unknown_song(self):
        self.assertIn("Song not found", self.ext.fetch_audio("subsonic-song-999", self.tmp)["error"])
        self.assertEqual(os.listdir(self.tmp), [])

    def test_connection_button(self):
        self.assertEqual(self.ext.test_connection(self.settings)["message"]["en"], "Connected.")
        self.assertIn("Wrong", self.ext.test_connection(dict(self.settings, password="x"))["message"])

    def test_page_declares_the_button_action_and_a_masked_password(self):
        els = self.ext.pages()[0]["elements"]
        self.assertIn({"type": "secret", "cle": "password",
                       "libelle": {"en": "Password", "fr": "Mot de passe"}}, els)
        actions = [e["action"] for e in els if e["type"] == "bouton"]
        self.assertEqual(actions, ["test_connection"])
        self.assertTrue(callable(getattr(self.ext, actions[0])))


if __name__ == "__main__":
    unittest.main()
