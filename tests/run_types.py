import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import importlib.util, logging, tempfile, os
from datetime import datetime, timezone
from pathlib import Path

os.environ["LOCAL_TZ"] = "Asia/Kolkata"

spec = importlib.util.spec_from_file_location("k2g", str(ROOT / "kinomap_to_garmin.py"))
k2g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(k2g)
EVENTS = []
k2g.time.sleep = lambda s: EVENTS.append(("sleep", s))  # no real waiting in tests
logging.basicConfig(level=logging.INFO, format="  LOG %(levelname)s %(message)s")

TYPES = [
    {"typeId": 4, "typeKey": "other", "parentTypeId": 17},
    {"typeId": 15, "typeKey": "indoor_rowing", "parentTypeId": 29},
    {"typeId": 160, "typeKey": "elliptical", "parentTypeId": 29},
]


def tcx(d, sport, name="a.tcx", when="2026-11-02T05:00:00Z"):
    p = Path(d) / name
    p.write_text(f'<?xml version="1.0"?><TrainingCenterDatabase><Activities>'
                 f'<Activity Sport="{sport}"><Id>{when}</Id></Activity>'
                 f'</Activities></TrainingCenterDatabase>')
    return p


class FakeConflict(Exception):
    def __init__(self):
        super().__init__("API Error 409 - Duplicate Activity")
        self.response = type("R", (), {"status_code": 409})()


class FakeGarmin:
    def __init__(self, upload=None, types=TYPES, recent=None, conflict=False, status=None):
        self._upload, self.types, self.recent, self.conflict = upload, types, recent or [], conflict
        self.set_calls = []
        self.names = []
        self.status = list(status or [])
        self.status_paths = []
        self.search_calls = 0

    def connectapi(self, path):
        EVENTS.append(("call", path))
        self.status_paths.append(path)
        if not self.status:
            raise ConnectionError("status endpoint unavailable")
        item = self.status.pop(0)
        if isinstance(item, Exception):
            # what the real library does: log a traceback, then raise
            logging.getLogger("garminconnect.api.base").error("library-noise traceback")
            raise item
        return item

    def set_activity_name(self, activity_id, title):
        self.names.append((activity_id, title))

    def upload_activity(self, path):
        if self.conflict:
            raise FakeConflict()
        return self._upload

    def get_activity_types(self):
        return self.types

    def get_activities_by_date(self, first, last):
        self.window = (first, last)
        self.search_calls += 1
        return self.recent

    def set_activity_type(self, activity_id, type_id, type_key, parent_type_id):
        self.set_calls.append((activity_id, type_id, type_key, parent_type_id))


OK = {"detailedImportResult": {"uploadId": 1, "successes": [{"internalId": 987654321}], "failures": []}}


def flow(g, path):
    result, aid = k2g.upload_to_garmin(g, path)
    if result == "uploaded":
        k2g.apply_type_from_tcx(g, path, aid)
    return result


with tempfile.TemporaryDirectory() as d:
    print("1) rowing -> indoor_rowing")
    g = FakeGarmin(OK)
    assert flow(g, tcx(d, "rowing")) == "uploaded"
    assert g.set_calls == [(987654321, 15, "indoor_rowing", 29)], g.set_calls

    print("2) running (elliptical) -> elliptical")
    g = FakeGarmin(OK)
    flow(g, tcx(d, "running"))
    assert g.set_calls == [(987654321, 160, "elliptical", 29)], g.set_calls

    print("3) upload reply without id -> found by start time")
    recent = [{"activityId": 111, "startTimeGMT": "2026-11-01 05:00:00"},
              {"activityId": 222, "startTimeGMT": "2026-11-02 05:00:30"}]
    g = FakeGarmin({"detailedImportResult": {"successes": [], "failures": []}}, recent=recent)
    flow(g, tcx(d, "rowing"))
    assert g.set_calls == [(222, 15, "indoor_rowing", 29)], g.set_calls

    print("3b) OLD activity (2024), reply without id -> found via date window")
    old = [{"activityId": 555, "startTimeGMT": "2024-01-10 06:30:10"}]
    g = FakeGarmin({"detailedImportResult": {"successes": []}}, recent=old)
    flow(g, tcx(d, "running", when="2024-01-10T06:30:00.000Z"))
    assert g.window == ("2024-01-09", "2024-01-11"), g.window
    assert g.set_calls == [(555, 160, "elliptical", 29)], g.set_calls

    print("4) id not found anywhere -> warning, no crash, no change")
    g = FakeGarmin({}, recent=[])
    flow(g, tcx(d, "rowing"))
    assert g.set_calls == []

    print("5) unknown type key -> logged error listing available keys, upload still ok")
    g = FakeGarmin(OK, types=[{"typeId": 4, "typeKey": "other", "parentTypeId": 17}])
    assert flow(g, tcx(d, "rowing")) == "uploaded"
    assert g.set_calls == []

    print("6) duplicate (409) -> no retype")
    g = FakeGarmin(conflict=True)
    assert flow(g, tcx(d, "rowing")) == "duplicate"
    assert g.set_calls == []

    print("7) unmapped sport (Biking) -> untouched")
    g = FakeGarmin(OK)
    flow(g, tcx(d, "Biking"))
    assert g.set_calls == []

    print("8) sport matching is case-insensitive (Rowing / ROWING)")
    for s in ("Rowing", "ROWING"):
        g = FakeGarmin(OK)
        flow(g, tcx(d, s))
        assert g.set_calls and g.set_calls[0][2] == "indoor_rowing"

