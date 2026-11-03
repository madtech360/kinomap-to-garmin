#!/usr/bin/env python3
"""
Kinomap -> Garmin Connect uploader.

Reads Kinomap "share activity" emails from an IMAP mailbox, pulls the .tcx file
out of the attached ZIP (CSV/GPX/TCX/PWX), and uploads it to Garmin Connect.

Setup (Python 3.12+):
    pip install garminconnect curl_cffi

One-time Garmin login (handles MFA, caches tokens in ~/.garminconnect):
    python kinomap_to_garmin.py --login

Normal run (put it in cron / Task Scheduler, e.g. every 15 minutes):
    python kinomap_to_garmin.py

Test without uploading anything:
    python kinomap_to_garmin.py --dry-run

Show the Garmin activity type keys you can use in SPORT_MAP (needs the login above):
    python kinomap_to_garmin.py --list-types

Configuration is by environment variables:
    IMAP_HOST        e.g. imap.gmail.com
    IMAP_USER        your mailbox address
    IMAP_PASSWORD    app password (Gmail/Outlook need an app password, not your normal one)
    IMAP_FOLDER      optional, default INBOX
    KINOMAP_FROM     optional, only mails whose From contains this text (e.g. "kinomap")
    KINOMAP_SUBJECT  optional, only mails whose Subject contains this text
    GARMIN_EMAIL     only needed for --login
    GARMIN_PASSWORD  only needed for --login (otherwise you are prompted)
    GARMIN_TOKENS    optional, default ~/.garminconnect
    STATE_FILE       optional, default ~/.kinomap_to_garmin_state.json
    LOG_FILE         optional, rotating log file, default ~/.kinomap_to_garmin.log
    LOG_LEVEL        INFO (default) or DEBUG (adds raw Garmin replies and each status check)
    RUN_ID           optional, trace id stamped on every log line (random if not set)
    SPORT_MAP        optional, TCX Sport -> Garmin activity type key, default
                     "rowing:indoor_rowing,running:elliptical". Empty = don't change types.
    SET_NAME         1 (default) renames to e.g. "Elliptical"; 0 leaves Garmin's name
    TYPE_LABELS      optional, type key -> name,
                     default "indoor_rowing:Indoor Rowing,elliptical:Elliptical"
    NAME_TIME_OF_DAY 0 (default); 1 gives "Morning Elliptical", "Evening Indoor Rowing", ...
    LOCAL_TZ         time zone used when NAME_TIME_OF_DAY=1, e.g. Asia/Kolkata (default UTC)
"""

import argparse
import calendar
import contextlib
import email
import getpass
import imaplib
import io
import json
import logging
import os
import re
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from email.message import Message
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

log = logging.getLogger("kinomap2garmin")

TOKENS = os.path.expanduser(os.environ.get("GARMIN_TOKENS", "~/.garminconnect"))
STATE_FILE = Path(os.path.expanduser(os.environ.get("STATE_FILE", "~/.kinomap_to_garmin_state.json")))
LOG_FILE = os.environ.get("LOG_FILE", "~/.kinomap_to_garmin.log")

# Short id stamped on every log line of a run, so a failure can be found with:
#   docker compose logs k2g | grep <id>      or      grep <id> /data/logs/k2g.log
RUN_ID = os.environ.get("RUN_ID") or uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------- #
# Logging: full detail to stderr (container log) and a rotating file
# --------------------------------------------------------------------------- #
class _RunFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.run = RUN_ID
        return True


def setup_logging(verbose: bool = False) -> None:
    debug = verbose or os.environ.get("LOG_LEVEL", "INFO").strip().upper() == "DEBUG"
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(run)s] %(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        path = Path(os.path.expanduser(LOG_FILE))
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8"))
    except OSError as exc:
        print(f"warning: cannot write log file {LOG_FILE}: {exc}", file=sys.stderr)
    for handler in handlers:
        handler.setFormatter(fmt)
        handler.addFilter(_RunFilter())
        root.addHandler(handler)
    if not debug:  # keep HTTP client chatter out unless asked for
        for name in ("garminconnect", "urllib3", "requests", "curl_cffi"):
            logging.getLogger(name).setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# Run summary: one short line for Home Assistant
# --------------------------------------------------------------------------- #
STATS: dict = {}


def reset_stats() -> None:
    STATS.clear()
    STATS.update(uploaded=[], duplicates=0, failed=0, skipped=0, warnings=0, dry_run=0, fatal=None)


reset_stats()


def short_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:160]


