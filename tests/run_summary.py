import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
"""End-to-end: summary line, log file with trace id, quiet INFO log, fatal errors."""
import importlib.util, io, json, logging, os, sys, tempfile, zipfile
from email.message import EmailMessage
from pathlib import Path

tmpdir = tempfile.mkdtemp()
os.environ.update(
    IMAP_HOST="h", IMAP_USER="u", IMAP_PASSWORD="secret-pass-123", RUN_ID="abc12345",
    LOG_FILE=f"{tmpdir}/logs/k2g.log", STATE_FILE=f"{tmpdir}/state.json",
    LOCAL_TZ="Asia/Kolkata", KINOMAP_FROM="kinomap",
)
spec = importlib.util.spec_from_file_location("k2g", str(ROOT / "kinomap_to_garmin.py"))
k2g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(k2g)
k2g.time.sleep = lambda s: None


def mail(sport, msgid, with_tcx=True):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.csv", "x")
        if with_tcx:
            zf.writestr("a.tcx", f'<TrainingCenterDatabase><Activities><Activity Sport="{sport}">'
                                 f'<Id>2026-11-02T05:00:00Z</Id></Activity></Activities></TrainingCenterDatabase>')
    m = EmailMessage()
    m["From"] = "Kinomap <no-reply@kinomap.com>"; m["Subject"] = "Your activity"; m["Message-ID"] = msgid
    m.set_content("x")
    m.add_attachment(buf.getvalue(), maintype="application", subtype="zip", filename="a.zip")
    return m.as_bytes()


class FakeIMAP:
    mails = {}
    login_error = None

    def __init__(self, host): pass
    def login(self, u, p):
        if FakeIMAP.login_error:
            raise FakeIMAP.login_error
        return "OK", []
    def select(self, name): return "OK", [b"3"]
    def list(self): return "OK", []
    def search(self, charset, *c): return "OK", [b" ".join(self.mails)]
    def fetch(self, num, parts):
        raw = self.mails[num]
        return "OK", [(b"%s (FLAGS () BODY[] {%d}" % (num, len(raw)), raw), b")"]
    def store(self, *a): return "OK", []
    def logout(self): return "OK", []


TYPES = [{"typeId": 4, "typeKey": "other", "parentTypeId": 17},
         {"typeId": 32, "typeKey": "indoor_rowing", "parentTypeId": 29},
         {"typeId": 30, "typeKey": "elliptical", "parentTypeId": 29}]
UPLOAD = {"detailedImportResult": {"uploadUuid": {"uuid": "86fc8bb3-12c6-468b-8d8e-0f5ed58b96da"},
          "creationDate": "2026-11-02 11:14:20.486 GMT", "successes": [], "failures": []}}


class FakeGarmin:
    def __init__(self, fail_upload=False, no_type=False):
        self.fail_upload, self.no_type, self.names = fail_upload, no_type, []
    def upload_activity(self, p):
        if self.fail_upload:
            raise RuntimeError("API Error 500 - boom")
        return UPLOAD
    def connectapi(self, path):
        return {"detailedImportResult": {"successes": [{"internalId": 24937796527}], "failures": []}}
    def get_activity_types(self): return [] if self.no_type else TYPES
    def set_activity_type(self, *a): pass
    def set_activity_name(self, i, n): self.names.append(n)


k2g.imaplib.IMAP4_SSL = FakeIMAP