print("13) status API: the real replies from the browser")
REAL_UPLOAD = {"detailedImportResult": {
    "uploadId": 500890638398, "uploadUuid": {"uuid": "86fc8bb3-12c6-468b-8d8e-0f5ed58b96da"},
    "owner": 121211, "fileSize": 575887, "processingTime": 90,
    "creationDate": "2026-11-02 11:14:20.486 GMT", "ipAddress": "",
    "fileName": "b40f35d442091b6bb205db4861d009ea.tcx", "report": None,
    "successes": [], "failures": []}}
STATUS_PENDING = {"detailedImportResult": {"uploadId": 500890638398, "successes": [], "failures": []}}
STATUS_DONE = {"detailedImportResult": {"uploadId": 500890638398,
               "successes": [{"internalId": 24937796527, "externalId": None, "messages": None}],
               "failures": []}}
WANT_PATH = "/activity-service/activity/status/1793618060486/86fc8bb312c6468b8d8e0f5ed58b96da"
with tempfile.TemporaryDirectory() as d:
    g = FakeGarmin(REAL_UPLOAD, status=[STATUS_PENDING, STATUS_PENDING, STATUS_DONE])
    assert flow(g, tcx(d, "rowing")) == "uploaded"
    assert g.status_paths == [WANT_PATH] * 3, g.status_paths
    assert g.search_calls == 0, "must not fall back to searching when status works"
    assert g.set_calls == [(24937796527, 15, "indoor_rowing", 29)], g.set_calls
    print("   polled", len(g.status_paths), "times at", g.status_paths[0])

    print("13b) creationDate without fractional seconds")
    up = {"detailedImportResult": dict(REAL_UPLOAD["detailedImportResult"], creationDate="2026-11-02 11:14:20 GMT")}
    g = FakeGarmin(up, status=[STATUS_DONE])
    flow(g, tcx(d, "rowing"))
    assert g.status_paths == ["/activity-service/activity/status/1793618060000/86fc8bb312c6468b8d8e0f5ed58b96da"]

    print("13c) status endpoint errors out every time -> falls back to date search")
    recent = [{"activityId": 777, "startTimeGMT": "2026-11-02 05:00:10"}]
    g = FakeGarmin(REAL_UPLOAD, recent=recent)
    flow(g, tcx(d, "rowing"))
    assert len(g.status_paths) == 10 and g.search_calls >= 1
    assert g.set_calls == [(777, 15, "indoor_rowing", 29)], g.set_calls

    print("13d) status says never ready -> falls back to date search")
    g = FakeGarmin(REAL_UPLOAD, recent=recent, status=[STATUS_PENDING] * 10)
    flow(g, tcx(d, "rowing"))
    assert g.set_calls == [(777, 15, "indoor_rowing", 29)], g.set_calls

    print("13e) status reports duplicate (code 202) -> no retype, reported as duplicate")
    DUP = {"detailedImportResult": {"successes": [], "failures": [
        {"internalId": 24937796527, "messages": [{"code": 202, "content": "Duplicate Activity."}]}]}}
    g = FakeGarmin(REAL_UPLOAD, status=[DUP])
    assert flow(g, tcx(d, "rowing")) == "duplicate"
    assert g.set_calls == [] and g.names == []

    print("13f) status reports another failure -> raises so the mail is retried")
    BAD = {"detailedImportResult": {"successes": [], "failures": [
        {"messages": [{"code": 500, "content": "Unsupported file."}]}]}}
    g = FakeGarmin(REAL_UPLOAD, status=[BAD])
    try:
        flow(g, tcx(d, "rowing")); raise SystemExit("FAIL: expected an error")
    except RuntimeError as e:
        print("   ", e)
        assert "Unsupported file" in str(e)

    print("13g) upload reply that already has an id -> status never called")
    g = FakeGarmin(OK)
    flow(g, tcx(d, "rowing"))
    assert g.status_paths == []

print("14) first status check returns empty body: wait first, stay quiet, retry, succeed")


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