def build_summary() -> dict:
    s = STATS
    if s["fatal"]:
        return {"run_id": RUN_ID, "ok": False, "message": f"Failed: {s['fatal']} (trace {RUN_ID})"}
    parts = []
    if s["uploaded"]:
        n = len(s["uploaded"])
        parts.append("Uploaded " + (s["uploaded"][0] if n == 1 else f"{n}: " + ", ".join(s["uploaded"])))
    if s["duplicates"]:
        parts.append(f"{s['duplicates']} already in Garmin")
    if s["skipped"]:
        parts.append(f"{s['skipped']} email(s) without a TCX file")
    if s["dry_run"]:
        parts.append(f"Dry run: {s['dry_run']} file(s) would be uploaded")
    if s["warnings"]:
        parts.append(f"{s['warnings']} type/name update(s) not applied")
    if s["failed"]:
        parts.append(f"{s['failed']} upload(s) failed")
    message = ". ".join(parts) or "No new Kinomap emails."
    ok = s["failed"] == 0
    if s["failed"] or s["warnings"]:
        message += f" (trace {RUN_ID})"
    return {"run_id": RUN_ID, "ok": ok, "message": message}


# --------------------------------------------------------------------------- #
# State (so a message is never uploaded twice)
# --------------------------------------------------------------------------- #
def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"done": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------- #
# Email / ZIP handling
# --------------------------------------------------------------------------- #
def extract_tcx_files(msg: Message, out_dir: Path) -> list[Path]:
    """Return paths of every .tcx found inside ZIP attachments (or attached directly)."""
    found: list[Path] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        filename = part.get_filename() or ""
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        lower = filename.lower()

        if lower.endswith(".tcx"):  # TCX attached directly
            target = out_dir / Path(filename).name
            target.write_bytes(payload)
            found.append(target)
            continue

        is_zip = lower.endswith(".zip") or part.get_content_type() in (
            "application/zip",
            "application/x-zip-compressed",
        )
        if not is_zip:
            continue
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as zf:
                for name in zf.namelist():
                    if name.lower().endswith(".tcx") and not name.endswith("/"):
                        # flatten path; guards against zip-slip
                        target = out_dir / Path(name).name
                        target.write_bytes(zf.read(name))
                        found.append(target)
        except zipfile.BadZipFile:
            log.warning("Attachment %r is not a valid ZIP, skipping", filename)
    return found


def select_folder(conn: imaplib.IMAP4_SSL, folder: str) -> None:
    """Open a folder, failing loudly (and listing real folder names) if it doesn't exist."""
    name = f'"{folder}"' if " " in folder and not folder.startswith('"') else folder
    status, data = conn.select(name)
    if status != "OK":
        try:
            listing = [line.decode(errors="replace") for line in conn.list()[1] if line]
        except Exception:
            listing = ["(could not list folders)"]
        raise RuntimeError(
            f"Cannot open folder {folder!r}: {data}. Folders on this server:\n  " + "\n  ".join(listing)
        )
    log.info("Opened folder %r (%s messages)", folder, data[0].decode() if data and data[0] else "?")


def fetch_candidates(conn: imaplib.IMAP4_SSL, args) -> list[bytes]:
    # UNDELETED: some servers only flag deleted mail (\Deleted) until it is expunged,
    # and a plain UNSEEN search would still return it.
    criteria = ["UNSEEN", "UNDELETED"]
    if args.sender:
        criteria += ["FROM", f'"{args.sender}"']
    if args.subject:
        criteria += ["SUBJECT", f'"{args.subject}"']
    status, data = conn.search(None, *criteria)
    if status != "OK":
        raise RuntimeError(f"IMAP search failed: {status} {data}")
    return data[0].split()


