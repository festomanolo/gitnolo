import json
import os
import time

from gitnolo import agents
from gitnolo.agents import FINISHED, IDLE, STALLED, WAITING, WORKING, Tracker


def write_jsonl(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(t))


def claude_file(home, repo):
    return home / ".claude" / "projects" / ("-" + str(repo).strip("/").replace("/", "-")) / "s1.jsonl"


def test_claude_working_then_finished_emits_event(tmp_path, repo):
    home = tmp_path / "h"
    now = time.time()
    f = claude_file(home, repo)
    base = [
        {"type": "user", "uuid": "u1", "cwd": str(repo), "timestamp": iso(now - 5), "message": {"role": "user", "content": "add login"}},
        {"type": "assistant", "uuid": "a1", "cwd": str(repo), "timestamp": iso(now - 4),
         "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": "Edit"}]}},
        {"type": "user", "uuid": "u2", "cwd": str(repo), "timestamp": iso(now - 3), "message": {"content": [{"type": "tool_result"}]}},
    ]
    write_jsonl(f, base)
    tr = Tracker([], home=str(home), processes=False)
    sessions, events = tr.poll()
    assert sessions[0].state == WORKING and sessions[0].repo == str(repo)
    final = [
        {"type": "assistant", "uuid": "a2", "cwd": str(repo), "timestamp": iso(now - 1),
         "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Done. The export still fails on big files."}]}},
        {"type": "system", "subtype": "turn_duration", "uuid": "t1", "timestamp": iso(now)},
        {"type": "system", "subtype": "away_summary", "uuid": "x", "timestamp": iso(now)},
    ]
    write_jsonl(f, base + final)
    os.utime(f, (now + 1, now + 1))
    sessions, events = tr.poll()
    assert sessions[0].state == FINISHED
    assert [e.kind for e in events] == ["turn_finished"]
    assert "still fails" in events[0].session.final_message
    assert events[0].session.turn_id.endswith("t1")


def test_claude_sidechain_and_gated_tools(tmp_path, repo):
    home = tmp_path / "h"
    now = time.time()
    write_jsonl(claude_file(home, repo), [
        {"type": "assistant", "uuid": "a1", "cwd": str(repo), "timestamp": iso(now),
         "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": "AskUserQuestion"}]}},
        {"type": "assistant", "uuid": "sc", "isSidechain": True, "cwd": str(repo), "timestamp": iso(now),
         "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "subagent"}]}},
    ])
    sessions, _ = Tracker([], home=str(home), processes=False).poll()
    assert sessions[0].state == WAITING


def test_claude_long_silence_is_stalled(tmp_path, repo):
    home = tmp_path / "h"
    old = time.time() - 3600
    write_jsonl(claude_file(home, repo), [
        {"type": "assistant", "uuid": "a1", "cwd": str(repo), "timestamp": iso(old),
         "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": "Bash"}]}},
    ])
    sessions, _ = Tracker([], home=str(home), processes=False).poll()
    assert sessions[0].state == STALLED


def test_kiro_waiting_and_finished(tmp_path, repo):
    home = tmp_path / "h"
    d = home / ".kiro" / "sessions" / "abc" / "sess_1"
    d.mkdir(parents=True)
    (d / "session.json").write_text(json.dumps({"id": "sess_1", "title": "Fix nav", "workspacePaths": [str(repo)]}))
    now = iso(time.time())
    entries = [
        {"id": "1", "timestamp": now, "payload": {"type": "user", "content": "fix nav"}},
        {"id": "2", "timestamp": now, "payload": {"type": "pending_interaction", "toolCallId": "t1", "question": "rm -rf build"}},
    ]
    write_jsonl(d / "messages.jsonl", entries)
    tr = Tracker([], home=str(home), processes=False)
    s, _ = tr.poll()
    assert s[0].state == WAITING and "rm -rf" in s[0].detail
    entries += [
        {"id": "3", "timestamp": now, "payload": {"type": "interaction_resolved", "toolCallId": "t1"}},
        {"id": "4", "timestamp": now, "payload": {"type": "assistant", "content": "Nav fixed."}},
        {"id": "5", "timestamp": now, "payload": {"type": "turn_end", "stopReason": "end_turn"}},
        {"id": "6", "timestamp": now, "payload": {"type": "session_start"}},
    ]
    write_jsonl(d / "messages.jsonl", entries)
    os.utime(d / "messages.jsonl", (time.time() + 2, time.time() + 2))
    s, ev = tr.poll()
    assert s[0].state == FINISHED and s[0].final_message == "Nav fixed."
    assert [e.kind for e in ev] == ["turn_finished"]


