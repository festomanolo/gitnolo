import os
import sys

from gitnolo import failover
from gitnolo.auto_accept import supervise_command


def _home(tmp_path, monkeypatch):
    monkeypatch.setattr(failover, "STATE_FILE", str(tmp_path / "failover.json"))
    monkeypatch.setattr(failover, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(failover, "BROWSER_DIR", str(tmp_path / "browser"))
    monkeypatch.setattr(failover, "HANDOFF_DIR", str(tmp_path / "handoff"))


def test_rotation_skips_limited_profiles(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    profs = ["A", "B", "C"]
    assert failover.current("kiro-cli", profs) == "A"
    failover.mark_limited("kiro", "B")
    assert failover.next_profile("kiro", profs) == "C"
    failover.mark_limited("kiro", "C")
    assert failover.next_profile("kiro", profs) is None


def test_limit_text_detection():
    assert failover.screen_limited("You have reached your monthly usage limit")
    assert failover.screen_limited("Error: 429 Too Many Requests")
    assert not failover.screen_limited("def handle_rate_limit(): pass")


def test_handoff_carries_task_and_last_message(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)

    class S:
        title = "add retries to the uploader"
        messages = [("1", "Editing upload.py next", 0.0)]
        final_message = ""

    text = failover.handoff_prompt("kiro", S(), str(tmp_path))
    assert "add retries" in text and "Editing upload.py" in text


def test_supervisor_restarts_agent_on_limit(tmp_path, monkeypatch, capfd):
    code = "import time;print('You have reached your usage limit',flush=True);time.sleep(30)"
    seen = []
    monkeypatch.setattr(sys, "stdin", open(os.devnull))

    def on_limit(screen):
        seen.append(screen)
        return [sys.executable, "-c", "print('RESUMED')"]

    supervise_command([sys.executable, "-c", code], auto_accept=False, on_limit=on_limit)
    assert seen and "RESUMED" in capfd.readouterr().out
