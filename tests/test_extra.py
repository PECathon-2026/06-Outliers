import time
from pathlib import Path
from fastapi.testclient import TestClient
from devpilot import app as A, llm, sandbox, tools

c = TestClient(A.app)
SEEDS = Path(__file__).parents[1] / "seeds"

def nokeys(mp):
    for k in llm.KEYS.values(): mp.delenv(k, raising=False)

def start(req="fix the discount calculation please"):
    c.post("/repos/reset")
    return c.post("/missions", json={"repo": "discount_shop", "request": req}).json()["id"]

def wait(mid):
    for _ in range(150):
        s = c.get(f"/missions/{mid}").json()
        if s["status"] != "running": return s
        time.sleep(0.2)
    raise AssertionError("mission timed out")

def src(): return (A.TARGETS / "discount_shop" / "discount.py").read_text()

def test_reject_leaves_repo_untouched(monkeypatch):
    nokeys(monkeypatch); mid = start()
    assert wait(mid)["status"] == "awaiting_approval"
    assert c.post(f"/missions/{mid}/reject").json()["status"] == "rejected"
    assert "price - pct" in src()

def test_approve_applies_and_cannot_repeat(monkeypatch):
    nokeys(monkeypatch); mid = start(); wait(mid)
    assert c.post(f"/missions/{mid}/approve").json()["status"] == "approved"
    assert "1 - pct / 100" in src()
    assert c.post(f"/missions/{mid}/approve").status_code == 409

def test_sse_stream_ends_with_done(monkeypatch):
    nokeys(monkeypatch); mid = start(); wait(mid)
    body = c.get(f"/missions/{mid}/events").text
    assert '"agent": "head"' in body and body.rstrip().endswith("data: {}")

def test_bad_repo_rejected():
    assert c.post("/missions", json={"repo": "../seeds", "request": "do a thing now"}).status_code == 400
    assert c.post("/missions", json={"repo": "nope", "request": "do a thing now"}).status_code == 404

def test_escalates_instead_of_looping_forever(monkeypatch):
    nokeys(monkeypatch); orig = llm.mock
    monkeypatch.setattr(llm, "mock", lambda role, d: {"tool": "done", "reason": "gave up"} if role == "builder" else orig(role, d))
    s = wait(start())
    assert s["status"] == "escalated" and s["iters"] <= llm.CFG["limits"]["max_iters"]

def test_tamper_guard_flags_weakened_tests(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"; a.mkdir(); b.mkdir()
    (a / "test_x.py").write_text("def test_a():\n    assert 1\n    assert 2\n")
    (b / "test_x.py").write_text("def test_a():\n    assert 1\n")
    assert any("weakened" in h["kind"] for h in tools.scan(b, a))
    (b / "test_x.py").write_text("import pytest\n@pytest.mark.skip\ndef test_a():\n    assert 1\n    assert 2\n")
    assert any("skipped" in h["kind"] for h in tools.scan(b, a))

def test_create_file_is_sandboxed(tmp_path):
    tb = tools.Toolbox(tmp_path, tmp_path)
    assert tb.call("create_file", {"path": "tests/test_new.py", "content": "x"}).startswith("created")
    assert tb.call("create_file", {"path": "tests/test_new.py", "content": "y"}).startswith("ERROR")
    assert tb.call("create_file", {"path": "../out.py", "content": "x"}).startswith("ERROR")

def test_new_seed_repos_start_failing():
    for name in ("failing_test", "sqli_users"):
        assert sandbox.run(SEEDS / name, "python -m pytest -q -p no:cacheprovider")["code"] == 1