def test_antigravity_finished_with_cwd_from_tool_call(tmp_path, repo):
    home = tmp_path / "h"
    f = home / ".gemini" / "antigravity-cli" / "brain" / "b1" / ".system_generated" / "logs" / "transcript.jsonl"
    now = iso(time.time())
    write_jsonl(f, [
        {"step_index": 1, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": now,
         "content": "<USER_REQUEST>\nship it\n</USER_REQUEST>"},
        {"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": now,
         "tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls", "Cwd": f"\"{repo}\""}}]},
        {"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": now, "content": "Shipped."},
    ])
    s, _ = Tracker([], home=str(home), processes=False).poll()
    assert s[0].state == FINISHED and s[0].repo == str(repo) and s[0].title == "ship it"


def test_process_matcher():
    ps = agents.ProcessScanner(["claude", "codex"])
    assert ps._match("2.1.286", ["claude"], "/Users/x/.local/share/claude/versions/2.1.286") == "claude"
    assert ps._match("node", ["node", "/usr/lib/node_modules/codex/bin/codex.js"], "/usr/bin/node") == "codex"
    assert ps._match("Claude", ["/Applications/Claude.app/Contents/MacOS/Claude"], "/Applications/Claude.app/Contents/MacOS/Claude") is None
    assert ps._match("zsh", ["zsh"], "/bin/zsh") is None


from gitnolo.agents import CLOSED, ERRORED, INTERRUPTED, LIMITED, classify_error, parse_reset


def _claude_turn(repo, now, last_entries):
    return [
        {"type": "user", "uuid": "u1", "cwd": str(repo), "timestamp": iso(now - 5), "message": {"role": "user", "content": "go"}},
        *last_entries,
    ]


def test_claude_usage_limit_with_reset_time(tmp_path, repo):
    home = tmp_path / "h"
    now = time.time()
    write_jsonl(claude_file(home, repo), _claude_turn(repo, now, [
        {"type": "assistant", "uuid": "e1", "cwd": str(repo), "timestamp": iso(now), "isApiErrorMessage": True, "error": "rate_limit",
         "message": {"stop_reason": "stop_sequence", "content": [{"type": "text", "text": "You've hit your session limit · resets 10:30am (Africa/Dar_es_Salaam)"}]}},
        {"type": "system", "subtype": "turn_duration", "uuid": "t1", "timestamp": iso(now)},
    ]))
    s, _ = Tracker([], home=str(home), processes=False).poll()
    assert s[0].state == LIMITED and s[0].stop_reason == "usage limit" and s[0].partial
    assert time.localtime(s[0].resume_at).tm_hour == 10 and time.localtime(s[0].resume_at).tm_min == 30
    assert s[0].final_message == ""


def test_claude_network_error(tmp_path, repo):
    home = tmp_path / "h"
    now = time.time()
    write_jsonl(claude_file(home, repo), _claude_turn(repo, now, [
        {"type": "assistant", "uuid": "e1", "cwd": str(repo), "timestamp": iso(now), "isApiErrorMessage": True, "error": "server_error",
         "message": {"content": [{"type": "text", "text": "API Error: Connection dropped (ECONNRESET)"}]}},
    ]))
    s, _ = Tracker([], home=str(home), processes=False).poll()
    assert s[0].state == ERRORED and s[0].stop_reason == "network"