cap = Capture()
logging.getLogger().addHandler(cap)
with tempfile.TemporaryDirectory() as d:
    EVENTS.clear()
    empty_body = ValueError("Connection error: Expecting value: line 1 column 1 (char 0)")
    g = FakeGarmin(REAL_UPLOAD, status=[empty_body, STATUS_DONE])
    flow(g, tcx(d, "rowing"))
    first_call = next(i for i, e in enumerate(EVENTS) if e[0] == "call")
    assert EVENTS[0] == ("sleep", 3.0) and first_call == 1, EVENTS[:3]
    assert g.set_calls == [(24937796527, 15, "indoor_rowing", 29)]
    assert not any("library-noise" in m for m in cap.records), "library traceback leaked into our log"
    assert not any("no result yet" in m for m in cap.records), "early misses are DEBUG now, quiet at INFO"
    # logging level of the library is restored afterwards
    assert logging.getLogger("garminconnect").level == logging.NOTSET
    logging.getLogger("garminconnect.api.base").error("library-noise after")
    assert any("library-noise after" in m for m in cap.records), "library logging not restored"
    print("   waited", EVENTS[0][1], "s before the first check; noise suppressed; logging restored")
logging.getLogger().removeHandler(cap)

print("10) names use local time (Asia/Kolkata = UTC+5:30)")
utc = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
assert k2g.NAME_TIME_OF_DAY is False, "time of day must be off by default"
assert k2g.build_name("indoor_rowing", utc("2026-11-02T13:00:00")) == "Indoor Rowing"
assert k2g.build_name("elliptical", None) == "Elliptical"   # no start time needed when off
assert k2g.build_name("hiit_workout", None) == "Hiit Workout"
print("    default: Indoor Rowing / Elliptical / Hiit Workout (no time of day)")
k2g.NAME_TIME_OF_DAY = True
cases = [
    ("indoor_rowing", "2026-11-02T05:00:00", "Morning Indoor Rowing"),   # 10:30 IST
    ("elliptical",    "2026-11-02T08:00:00", "Afternoon Elliptical"),    # 13:30 IST
    ("indoor_rowing", "2026-11-02T13:00:00", "Evening Indoor Rowing"),   # 18:30 IST
    ("elliptical",    "2026-11-02T16:30:00", "Night Elliptical"),        # 22:00 IST
    ("indoor_rowing", "2026-11-02T23:30:00", "Morning Indoor Rowing"),   # 05:00 IST next day
    ("indoor_rowing", "2026-11-02T22:30:00", "Night Indoor Rowing"),     # 04:00 IST
    ("cycling",       "2026-11-02T05:00:00", "Morning Cycling"),         # unlisted key -> title case
]
for key, ts, want in cases:
    got = k2g.build_name(key, utc(ts))
    assert got == want, (key, ts, got, want)
    print("   ", ts, key, "->", got)

with tempfile.TemporaryDirectory() as d:
    print("11) end to end: renamed 'Morning Indoor Rowing' with time of day on, 'Indoor Rowing' off; no name when type fails")
    g = FakeGarmin(OK)
    flow(g, tcx(d, "rowing"))
    assert g.names == [(987654321, "Morning Indoor Rowing")], g.names
    k2g.NAME_TIME_OF_DAY = False
    g = FakeGarmin(OK)
    flow(g, tcx(d, "rowing"))
    assert g.names == [(987654321, "Indoor Rowing")], g.names
    g = FakeGarmin(OK)
    flow(g, tcx(d, "running"))
    assert g.names == [(987654321, "Elliptical")], g.names
    k2g.NAME_TIME_OF_DAY = True
    k2g.SET_NAME = False
    g = FakeGarmin(OK)
    flow(g, tcx(d, "rowing"))
    assert g.names == [] and g.set_calls, "SET_NAME=0 must still set the type"
    k2g.SET_NAME = True
    g = FakeGarmin(OK, types=[{"typeId": 4, "typeKey": "other", "parentTypeId": 17}])
    flow(g, tcx(d, "rowing"))
    assert g.names == []
    g = FakeGarmin(OK)
    flow(g, tcx(d, "Biking"))
    assert g.names == []

print("12) bad LOCAL_TZ gives a clear error")
old = k2g.LOCAL_TZ
k2g.LOCAL_TZ = "Mars/Olympus"
try:
    k2g.build_name("elliptical", utc("2026-11-02T05:00:00")); raise SystemExit("FAIL")
except RuntimeError as e:
    print("   ", e)
k2g.LOCAL_TZ = old

print("15) --list-types prints key, id and parent key")
import io as _io
buf = _io.StringIO()
assert k2g.list_activity_types(FakeGarmin(), out=buf) == 0
text = buf.getvalue()
assert "indoor_rowing" in text and "elliptical" in text and "typeKey" in text
rows = [l for l in text.splitlines() if l.startswith(("indoor_rowing", "elliptical", "other"))]
print("   " + "\n   ".join(rows))
assert any(l.split()[0] == "indoor_rowing" and l.split()[1] == "15" for l in rows)

print("9) SPORT_MAP parsing")
assert k2g.parse_sport_map("rowing:indoor_rowing, Running : elliptical,bad,:x") == {
    "rowing": "indoor_rowing", "running": "elliptical"}
assert k2g.parse_sport_map("") == {}
print("ALL OK")
