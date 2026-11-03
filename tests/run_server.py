import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import json, os, subprocess, sys, threading, time, urllib.request, urllib.error

TOKEN = "test-token-0123456789abcdef"

def start(mode, port, timeout="30"):
    env = dict(os.environ, TRIGGER_TOKEN=TOKEN, TRIGGER_PORT=str(port), STUB_MODE=mode, RUN_TIMEOUT=timeout,
               SCRIPT_PATH=str(ROOT / "tests" / "stubs" / "stub.py"))
    p = subprocess.Popen([sys.executable, "-I", str(ROOT / "trigger_server.py")], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1); return p
        except Exception:
            time.sleep(0.1)
    raise SystemExit("server did not start: " + p.stderr.read())

def post(port, token=TOKEN):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/run", method="POST",
                                 headers={"Authorization": f"Bearer {token}"})
    try:
        r = urllib.request.urlopen(req, timeout=30); return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

def stop(p):
    p.terminate(); return p.communicate(timeout=5)[1]

print("1) success -> short JSON, no log dump; detail goes to the container log with the same trace id")
p = start("ok", 18801); code, body = post(18801); err = stop(p)
print("  ", body)
assert code == 200 and set(body) == {"ok", "message", "trace_id"} and body["ok"] and body["message"] == "Uploaded Elliptical"
assert len(body["trace_id"]) == 8
assert f"[{body['trace_id']}] stub log line" in err, err          # uploader line reached the container log
assert f"[{body['trace_id']}] run finished: ok=True" in err        # server lines too

print("2) failure -> short message carrying the trace id")
p = start("fail", 18802); code, body = post(18802); err = stop(p)
print("  ", body)
assert not body["ok"] and body["message"] == f"1 upload(s) failed (trace {body['trace_id']})"
assert f"[{body['trace_id']}] stub log line" in err

print("3) crash with no summary -> still a short, traceable message")
p = start("crash", 18803); code, body = post(18803); err = stop(p)
print("  ", body)
assert not body["ok"] and body["message"] == f"Failed: the run ended unexpectedly, exit code 3 (trace {body['trace_id']})"

print("4) timeout")
p = start("slow", 18804, timeout="1"); code, body = post(18804); err = stop(p)
print("  ", body)
assert not body["ok"] and body["message"] == f"Failed: timed out after 1s (trace {body['trace_id']})"

print("5) second press while a run is in progress")
p = start("slow", 18805, timeout="30")
res = {}
t = threading.Thread(target=lambda: res.update(first=post(18805))); t.start()
time.sleep(1)
code2, body2 = post(18805)
t.join(); stop(p)
print("  ", body2)
assert body2 == {"ok": False, "message": "A run is already in progress, try again shortly.", "trace_id": None}
assert res["first"][1]["ok"]

print("6) auth still enforced; temp summary files cleaned up")
p = start("ok", 18806)
assert post(18806, token="nope")[0] == 401
post(18806); stop(p)
import glob, tempfile
assert not glob.glob(os.path.join(tempfile.gettempdir(), "k2g-*.json")), "summary temp file left behind"
print("ALL OK")