# --------------------------------------------------------------------------- #
# Garmin
# --------------------------------------------------------------------------- #
def garmin_login_interactive() -> None:
    from garminconnect import Garmin

    email_addr = os.environ.get("GARMIN_EMAIL") or input("Garmin email: ")
    password = os.environ.get("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")
    g = Garmin(email_addr, password, prompt_mfa=lambda: input("MFA code: ").strip())
    g.login(TOKENS)
    log.info("Logged in, tokens cached in %s", TOKENS)


def list_activity_types(g, out=None) -> int:
    """Print every activity type Garmin offers for this account: id, key, parent key.
    The key column is what goes into SPORT_MAP, e.g. SPORT_MAP="biking:cycling"."""
    out = out or sys.stdout
    types = g.get_activity_types()
    if isinstance(types, dict):
        types = next((v for v in types.values() if isinstance(v, list)), [])
    by_id = {t.get("typeId"): t.get("typeKey") for t in types}
    rows = sorted(((t.get("typeKey") or "", t.get("typeId"), by_id.get(t.get("parentTypeId")) or "")
                   for t in types if t.get("typeKey")), key=lambda r: (r[2], r[0]))
    print(f"{'typeKey (use in SPORT_MAP)':<34}{'typeId':>7}  parent", file=out)
    for key, tid, parent in rows:
        print(f"{key:<34}{tid:>7}  {parent}", file=out)
    print(f"\n{len(rows)} types. Example: SPORT_MAP=rowing:indoor_rowing,biking:cycling", file=out)
    return 0


def garmin_client():
    from garminconnect import Garmin

    g = Garmin()
    g.login(TOKENS)  # uses cached tokens; run --login first if this fails
    return g


