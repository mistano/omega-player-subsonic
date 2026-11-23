# =============================================================================
#  Subsonic source - example extension for Omega Player
#
#  Searches and downloads the music on YOUR OWN server, through the Subsonic
#  API that Navidrome, Airsonic-Advanced, Gonic, Ampache and others implement.
#  Nothing is contacted until you fill in the settings page.
#
#  Security choices, all deliberate:
#    - the password is never sent: every call carries t = md5(password + salt)
#      with a fresh random salt (Subsonic token auth, API 1.13+);
#    - no URL handed to the app ever contains credentials (thumbnails are left
#      empty for that reason: the app stores URLs). Covers come from the tags
#      of the downloaded file instead;
#    - error messages never include a request URL, and nothing is logged.
#
#  Python 3.11, only dependency: requests (bundled with the app).
# =============================================================================

import hashlib
import os
import re
import secrets
from xml.etree import ElementTree

import requests

API_VERSION = "1.16.1"
CLIENT = "OmegaPlayer"
TIMEOUT = (10, 30)          # connect, read (seconds)
CHUNK = 64 * 1024

_PREFIX = "subsonic-"       # subsonic-song-<id>, subsonic-album-<id>, subsonic-playlist-<id>
                            # no colon or slash: the app uses ids in file names


class SubsonicError(Exception):
    """A readable failure. Its message never contains a URL or a credential.
    `code` is the Subsonic error code when the server sent one."""
    code = None


# ── Settings ─────────────────────────────────────────────────────────────────

def _settings(values=None):
    """(server, user, password) from the settings page. `values` is the dict an
    action receives; otherwise read through the injected reglage()."""
    get = values.get if values is not None else reglage
    server = (get("server", "") or "").strip().rstrip("/")
    user = (get("user", "") or "").strip()
    password = get("password", "") or ""
    if not server or not user or not password:
        raise SubsonicError("Fill in the server address, user and password in this extension's page.")
    if not re.match(r"^https?://", server, re.I):
        raise SubsonicError("The server address must start with https:// (or http:// on your home network).")
    return server, user, password


def _auth(user, password):
    salt = secrets.token_hex(8)
    token = hashlib.md5((password + salt).encode("utf-8")).hexdigest()
    return {"u": user, "t": token, "s": salt, "v": API_VERSION, "c": CLIENT}


# ── HTTP ─────────────────────────────────────────────────────────────────────

def _get(endpoint, settings=None, stream=False, **params):
    server, user, password = settings or _settings()
    query = _auth(user, password)
    query.update(params)
    query["f"] = "json"       # binary endpoints use it for their error answers
    try:
        r = requests.get("%s/rest/%s.view" % (server, endpoint), params=query,
                         timeout=TIMEOUT, stream=stream)
    except requests.exceptions.SSLError:
        raise SubsonicError("The server's HTTPS certificate was refused.") from None
    except requests.exceptions.Timeout:
        raise SubsonicError("The server did not answer in time.") from None
    except requests.exceptions.RequestException as e:
        # str(e) may quote the URL, token included: only the type goes out.
        raise SubsonicError("Cannot reach the server (%s)." % type(e).__name__) from None
    if r.status_code in (401, 403):
        r.close()
        raise SubsonicError("The server refused the login (HTTP %d)." % r.status_code)
    if r.status_code != 200:
        r.close()
        raise SubsonicError("The server answered HTTP %d." % r.status_code)
    if stream and not _is_api_answer(r):
        return r                                  # binary audio, caller reads it
    try:
        body = r.json().get("subsonic-response", {})
    except ValueError:
        body = _xml_answer(r.text)
    finally:
        r.close()
    if body.get("status") != "ok":
        err = body.get("error") or {}
        e = SubsonicError("Server error %s: %s" % (err.get("code", "?"), err.get("message", "unknown")))
        e.code = err.get("code")
        raise e
    return body


def _xml_answer(text):
    """Error answer of a server that ignores f=json:
    <subsonic-response status="failed"><error code="50" message="..."/></subsonic-response>"""
    try:
        root = ElementTree.fromstring(text or "")
    except ElementTree.ParseError:
        root = None
    if root is None or not root.tag.endswith("subsonic-response"):
        raise SubsonicError("The server did not answer like a Subsonic server.")
    err = next((el for el in root if el.tag.endswith("error")), None)
    if err is None:
        return {"status": root.get("status")}
    code = err.get("code")
    return {"status": root.get("status"),
            "error": {"code": int(code) if (code or "").isdigit() else code,
                      "message": err.get("message") or "unknown"}}


def _is_api_answer(r):
    """A binary endpoint answers JSON (or XML, on servers that ignore f) only to report an error."""
    ctype = (r.headers.get("Content-Type") or "").lower()
    return "json" in ctype or "xml" in ctype


# ── Mapping ──────────────────────────────────────────────────────────────────

def _track(song):
    duration_s = int(song.get("duration") or 0)
    minutes, seconds = divmod(duration_s, 60)
    sid = str(song.get("id", ""))
    album = song.get("album") or ""
    return {
        "sourceId": _PREFIX + "song-" + sid,
        "title": song.get("title") or "",
        "artist": song.get("artist") or "",
        "durationMs": duration_s * 1000,
        "duration": "%d:%02d" % (minutes, seconds) if duration_s else "",
        "thumbnailUrl": "",                       # a cover URL would carry the token
        "url": _PREFIX + "song-" + sid,
        "description": album,
    }


