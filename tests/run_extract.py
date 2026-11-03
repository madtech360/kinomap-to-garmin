import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import importlib.util, io, zipfile, email, tempfile, sys
from email.message import EmailMessage
from pathlib import Path

spec = importlib.util.spec_from_file_location("k2g", str(ROOT / "kinomap_to_garmin.py"))
k2g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(k2g)

TCX = b'<?xml version="1.0"?><TrainingCenterDatabase><Activities><Activity Sport="Other"><Id>2026-11-02T05:00:00Z</Id></Activity></Activities></TrainingCenterDatabase>'

def make_zip(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for n in names:
            zf.writestr(n, TCX if n.endswith(".tcx") else b"x")
        zf.writestr("../evil.tcx", TCX)  # zip-slip attempt
    return buf.getvalue()

def build(attach_name, payload, mime=("application", "zip")):
    m = EmailMessage()
    m["From"] = "Kinomap <no-reply@kinomap.com>"
    m["Subject"] = "Your Kinomap activity"
    m["Message-ID"] = "<t1@example>"
    m.set_content("Your activity is attached")
    m.add_attachment(payload, maintype=mime[0], subtype=mime[1], filename=attach_name)
    return email.message_from_bytes(m.as_bytes())

with tempfile.TemporaryDirectory() as d:
    out = Path(d)
    # 1) normal ZIP with csv/gpx/tcx/pwx
    msg = build("activity.zip", make_zip(["a.csv", "a.gpx", "a.tcx", "a.pwx"]))
    files = k2g.extract_tcx_files(msg, out)
    names = sorted(f.name for f in files)
    print("zip case:", names)
    assert "a.tcx" in names and all(f.parent == out for f in files), "path escape!"
    assert not any(n.endswith((".csv", ".gpx", ".pwx")) for n in names)
    assert not (out.parent / "evil.tcx").exists()

with tempfile.TemporaryDirectory() as d:
    # 2) no tcx
    msg = build("a.zip", make_zip(["a.csv"]))
    print("no-tcx case:", [f.name for f in k2g.extract_tcx_files(msg, Path(d))])

with tempfile.TemporaryDirectory() as d:
    # 3) bad zip
    msg = build("a.zip", b"not a zip")
    print("bad-zip case:", k2g.extract_tcx_files(msg, Path(d)))

with tempfile.TemporaryDirectory() as d:
    # 4) tcx attached directly
    msg = build("run.tcx", TCX, ("application", "octet-stream"))
    print("direct case:", [f.name for f in k2g.extract_tcx_files(msg, Path(d))])

print("ALL OK")
