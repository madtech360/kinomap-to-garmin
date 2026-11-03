import json, os, sys, time
mode = os.environ.get("STUB_MODE", "ok")
run_id = os.environ["RUN_ID"]
summary = sys.argv[sys.argv.index("--summary-file") + 1]
print(f"2026-11-02 11:00:00,000 INFO [{run_id}] stub log line (full detail lives here)", file=sys.stderr, flush=True)
if mode == "ok":
    open(summary, "w").write(json.dumps({"run_id": run_id, "ok": True, "message": "Uploaded Elliptical"}))
elif mode == "fail":
    open(summary, "w").write(json.dumps({"run_id": run_id, "ok": False, "message": f"1 upload(s) failed (trace {run_id})"}))
elif mode == "crash":
    sys.exit(3)
elif mode == "slow":
    time.sleep(4)
    open(summary, "w").write(json.dumps({"run_id": run_id, "ok": True, "message": "Uploaded Elliptical"}))
