"""pytest entry point. Each tests/run_*.py is a self-contained scenario script that prints
"ALL OK" when every assertion in it passes; they use fake IMAP/Garmin objects, so no network,
mailbox or Garmin account is needed."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = sorted(p.name for p in (ROOT / "tests").glob("run_*.py"))


@pytest.mark.parametrize("script", SCRIPTS)
def test_scenarios(script):
    proc = subprocess.run([sys.executable, str(ROOT / "tests" / script)],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0 and "ALL OK" in proc.stdout, proc.stdout[-3000:] + proc.stderr[-3000:]


@pytest.mark.parametrize("token,expected", [
    ("", "at least 16"),
    ("short", "at least 16"),
    ("change-me-generate-with-openssl-rand-hex-24", "placeholder"),
])
def test_server_refuses_weak_or_example_token(token, expected):
    env = dict(os.environ, TRIGGER_TOKEN=token)
    proc = subprocess.run([sys.executable, "-I", str(ROOT / "trigger_server.py")],
                          env=env, capture_output=True, text=True, timeout=20)
    assert proc.returncode != 0 and expected in proc.stderr


def test_no_secrets_or_personal_data_committed():
    import re
    banned = ["/tmp/claude", "/tmp/work", "3f9a1c7e52b84d06a9e1c3f7b2d84a60"]
    dummy_emails = {"you@gmail.com", "you@example.com", "your.name@gmail.com", "your.name@example.com",
                    "no-reply@kinomap.com"}
    email_re = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
    for path in ROOT.rglob("*"):
        if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        if path.suffix in (".pyc", ".zip"):
            continue
        text = path.read_text(errors="ignore")
        rel = path.relative_to(ROOT)
        for needle in banned:
            assert needle not in text or path.name == "test_suite.py", f"{needle!r} found in {rel}"
        for addr in email_re.findall(text):
            assert addr.lower() in dummy_emails or addr.lower().endswith("@users.noreply.github.com") \
                or path.name == "test_suite.py", f"real-looking email {addr!r} in {rel}"
