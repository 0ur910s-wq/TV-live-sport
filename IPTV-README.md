# IPTV Player

This directory contains the IPTV Player application files imported from the supplied archive.

## Included

- `server.py` — Flask server and API
- `index.html` — player interface
- `edit.html` — channel/editor interface
- `space.js` — optional Node-based stream scanner
- Channel, playlist and category metadata JSON files
- Windows start/stop helper scripts

## Deliberately not included

The following archive files were not committed because they contain machine-specific, generated, or private state:

- `settings.json` — contains local paths and authentication hashes
- `recordings.json` — local recording state
- `epg.xml` — generated 32 MB EPG cache; the server can refresh it from its configured source
- `start_server_hidden.vbs.lnk` — machine-specific Windows shortcut

Before running locally, create a local `settings.json` from your own configuration and install the Python dependencies used by `server.py`.
