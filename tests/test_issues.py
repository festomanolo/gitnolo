from gitnolo import issues


def titles(msg):
    return [d.title for d in issues.extract(msg)]


def test_keeps_unresolved_problems():
    msg = """Done.

- The login page still returns a 500 when the session cookie is missing
- Untested: I couldn't run the iOS build here
- TODO: wire the settings toggle to the API
"""
    t = titles(msg)
    assert len(t) == 3


def test_drops_resolved_and_descriptive_lines():
    msg = """Summary
- Fixed the crash on startup
- The error is gone and all 40 tests pass
- Cause: the cache returned stale data when the request failed
- Fix: the onboarding screen now appears only for new users
- I checked the flows with no console errors
- Locked periods so they cannot be edited
"""
    assert titles(msg) == []


def test_ignores_agent_environment_chatter():
    msg = "Several Bash and Edit calls failed with a permission error.\nThe terminal is in a broken state."
    assert titles(msg) == []


def test_heading_bullets_and_dedupe():
    msg = "## Next steps\n- Add rate limiting to the upload endpoint\n- Add rate limiting to upload endpoint\n"
    t = titles(msg)
    assert len(t) == 1


def test_llm_can_only_filter_not_invent():
    class FakeLLM:
        def refine_issues(self, message, cands):
            return [{"index": 0, "title": "Fix 500 on missing session cookie", "type": "bug"}, {"index": 99, "title": "invented"}]

    msg = "- The login page still returns a 500 when the cookie is missing\n- TODO: dark mode toggle"
    ds = issues.extract(msg, llm=FakeLLM())
    assert [d.title for d in ds] == ["Fix 500 on missing session cookie"]
    assert "bug" in ds[0].labels