def test_claude_user_interrupt_is_partial(tmp_path, repo):
    home = tmp_path / "h"
    now = time.time()
    write_jsonl(claude_file(home, repo), _claude_turn(repo, now, [
        {"type": "user", "uuid": "u2", "cwd": str(repo), "timestamp": iso(now), "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}},
    ]))
    s, _ = Tracker([], home=str(home), processes=False).poll()
    assert s[0].state == INTERRUPTED and s[0].stop_reason == "user interrupt" and s[0].partial


def test_kiro_cancelled_error_and_stuck_loop(tmp_path, repo):
    home = tmp_path / "h"
    d = home / ".kiro" / "sessions" / "abc" / "sess_1"
    d.mkdir(parents=True)
    (d / "session.json").write_text(json.dumps({"id": "sess_1", "workspacePaths": [str(repo)]}))
    now = iso(time.time())
    base = [{"id": "1", "timestamp": now, "payload": {"type": "user", "content": "go"}}]
    cases = [
        ([{"id": "2", "timestamp": now, "payload": {"type": "turn_end", "stopReason": "cancelled"}}], INTERRUPTED, "user interrupt"),
        ([{"id": "2", "timestamp": now, "payload": {"type": "assistant", "content": "Request failed: 429 Too Many Requests"}},
          {"id": "3", "timestamp": now, "payload": {"type": "turn_end", "stopReason": "error"}}], LIMITED, "usage limit"),
        ([{"id": "2", "timestamp": now, "payload": {"type": "pending_interaction", "toolCallId": "failure-intervention-prompt-1",
                                                     "question": "execute_bash failed 7 times in a row"}}], WAITING, "stuck in a loop"),
    ]
    for i, (tail, state, reason) in enumerate(cases):
        write_jsonl(d / "messages.jsonl", base + tail)
        os.utime(d / "messages.jsonl", (time.time() + i + 1, time.time() + i + 1))
        s, _ = Tracker([], home=str(home), processes=False).poll()
        assert (s[0].state, s[0].stop_reason) == (state, reason)


def test_closed_mid_task_when_process_vanishes(tmp_path, repo):
    home = tmp_path / "h"
    now = time.time()
    write_jsonl(claude_file(home, repo), _claude_turn(repo, now, [
        {"type": "assistant", "uuid": "a1", "cwd": str(repo), "timestamp": iso(now),
         "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": "Edit"}]}},
    ]))
    tr = Tracker([], home=str(home), processes=False)

    class FakeProcs:
        def __init__(self):
            self.alive = True

        def scan(self):
            return [agents.Session("proc:42", "claude", "42", str(repo), str(repo), "running", time.time(), pid=42)] if self.alive else []

    tr.procs = FakeProcs()
    s, _ = tr.poll()
    assert s[0].pid == 42 and s[0].state == WORKING
    tr.procs.alive = False
    s, ev = tr.poll()
    assert s[0].state == CLOSED and s[0].stop_reason == "closed mid-task"
    assert "closed" in [e.kind for e in ev]


def test_classifier_and_reset_parser():
    assert classify_error("API Error: Unable to connect to API (ENOTFOUND)", "server_error") == "network"
    assert classify_error("Overloaded", "server_error") == "server error"
    assert classify_error("Login expired · Please run /login", "authentication_failed") == "auth expired"
    assert classify_error("RESOURCE_EXHAUSTED (code 429)") == "usage limit"
    assert classify_error("UNAVAILABLE (code 503): The service is currently unavailable.") == "server error"
    noon = time.mktime(time.strptime("2026-10-01 12:00", "%Y-%m-%d %H:%M"))
    assert time.localtime(parse_reset("resets 5:10pm", noon)).tm_hour == 17
    assert parse_reset("resets 10:30am", noon) > noon + 20 * 3600  # tomorrow