def _parse(url_or_id):
    """('song' | 'album' | 'playlist', id), from our own ids or a pasted Navidrome
    web link (https://server/app/#/album/<id>/show, #/playlist/<id>/show)."""
    s = (url_or_id or "").strip()
    m = (re.match(r"^subsonic-(song|album|playlist)-(.+)$", s)
         or re.search(r"#/(album|playlist)/([^/?#]+)", s))
    if not m:
        raise SubsonicError("Not a Subsonic item: use this extension's search, or paste a Navidrome album "
                            "or playlist link.")
    return m.group(1), m.group(2)


def _safe_name(text):
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", text).strip(" .")
    return name[:120] or "track"


# ── Search source ────────────────────────────────────────────────────────────

def search(query, max_results=20, offset=0):
    q = (query or "").strip()
    if not q:
        return {"results": [], "offset": offset, "total_fetched": 0}
    try:
        body = _get("search3", query=q, songCount=int(max_results), songOffset=int(offset),
                    artistCount=0, albumCount=0)
    except SubsonicError as e:
        return {"error": str(e)}
    songs = (body.get("searchResult3") or {}).get("song") or []
    results = [_track(s) for s in songs]
    return {"results": results, "offset": offset, "total_fetched": len(results)}


def stream_info(url):
    try:
        kind, item = _parse(url)
        if kind != "song":
            meta = _collection(kind, item, 200)
            meta["_type"] = "playlist"
            return meta
        return _track(_get("getSong", id=item).get("song") or {})
    except SubsonicError as e:
        return {"error": str(e)}


def playlist_meta(url, max_items=200):
    try:
        kind, item = _parse(url)
        if kind == "song":
            raise SubsonicError("This is a single track, not an album or a playlist.")
        return _collection(kind, item, max_items)
    except SubsonicError as e:
        return {"error": str(e)}


def _collection(kind, item, max_items):
    if kind == "album":
        node = _get("getAlbum", id=item).get("album") or {}
        songs, title, artist = node.get("song") or [], node.get("name") or "", node.get("artist") or ""
    else:
        node = _get("getPlaylist", id=item).get("playlist") or {}
        songs, title, artist = node.get("entry") or [], node.get("name") or "", node.get("owner") or ""
    tracks = [_track(s) for s in songs[:int(max_items)]]
    return {"id": _PREFIX + kind + "-" + item, "title": title, "artist": artist,
            "trackCount": len(songs), "results": tracks}


def fetch_audio(url, out_dir):
    """Original file (tags and embedded cover kept) via download; if the account
    may not download, the untranscoded stream instead."""
    try:
        kind, item = _parse(url)
        if kind != "song":
            raise SubsonicError("Download albums and playlists track by track.")
        settings = _settings()
        song = _get("getSong", settings, id=item).get("song") or {}
        try:
            r = _get("download", settings, stream=True, id=item)
        except SubsonicError as e:
            if getattr(e, "code", None) != 50:    # 50 = not authorised for this action
                raise
            r = _get("stream", settings, stream=True, id=item, format="raw")
        if not hasattr(r, "iter_content"):
            raise SubsonicError("The server sent no audio for this track.")
        suffix = re.sub(r"[^A-Za-z0-9]", "", song.get("suffix") or "") or "mp3"
        # The id keeps two tracks with the same artist and title from overwriting each other.
        base = _safe_name("%s - %s [%s]" % (song.get("artist") or "", song.get("title") or "", item))
        final = os.path.join(out_dir, base + "." + suffix)
        part = final + ".part"
        try:
            with r, open(part, "wb") as f:
                for chunk in r.iter_content(CHUNK):
                    f.write(chunk)
                f.flush()
                os.fsync(f.fileno())
            os.replace(part, final)
        except requests.exceptions.RequestException as e:
            raise SubsonicError("Download interrupted (%s)." % type(e).__name__) from None
        finally:
            if os.path.exists(part):
                os.remove(part)
        t = _track(song)
        return {"filePath": os.path.abspath(final), "sourceId": t["sourceId"], "title": t["title"],
                "artist": t["artist"], "durationMs": t["durationMs"], "description": t["description"],
                "format": suffix}
    except SubsonicError as e:
        return {"error": str(e)}


# ── Settings page ────────────────────────────────────────────────────────────

def pages():
    return [{
        "id": "settings",
        "titre": {"en": "Your Subsonic server", "fr": "Votre serveur Subsonic"},
        "elements": [
            {"type": "message",
             "texte": {"en": "Works with Navidrome, Airsonic-Advanced, Gonic and other servers that speak "
                             "the Subsonic API. Your password is never sent: each request carries a salted hash.",
                       "fr": "Fonctionne avec Navidrome, Airsonic-Advanced, Gonic et les autres serveurs "
                             "compatibles avec l'API Subsonic. Le mot de passe n'est jamais envoyé : chaque "
                             "requête porte une empreinte salée."}},
            {"type": "texte", "cle": "server",
             "libelle": {"en": "Server address", "fr": "Adresse du serveur"},
             "aide": {"en": "For example https://music.example.org", "fr": "Par exemple https://musique.exemple.org"}},
            {"type": "texte", "cle": "user", "libelle": {"en": "User", "fr": "Utilisateur"}},
            {"type": "secret", "cle": "password", "libelle": {"en": "Password", "fr": "Mot de passe"}},
            {"type": "bouton", "action": "test_connection",
             "libelle": {"en": "Test the connection", "fr": "Tester la connexion"}},
        ],
    }]


def test_connection(reglages):
    try:
        _get("ping", _settings(reglages))
    except SubsonicError as e:
        return {"message": str(e)}
    return {"message": {"en": "Connected.", "fr": "Connexion réussie."}}
