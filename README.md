# Subsonic server: example extension for Omega Player

Search and download the music stored on **your own server** from Omega Player, through
the Subsonic API. Navidrome, Airsonic-Advanced, Gonic, Ampache and other compatible servers
implement it.

This is an example extension: it shows what the
[extension SDK](https://mistano.github.io/omega-player/sdk/) can do in under 300 lines of
plain Python. Read it, fork it, adapt it.

## What it does

- **Search** your server's tracks from the app's search tab (`search3`).
- **Download** a track to your library. The original file comes down with its tags and
  embedded cover intact (`download`). If your account is not allowed to download, the
  extension falls back to the untranscoded stream (`stream?format=raw`).
- **Albums and playlists**: paste a Navidrome album or playlist link (`…/app/#/album/<id>/show`)
  and it opens as a collection (`getAlbum`, `getPlaylist`). Search returns tracks only.
- A **settings page** with the server address, user, password (a masked field) and a
  « Test the connection » button.

It does not play streams directly from the server. Tracks are downloaded first, then
played from your phone.

## Install

1. Build the zip from this folder. The two files go at the zip's root:

   ```
   python -m zipfile -c subsonic.zip manifest.json media_plugin.py
   ```

2. Copy `subsonic.zip` to your phone. In Omega Player, go to Settings › Extensions ›
   Import, then approve it.
3. Open the extension's page. Fill in your server address, user and password, then tap
   « Test the connection ».

## What it can access, and what it sends

Omega Player extensions are **not sandboxed**. An extension runs with the app's own
permissions: network, your music library, and the microphone if you granted it. Read the
code before you approve an extension. This file is short on purpose.

This extension:

- talks **only** to the server address you typed. No analytics, no other host;
- **never sends your password**. Each request carries `t = md5(password + salt)` with a
  fresh random salt: Subsonic token authentication, API 1.13 and later. With plain
  `http://`, someone on your network could still replay a token, so use `https://` outside
  your home network;
- never puts credentials in a URL it hands to the app. That is why search results have no
  thumbnail: a Subsonic cover URL carries the token, and the app stores the URLs it is
  given. Covers come from the downloaded file's tags instead;
- never logs anything, and its error messages never contain a URL.

Your settings, password included, are stored by the app in the extension's
`data/reglages.json`, inside the app's private storage.

## Try it without a server: Navidrome in Docker

```
docker run -d --name navidrome -p 4533:4533 \
  -v /path/to/some/music:/music:ro -v navidrome-data:/data \
  deluan/navidrome:latest
```

Open http://localhost:4533 and create the admin user. To reach it from an Android
emulator, the server address is `http://10.0.2.2:4533`. From a phone on the same Wi-Fi,
use the computer's local IP address.

## Tests

The tests run against a fake Subsonic server on 127.0.0.1 and need only Python and
`requests`:

```
python -m unittest test_subsonic
```

## Limits

- Error messages are in English only.
- Tracks are downloaded one at a time. An album or playlist is downloaded track by track.
- There is no `related()`. The app's own recommendations still work on downloaded tracks.

## License

MIT, see [LICENSE](LICENSE). This licence covers this extension only, not Omega Player.
