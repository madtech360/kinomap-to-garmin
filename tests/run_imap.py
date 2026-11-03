import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import importlib.util, io, zipfile, logging, os, sys
from email.message import EmailMessage
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location("k2g", str(ROOT / "kinomap_to_garmin.py"))
k2g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(k2g)

logging.basicConfig(level=logging.INFO, format="  LOG %(message)s")

TCX = b'<?xml version="1.0"?><TrainingCenterDatabase/>'


def raw_mail(subject, msgid):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a.tcx", TCX)
    m = EmailMessage()
    m["From"] = "Kinomap <no-reply@kinomap.com>"
    m["Subject"] = subject
    m["Message-ID"] = msgid
    m["Date"] = "Mon, 02 Nov 2026 10:00:00 +0530"
    m.set_content("x")
    m.add_attachment(buf.getvalue(), maintype="application", subtype="zip", filename="a.zip")
    return m.as_bytes()


class FakeIMAP:
    calls = []
    folders = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Kinomap"',
               b'(\\HasNoChildren) "/" "[Gmail]/Trash"']
    exists = {"INBOX": 3, "Kinomap": 2}
    mails = {}  # num -> (flags, raw)

    def __init__(self, host):
        pass

    def login(self, u, p):
        return "OK", []

    def select(self, name):
        FakeIMAP.calls.append(("select", name))
        key = name.strip('"')
        if key not in self.exists:
            return "NO", [b"[NONEXISTENT] Unknown Mailbox"]
        return "OK", [str(self.exists[key]).encode()]

    def list(self):
        return "OK", self.folders

    def search(self, charset, *criteria):
        FakeIMAP.calls.append(("search", criteria))
        return "OK", [b" ".join(self.mails.keys())]

    def fetch(self, num, parts):
        flags, raw = self.mails[num]
        head = b"%s (FLAGS (%s) BODY[] {%d}" % (num, flags, len(raw))
        return "OK", [(head, raw), b")"]

    def store(self, num, op, flag):
        FakeIMAP.calls.append(("store", num, flag))
        return "OK", []

    def close(self):
        FakeIMAP.calls.append(("close",))

    def logout(self):
        FakeIMAP.calls.append(("logout",))


k2g.imaplib.IMAP4_SSL = FakeIMAP
os.environ.update(IMAP_HOST="h", IMAP_USER="u", IMAP_PASSWORD="p")
os.environ["STATE_FILE"] = str(Path(__import__("tempfile").mkdtemp()) / "state_test.json")
k2g.STATE_FILE = k2g.Path(os.environ["STATE_FILE"])
if k2g.STATE_FILE.exists():
    k2g.STATE_FILE.unlink()

def args(**kw):
    base = dict(folder="Kinomap", sender="kinomap", subject=None, dry_run=True)
    base.update(kw)
    return SimpleNamespace(**base)

print("1) wrong folder name -> clear error with folder list")
try:
    k2g.run(args(folder="Kinomaps"))
    raise SystemExit("FAIL: no error")
except RuntimeError as e:
    print("  ", str(e).replace("\n", "\n   "))
    assert "Kinomap" in str(e) and "[Gmail]/Trash" in str(e)

print("2) search uses UNDELETED; \\Deleted-flagged mail is skipped; normal mail handled")
FakeIMAP.calls.clear()
FakeIMAP.mails = {
    b"1": (b"\\Seen \\Deleted", raw_mail("old deleted", "<d@x>")),
    b"2": (b"", raw_mail("fresh ride", "<f@x>")),
}
rc = k2g.run(args())
search = [c for c in FakeIMAP.calls if c[0] == "search"][0][1]
print("   search criteria:", search)
assert "UNDELETED" in search and "UNSEEN" in search
assert ("close",) not in FakeIMAP.calls, "must not CLOSE (would expunge)"
assert ("logout",) in FakeIMAP.calls
assert not any(c[0] == "store" for c in FakeIMAP.calls), "dry-run must not mark anything"

print("3) folder with a space gets quoted")
FakeIMAP.exists["My Rides"] = 1
FakeIMAP.calls.clear()
FakeIMAP.mails = {}
k2g.run(args(folder="My Rides"))
print("   ", [c for c in FakeIMAP.calls if c[0] == "select"])
assert ("select", '"My Rides"') in FakeIMAP.calls
print("ALL OK")
