import shutil
from pathlib import Path
from devpilot import graph, llm, tools
from devpilot.events import Log

SEED = Path(__file__).parents[1] / "seeds" / "discount_shop"

def mk(tmp, req, mid):
    repo = tmp / "repo"; shutil.copytree(SEED, repo)
    return repo, dict(id=mid, repo=str(repo), request=req, status="running", plan=None, diff="", report=None, iters=0, run_dir=str(tmp / "run"))

def test_review_finding_changes_builder_action(tmp_path, monkeypatch):
    for k in llm.KEYS.values(): monkeypatch.delenv(k, raising=False)
    repo, M = mk(tmp_path, "fix the discount calculation", "t1")
    log = Log(tmp_path / "e.db"); graph.run_mission(M, log, llm.CFG["limits"])
    ev = log.list("t1")
    assert M["status"] == "awaiting_approval" and M["iters"] == 2
    crit = next(e for e in ev if e["agent"] == "shadow" and e["type"] == "critique")   # scan found the planted secret
    nxt = next(e for e in ev if e["agent"] == "builder" and crit["id"] in e["refs"])   # builder reacted directly to it
    assert "config.py" in crit["payload"]["msg"] and nxt["payload"]["tool"] == "read_file"
    assert 'os.environ' in (tmp_path / "run" / "work" / "config.py").read_text()
    assert [e["payload"]["code"] for e in ev if e["type"] == "test_result"] == [0, 0]
    assert M["report"]["unresolved"] == [] and "sk-live" in (repo / "config.py").read_text()  # real repo untouched

def test_vague_request_asks_human(tmp_path):
    repo, M = mk(tmp_path, "fix it", "t2")
    graph.run_mission(M, Log(tmp_path / "e.db"), llm.CFG["limits"])
    assert M["status"] == "needs_clarification"

def test_tool_failures_do_not_crash(tmp_path):
    tb = tools.Toolbox(tmp_path, tmp_path)
    assert tb.call("edit_file", {"path": "../x", "old": "a", "new": "b"}).startswith("ERROR")
    assert tb.call("rm_rf", {}).startswith("ERROR")

def test_scan_flags_sql_injection_and_shell(tmp_path):
    (tmp_path / "db.py").write_text('def f(c, n):\n    c.execute(f"SELECT * FROM u WHERE n=\'{n}\'")\n    os.system("ls " + n)\n')
    kinds = {h["kind"] for h in tools.scan(tmp_path)}
    assert any("SQL" in k for k in kinds) and any("shell" in k for k in kinds)