def parse_sport_map(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in text.split(","):
        if ":" in pair:
            key, value = pair.split(":", 1)
            if key.strip() and value.strip():
                out[key.strip().lower()] = value.strip()
    return out


# TCX Sport value (lower case) -> Garmin activity type key.
# Override with SPORT_MAP="rowing:indoor_rowing,running:elliptical"; set it empty to disable.
SPORT_MAP = parse_sport_map(os.environ.get("SPORT_MAP", "rowing:indoor_rowing,running:elliptical"))


# Rename the activity after setting its type, e.g. "Indoor Rowing" or "Elliptical".
# SET_NAME=0 turns renaming off. NAME_TIME_OF_DAY=1 adds "Morning/Afternoon/Evening/Night" in
# front, worked out from the TCX start time (UTC) converted to LOCAL_TZ.
LOCAL_TZ = os.environ.get("LOCAL_TZ", "UTC")
SET_NAME = os.environ.get("SET_NAME", "1").strip().lower() not in ("0", "false", "no", "off", "")
NAME_TIME_OF_DAY = os.environ.get("NAME_TIME_OF_DAY", "0").strip().lower() in ("1", "true", "yes", "on")
# Name for each Garmin type key; unlisted keys become e.g. "Hiit Workout"
TYPE_LABELS = parse_sport_map(
    os.environ.get("TYPE_LABELS", "indoor_rowing:Indoor Rowing,elliptical:Elliptical")
)


def part_of_day(hour: int) -> str:
    if 5 <= hour < 12:
        return "Morning"
    if 12 <= hour < 17:
        return "Afternoon"
    if 17 <= hour < 21:
        return "Evening"
    return "Night"


def build_name(type_key: str, start: datetime | None) -> str:
    label = TYPE_LABELS.get(type_key.lower()) or type_key.replace("_", " ").title()
    if not NAME_TIME_OF_DAY:
        return label
    if start is None:
        raise RuntimeError("TCX has no start time, cannot work out the time of day")
    try:
        local = start.astimezone(ZoneInfo(LOCAL_TZ))
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Unknown LOCAL_TZ {LOCAL_TZ!r} (use e.g. Asia/Kolkata)") from exc
    return f"{part_of_day(local.hour)} {label}"


def read_tcx_meta(tcx: Path) -> tuple[str | None, datetime | None]:
    """Sport attribute and start time of the first <Activity>, read without an XML parser."""
    head = tcx.read_bytes()[:8192].decode("utf-8", errors="replace")
    m = re.search(r'<Activity\b[^>]*\bSport\s*=\s*"([^"]*)"', head, re.I)
    sport = m.group(1).strip() if m else None
    start = None
    t = re.search(r"<Id>\s*([^<\s]+)\s*</Id>", head)
    if t:
        try:
            start = datetime.fromisoformat(t.group(1).replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return sport, start


def activity_id_from_response(resp) -> int | None:
    try:
        successes = resp["detailedImportResult"]["successes"]
        return int(successes[0]["internalId"]) if successes else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _creation_millis(text: str) -> int:
    """'2026-11-02 11:14:20.486 GMT' -> epoch milliseconds (what Garmin's status URL expects)."""
    for fmt in ("%Y-%m-%d %H:%M:%S.%f GMT", "%Y-%m-%d %H:%M:%S GMT"):
        try:
            d = datetime.strptime(text, fmt)
            return calendar.timegm(d.timetuple()) * 1000 + d.microsecond // 1000
        except ValueError:
            continue
    raise ValueError(f"unrecognised creationDate {text!r}")


def _failure_text(failures: list) -> str:
    parts = []
    for f in failures:
        for m in (f.get("messages") or []):
            parts.append(f"{m.get('code')}: {m.get('content')}")
    return "; ".join(parts) or json.dumps(failures, default=str)[:500]


@contextlib.contextmanager
def quiet_library_logs():
    """While Garmin is still processing, an early status check returns an empty body and the
    library logs a scary traceback for it. That is expected, so keep it out of our log."""
    lib = logging.getLogger("garminconnect")
    previous = lib.level
    lib.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        lib.setLevel(previous)


def poll_upload_status(g, resp, tries: int = 10, delay: float = 2.0,
                       first_delay: float = 3.0) -> tuple[str, int | None]:
    """Ask Garmin how the upload went, the same call the web page makes.

    GET /activity-service/activity/status/<creationDate in ms>/<uploadUuid without hyphens>
    Returns (outcome, activity_id): outcome is 'ok', 'duplicate', 'failed' or 'unknown'.
    Garmin needs a moment to process the file, so wait first_delay seconds before checking."""
    try:
        result = resp["detailedImportResult"]
        uuid = result["uploadUuid"]["uuid"].replace("-", "")
        millis = _creation_millis(result["creationDate"])
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        log.info("Upload reply has no usable uploadUuid/creationDate (%s), cannot poll status", exc)
        return "unknown", None
    path = f"/activity-service/activity/status/{millis}/{uuid}"
    time.sleep(first_delay)
    for attempt in range(1, tries + 1):
        try:
            with quiet_library_logs():
                status = g.connectapi(path)
        except Exception as exc:
            log.debug("Status check %d/%d: no result yet (%s)", attempt, tries, exc)
            time.sleep(delay)
            continue
        detail = (status or {}).get("detailedImportResult") or {}
        successes = detail.get("successes") or []
        failures = detail.get("failures") or []
        log.debug("Status check %d/%d: %d success(es), %d failure(s)", attempt, tries,
                  len(successes), len(failures))
        if successes:
            return "ok", int(successes[0]["internalId"])
        if failures:
            text = _failure_text(failures)
            if "duplicate" in text.lower() or any(
                (m or {}).get("code") == 202 for f in failures for m in (f.get("messages") or [])
            ):
                return "duplicate", None
            raise RuntimeError(f"Garmin rejected the upload: {text}")
        time.sleep(delay)
    log.warning("Garmin did not report a result after %d status checks", tries)
    return "unknown", None


def find_recent_activity(g, start: datetime | None, tries: int = 5) -> int | None:
    """Fallback when the upload reply has no activity id: match by start time.

    Searches a +/- 1 day window around the TCX start (so old activities work too)."""
    if start is None:
        log.warning("No start time found in the TCX, cannot look the activity up")
        return None
    day = timedelta(days=1)
    first, last = (start - day).strftime("%Y-%m-%d"), (start + day).strftime("%Y-%m-%d")
    closest = None
    for attempt in range(1, tries + 1):
        acts = g.get_activities_by_date(first, last)
        log.debug("Lookup %d/%d: %d activities between %s and %s, looking for start %s",
                  attempt, tries, len(acts), first, last, start.isoformat())
        for act in acts:
            try:
                t = datetime.strptime(act["startTimeGMT"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except (KeyError, TypeError, ValueError):
                continue
            diff = abs((t - start).total_seconds())
            closest = diff if closest is None else min(closest, diff)
            if diff <= 120:
                return int(act["activityId"])
        time.sleep(3)
    log.warning("No activity within 120s of the TCX start time (closest was %s seconds away)",
                "n/a" if closest is None else int(closest))
    return None


def set_garmin_type(g, activity_id: int, type_key: str) -> None:
    types = g.get_activity_types()
    if isinstance(types, dict):  # tolerate a wrapped list
        types = next((v for v in types.values() if isinstance(v, list)), [])
    match = next((t for t in types if t.get("typeKey") == type_key), None)
    if match is None:
        keys = sorted(str(t.get("typeKey")) for t in types if t.get("typeKey"))
        raise RuntimeError(f"Garmin has no activity type {type_key!r}. Available keys: {', '.join(keys)}")
    if match.get("typeId") is None or match.get("parentTypeId") is None:
        raise RuntimeError(f"Type entry for {type_key!r} lacks typeId/parentTypeId: {match}")
    g.set_activity_type(activity_id, match["typeId"], match["typeKey"], match["parentTypeId"])


def upload_to_garmin(g, tcx: Path) -> tuple[str, int | None]:
    """Returns ('uploaded', activity_id or None) or ('duplicate', None). Raises on other failures."""
    try:
        resp = g.upload_activity(str(tcx))
    except Exception as exc:  # library raises its own error types
        status = getattr(getattr(exc, "response", None), "status_code", None)
        text = str(exc).lower()
        if status == 409 or "409" in text or "duplicate" in text:
            return "duplicate", None
        raise
    try:
        shown = json.dumps(resp, default=str)
    except (TypeError, ValueError):
        shown = repr(resp)
    log.debug("%s: Garmin upload response: %s", tcx.name, shown[:3000])
    activity_id = activity_id_from_response(resp)
    if activity_id is None:
        outcome, activity_id = poll_upload_status(g, resp)
        if outcome == "duplicate":
            return "duplicate", None
    log.info("%s: uploaded, Garmin activity id %s", tcx.name, activity_id)
    return "uploaded", activity_id


def apply_type_from_tcx(g, tcx: Path, activity_id: int | None) -> str | None:
    """Best effort: a failure here is logged and counted as a warning, but never makes the
    upload count as failed. Returns the activity's new name (or type) for the summary."""
    sport, start = read_tcx_meta(tcx)
    type_key = SPORT_MAP.get((sport or "").lower())
    if not type_key:
        log.info("%s: TCX sport %r has no mapping, leaving Garmin's default type", tcx.name, sport)
        return None
    try:
        if activity_id is None:
            activity_id = find_recent_activity(g, start)
        if activity_id is None:
            log.warning("%s: could not find the uploaded activity, set type %r by hand", tcx.name, type_key)
            STATS["warnings"] += 1
            return None
        set_garmin_type(g, activity_id, type_key)
        log.info("%s: activity %s set to type %r (TCX sport %r)", tcx.name, activity_id, type_key, sport)
    except Exception:
        log.exception("%s: uploaded, but setting the type to %r failed", tcx.name, type_key)
        STATS["warnings"] += 1
        return None
    label = TYPE_LABELS.get(type_key.lower()) or type_key.replace("_", " ").title()
    if SET_NAME:
        try:
            name = build_name(type_key, start)
            g.set_activity_name(activity_id, name)
            log.info("%s: activity %s renamed to %r", tcx.name, activity_id, name)
            return name
        except Exception:
            log.exception("%s: type was set, but renaming the activity failed", tcx.name)
            STATS["warnings"] += 1
    return label


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def run(args) -> int:
    reset_stats()
    host, user, pw = (os.environ.get(k) for k in ("IMAP_HOST", "IMAP_USER", "IMAP_PASSWORD"))
    if not (host and user and pw):
        log.error("Set IMAP_HOST, IMAP_USER and IMAP_PASSWORD")
        STATS["fatal"] = "IMAP_HOST, IMAP_USER and IMAP_PASSWORD must be set"
        return 2

    state = load_state()
    done = set(state["done"])
    g = None if args.dry_run else garmin_client()
    failures = 0

    log.info("Settings: host=%s folder=%r sender=%r subject=%r",
             host, args.folder, args.sender, args.subject)
    conn = imaplib.IMAP4_SSL(host)
    conn.login(user, pw)
    select_folder(conn, args.folder)
    try:
        candidates = fetch_candidates(conn, args)
        log.info("%d unread, non-deleted message(s) match", len(candidates))
        for num in candidates:
            # BODY.PEEK so the message stays unread until we succeed
            status, data = conn.fetch(num, "(FLAGS BODY.PEEK[])")
            if status != "OK" or not data or not data[0]:
                continue
            envelope = data[0][0]
            if b"\\Deleted" in envelope:  # belt and braces: never touch deleted mail
                log.info("Skipping message %s: flagged \\Deleted", num.decode())
                continue
            msg = email.message_from_bytes(data[0][1])
            log.info("Candidate %s: subject=%r date=%r from=%r flags=%s", num.decode(),
                     msg.get("Subject"), msg.get("Date"), msg.get("From"),
                     envelope.decode(errors="replace").split("FLAGS", 1)[-1].split(")")[0] + ")")
            msg_id = (msg.get("Message-ID") or f"imap-{num.decode()}").strip()
            if msg_id in done:
                conn.store(num, "+FLAGS", "\\Seen")
                continue

            with tempfile.TemporaryDirectory() as tmp:
                tcx_files = extract_tcx_files(msg, Path(tmp))
                if not tcx_files:
                    log.info("No TCX in %r, leaving untouched", msg.get("Subject"))
                    STATS["skipped"] += 1
                    continue
                ok = True
                for tcx in tcx_files:
                    if args.dry_run:
                        log.info("[dry-run] would upload %s (%d bytes) from %r",
                                 tcx.name, tcx.stat().st_size, msg.get("Subject"))
                        STATS["dry_run"] += 1
                        ok = False  # don't mark as processed in dry-run
                        continue
                    try:
                        result, activity_id = upload_to_garmin(g, tcx)
                        if result == "uploaded":
                            label = apply_type_from_tcx(g, tcx, activity_id)
                            STATS["uploaded"].append(label or "activity")
                        else:
                            log.info("%s: already in Garmin, nothing to do", tcx.name)
                            STATS["duplicates"] += 1
                    except Exception:
                        log.exception("Upload failed for %s", tcx.name)
                        STATS["failed"] += 1
                        ok = False
                        failures += 1
                if ok:
                    done.add(msg_id)
                    state["done"] = sorted(done)
                    save_state(state)
                    conn.store(num, "+FLAGS", "\\Seen")
    finally:
        # No conn.close(): CLOSE would permanently expunge any mail other clients
        # have flagged \Deleted in this folder. logout() leaves the mailbox untouched.
        conn.logout()
    return 1 if failures else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--login", action="store_true", help="one-time interactive Garmin login (MFA)")
    p.add_argument("--list-types", action="store_true",
                   help="print the Garmin activity type keys available to use in SPORT_MAP, then exit")
    p.add_argument("--dry-run", action="store_true", help="extract TCX but do not upload or mark mail read")
    p.add_argument("--folder", default=os.environ.get("IMAP_FOLDER", "INBOX"))
    p.add_argument("--sender", default=os.environ.get("KINOMAP_FROM"))
    p.add_argument("--subject", default=os.environ.get("KINOMAP_SUBJECT"))
    p.add_argument("-v", "--verbose", action="store_true", help="also log raw Garmin replies (DEBUG)")
    p.add_argument("--summary-file", help="write a one-line JSON result here (used by trigger_server.py)")
    args = p.parse_args()
    setup_logging(args.verbose)
    if args.login:
        garmin_login_interactive()
        return 0
    if args.list_types:
        return list_activity_types(garmin_client())
    try:
        rc = run(args)
    except Exception as exc:
        log.exception("Run failed")
        STATS["fatal"] = short_error(exc)
        rc = 1
    summary = build_summary()
    log.info("Result: %s", summary["message"])
    if args.summary_file:
        try:
            Path(args.summary_file).write_text(json.dumps(summary))
        except OSError:
            log.exception("Could not write summary file %s", args.summary_file)
    return rc


if __name__ == "__main__":
    sys.exit(main())
