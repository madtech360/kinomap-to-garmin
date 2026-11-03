# kinomap-to-garmin

Get your [Kinomap](https://www.kinomap.com) workouts into Garmin Connect.

Kinomap has no Garmin integration, but it can **email** a finished activity as a ZIP (CSV, GPX, TCX and PWX).
This small self-hosted tool reads that email, pulls out the TCX file, uploads it to Garmin Connect, and then fixes
what a TCX upload gets wrong: the **activity type** (so a rowing session is *Indoor Rowing*, not *Other*) and the
**name**.

You can run it on a timer, or trigger it from a button in **Home Assistant**.

> **Unofficial.** Not affiliated with or endorsed by Kinomap or Garmin. It uses the community
> [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) library, which talks to Garmin's web
> services without an official API. Garmin can change that at any time, and automated logins may be against
> Garmin's terms. Use it for your own account, at your own risk.

## What it does

1. Looks in an IMAP mailbox (folder and sender are configurable) for unread Kinomap emails.
2. Extracts the `.tcx` file from the ZIP attachment.
3. Uploads it to Garmin Connect and waits for Garmin to finish processing it.
4. Sets the activity type from the TCX `Sport` value and renames the activity.
5. Marks the email as read, and remembers it so nothing is uploaded twice.

Default mapping (change it with `SPORT_MAP`):

| TCX `Sport` | Garmin type | Name |
|---|---|---|
| `rowing` | `indoor_rowing` | Indoor Rowing |
| `running` | `elliptical` | Elliptical |

The `running` → elliptical default exists because Kinomap labels elliptical sessions as running. **If you also use
Kinomap on a treadmill, change it** (for example `SPORT_MAP=rowing:indoor_rowing`) or every treadmill run will be
filed as an elliptical session.

## Quick start (Docker)

You need a Linux machine with Docker Compose, an IMAP mailbox that receives the Kinomap emails, and a Garmin account.

```bash
git clone https://github.com/madtech360/kinomap-to-garmin.git
cd kinomap-to-garmin
cp .env.example .env
chmod 600 .env
nano .env                       # fill in your mailbox and settings
docker compose build
```

**1. Log in to Garmin once** (handles MFA, stores tokens in the Docker volume, so no password is kept):

```bash
docker compose run --rm -it --entrypoint python k2g kinomap_to_garmin.py --login
```

**2. Dry run** (extracts the TCX and shows what it would do, without uploading or marking mail read):

```bash
docker compose run --rm --entrypoint python k2g kinomap_to_garmin.py --dry-run
```

**3. Start it:**

```bash
docker compose up -d
```

By default the container waits for a trigger (`MODE=trigger`). Press the button in Home Assistant, or call it by hand:

```bash
curl -X POST -H "Authorization: Bearer $TRIGGER_TOKEN" http://127.0.0.1:8765/run
# {"ok": true, "message": "Uploaded Elliptical", "trace_id": "3fa91c2e"}
```

Prefer a timer? Set `MODE=loop` and `INTERVAL=900` (seconds) in `.env` and it polls the mailbox by itself.

### Without Docker

```bash
pip install -r requirements.txt          # Python 3.12 or newer
export IMAP_HOST=... IMAP_USER=... IMAP_PASSWORD=...
python kinomap_to_garmin.py --login
python kinomap_to_garmin.py              # run once, e.g. from cron
python kinomap_to_garmin.py --list-types # show Garmin activity type keys for SPORT_MAP
```

## Home Assistant

[`homeassistant/kinomap.yaml`](homeassistant/kinomap.yaml) has a `rest_command`, a script and a dashboard button.
Home Assistant shows only the short result (`Uploaded Elliptical`, or `1 upload(s) failed (trace 3fa91c2e)`).

Set `TRIGGER_BIND` in `.env` to the server's LAN IP so Home Assistant can reach the port. The default `127.0.0.1`
is reachable only from the server itself. If Home Assistant runs in Docker on the same machine, use the machine's
LAN IP in the URL, not `localhost`.

## Configuration

All settings are environment variables, normally in `.env` (see [`.env.example`](.env.example)).

| Variable | Default | Purpose |
|---|---|---|
| `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` | required | Mailbox login. Gmail and Outlook need an **app password**. |
| `IMAP_FOLDER` | `INBOX` | Folder to read. Names are exact; a wrong one lists the folders that exist. |
| `KINOMAP_FROM`, `KINOMAP_SUBJECT` | none | Only use mail whose sender/subject contains this text. |
| `SPORT_MAP` | `rowing:indoor_rowing,running:elliptical` | TCX sport → Garmin type key. Empty leaves types alone. |
| `SET_NAME` | `1` | Rename the activity after setting the type. |
| `TYPE_LABELS` | `indoor_rowing:Indoor Rowing,elliptical:Elliptical` | Name per type. |
| `NAME_TIME_OF_DAY` | `0` | `1` gives names like `Morning Elliptical`. |
| `LOCAL_TZ` | `UTC` | Time zone for `NAME_TIME_OF_DAY` (TCX times are UTC), e.g. `Asia/Kolkata`. |
| `MODE` | `trigger` | `trigger` waits for a request, `loop` runs every `INTERVAL` seconds. |
| `TRIGGER_TOKEN` | required in trigger mode | Secret the caller sends. 16+ characters; the example placeholder is refused. |
| `TRIGGER_PORT`, `TRIGGER_BIND` | `8765`, `127.0.0.1` | Port and the address it is published on. |
| `RUN_TIMEOUT` | `300` | Seconds before one run is stopped. |
| `LOG_LEVEL` | `INFO` | `DEBUG` also logs raw Garmin replies and each status check. |

### Finding the right Garmin type key

`SPORT_MAP` needs Garmin's **type key** for the right-hand side. TCX only knows `Running`, `Biking` and `Other`
(Kinomap also sends values such as `rowing`), so you map whatever Kinomap writes to a Garmin key. To see every key
your Garmin account offers, after the one-time login:

```bash
docker compose run --rm --entrypoint python k2g kinomap_to_garmin.py --list-types
```

It prints a table of `typeKey`, `typeId` and parent type. Pick the key you want, for example `cycling`, and add it:

```
SPORT_MAP=rowing:indoor_rowing,running:elliptical,biking:cycling
```

Use the key, not the display name or the id. The TCX side is the `Sport="..."` value in the file from Kinomap
(not case-sensitive). To see what Kinomap writes for a sport, open one exported TCX and look at the `Activity` tag.
If a key is wrong, the log shows `Garmin has no activity type 'x'` and lists the valid ones.

## Logs and troubleshooting

Every run gets a short **trace id**, stamped on every log line, in both the container log and a rotating file
(`/data/logs/k2g.log`):

```bash
docker compose logs k2g | grep 3fa91c2e
docker compose exec k2g grep 3fa91c2e /data/logs/k2g.log
```

| Symptom | Likely cause |
|---|---|
| `Cannot open folder ...` | Wrong `IMAP_FOLDER`; the error lists the real folder names. |
| `[AUTHENTICATIONFAILED]` | Use an app password, and check IMAP is enabled for the mailbox. |
| Garmin login errors | Re-run the `--login` step; rebuild the image to get the newest `garminconnect`. |
| Type or name not applied | Upload still succeeds; the log says why. Check `SPORT_MAP` and the type key. |
| "already in Garmin" | Garmin rejected a duplicate. Delete the old activity to re-upload. |
| Settings changes ignored | Use `docker compose up -d --force-recreate`; `restart` keeps the old environment. |

## How the upload is confirmed

Garmin accepts the upload and replies with an upload id and UUID, but not the activity id. The tool then polls
`/activity-service/activity/status/<creation time in ms>/<uuid>`, the same call Garmin's web page makes, to get the
activity id, and falls back to matching the activity by start time if that call is unavailable.

## Development

```bash
pip install -r requirements.txt pytest
python -m pytest -q
```

The tests use fake IMAP and Garmin objects, so they need no mailbox, network or Garmin account. CI runs them on
Python 3.12 and 3.13 and builds the Docker image.

## Security notes

- `.env` holds your mailbox password. Keep it `chmod 600` and out of git (it is in `.gitignore`).
- Prefer not to store your Garmin password: leave `GARMIN_PASSWORD` out and type it at the `--login` prompt.
- The trigger endpoint is plain HTTP protected by a bearer token. Keep it on your home network, never forward the
  port on your router, and put it behind a reverse proxy with TLS if you need remote access.
- Logs never contain passwords or tokens, but they do contain email subjects and activity details.

## Limitations

- Tested with Kinomap rowing and elliptical sessions. Other sports upload fine, but you must add them to `SPORT_MAP`.
- TCX cannot carry rowing-specific data such as stroke rate, so Garmin shows the basics (time, distance, heart rate).
- One mailbox, one Garmin account.

## License

[MIT](LICENSE)
