import os, pytest
from devpilot import sandbox as s

def test_blocks_traversal(tmp_path):
    with pytest.raises(s.Blocked): s.safe_path(tmp_path, "../x")

def test_blocks_symlink_escape(tmp_path):
    out = tmp_path / "out"; out.mkdir(); repo = tmp_path / "repo"; repo.mkdir()
    os.symlink(out, repo / "link")
    with pytest.raises(s.Blocked): s.safe_path(repo, "link/f")

@pytest.mark.parametrize("c", ["rm -rf /", "git push", "curl http://x", "python -c 'print(1)'", "cat ../x"])
def test_blocks_commands(tmp_path, c):
    with pytest.raises(s.Blocked): s.run(tmp_path, c)

def test_allows_ls(tmp_path):
    (tmp_path / "a.txt").write_text("hi")
    assert "a.txt" in s.run(tmp_path, "ls")["out"]