def main(garmin, extra=None, level="INFO"):
    """Run main() like the CLI does, return (rc, summary dict, log file text, stderr text)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h); h.close()
    Path(os.environ["LOG_FILE"]).unlink(missing_ok=True)
    if Path(os.environ["STATE_FILE"]).exists():
        Path(os.environ["STATE_FILE"]).unlink()
    os.environ["LOG_LEVEL"] = level
    k2g.garmin_client = lambda: garmin
    summary_file = f"{tmpdir}/summary.json"
    Path(summary_file).unlink(missing_ok=True)
    old_err = sys.stderr; sys.stderr = io.StringIO()
    sys.argv = ["k2g", "--summary-file", summary_file] + (extra or [])
    try:
        rc = k2g.main()
    finally:
        captured = sys.stderr.getvalue(); sys.stderr = old_err
        for h in list(root.handlers):
            h.flush()
    summary = json.loads(Path(summary_file).read_text())
    return rc, summary, Path(os.environ["LOG_FILE"]).read_text(), captured


print("1) success: short message, log file + container log tagged with the trace id")
FakeIMAP.mails = {b"1": mail("running", "<m1@x>")}
rc, s, filelog, err = main(FakeGarmin())
print("   summary:", s)
assert rc == 0 and s == {"run_id": "abc12345", "ok": True, "message": "Uploaded Elliptical"}, s
assert "[abc12345]" in filelog and "[abc12345]" in err
assert "renamed to 'Elliptical'" in filelog
assert "Garmin upload response" not in filelog, "raw reply must be DEBUG only"
assert "Status check" not in filelog, "status polling must be DEBUG only"
assert "secret-pass-123" not in filelog + err, "password leaked"
print("   log file lines:", len(filelog.splitlines()))
print("   " + "\n   ".join(filelog.splitlines()))

print("2) DEBUG adds raw reply and status checks")
FakeIMAP.mails = {b"1": mail("rowing", "<m2@x>")}
rc, s, filelog, _ = main(FakeGarmin(), level="DEBUG")
assert "Garmin upload response" in filelog and "Status check" in filelog and s["message"] == "Uploaded Indoor Rowing"

print("3) two emails -> 'Uploaded 2: ...'")
FakeIMAP.mails = {b"1": mail("rowing", "<a@x>"), b"2": mail("running", "<b@x>")}
rc, s, *_ = main(FakeGarmin())
assert s["message"] == "Uploaded 2: Indoor Rowing, Elliptical", s

print("4) nothing new")
FakeIMAP.mails = {}
rc, s, *_ = main(FakeGarmin())
assert rc == 0 and s["ok"] and s["message"] == "No new Kinomap emails.", s

print("5) email without TCX")
FakeIMAP.mails = {b"1": mail("x", "<n@x>", with_tcx=False)}
rc, s, *_ = main(FakeGarmin())
assert s["ok"] and s["message"] == "1 email(s) without a TCX file", s

print("6) upload fails -> short message with trace id, traceback only in the log")
FakeIMAP.mails = {b"1": mail("rowing", "<f@x>")}
rc, s, filelog, err = main(FakeGarmin(fail_upload=True))
print("   summary:", s)
assert rc == 1 and not s["ok"] and s["message"] == "1 upload(s) failed (trace abc12345)", s
assert "Traceback" in filelog and "API Error 500" in filelog and "Traceback" not in s["message"]

print("7) uploaded but type could not be set -> still ok, warning with trace id")
FakeIMAP.mails = {b"1": mail("rowing", "<w@x>")}
rc, s, filelog, _ = main(FakeGarmin(no_type=True))
print("   summary:", s)
assert rc == 0 and s["ok"] and s["message"] == "Uploaded activity. 1 type/name update(s) not applied (trace abc12345)", s

print("8) mailbox login fails -> 'Failed: ...' with trace id, full traceback in log")
FakeIMAP.login_error = k2g.imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
rc, s, filelog, _ = main(FakeGarmin())
FakeIMAP.login_error = None
print("   summary:", s)
assert rc == 1 and not s["ok"] and s["message"].startswith("Failed: error: [AUTHENTICATIONFAILED]") \
    and s["message"].endswith("(trace abc12345)"), s
assert "Traceback" in filelog

print("9) missing settings")
saved = os.environ.pop("IMAP_HOST")
rc, s, *_ = main(FakeGarmin())
os.environ["IMAP_HOST"] = saved
assert rc == 2 and s["message"] == "Failed: IMAP_HOST, IMAP_USER and IMAP_PASSWORD must be set (trace abc12345)", s

print("10) unwritable log file location does not stop the run")
good = os.environ["LOG_FILE"]
os.environ["LOG_FILE"] = "/proc/nope/k2g.log"; k2g.LOG_FILE = "/proc/nope/k2g.log"
FakeIMAP.mails = {}
root = logging.getLogger()
for h in list(root.handlers):
    root.removeHandler(h)
old = sys.stderr; sys.stderr = io.StringIO()
sys.argv = ["k2g", "--summary-file", f"{tmpdir}/s2.json"]
rc = k2g.main()
warn = sys.stderr.getvalue(); sys.stderr = old
assert rc == 0 and "cannot write log file" in warn, warn
print("ALL OK")
