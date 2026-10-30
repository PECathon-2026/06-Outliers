"""Tools agents can call. Every path goes through the sandbox guard."""
import difflib, re
from pathlib import Path
from . import sandbox

IGN = {"__pycache__", ".pytest_cache", ".git"}
EXT = {".py", ".md", ".txt", ".yaml", ".json"}
RULES = [  # (regex, kind, severity, python_only)
    (r"""(?i)((api[_-]?key|secret|password|token)\s*=\s*['"][^'"]{8,}['"]|sk-[A-Za-z0-9-]{10,})""", "hardcoded secret", "high", False),
    (r"""execute\(\s*f['"]|execute\([^)]*(%|\+|\.format\()""", "SQL built from strings (injection risk)", "high", True),
    (r"os\.system\(|shell\s*=\s*True", "shell execution (command injection risk)", "high", True),
    (r"\beval\(|\bexec\(", "eval/exec", "medium", True),
    (r"pytest\.mark\.(skip|xfail)|pytest\.skip\(", "test skipped or disabled", "high", True),
    (r"(?i)ignore (all )?(previous|prior) instructions", "prompt-injection text in repo", "medium", False)]

def files(root):
    return {str(p.relative_to(root)): p.read_text(errors="ignore") for p in sorted(Path(root).rglob("*"))
            if p.is_file() and not set(p.relative_to(root).parts) & IGN and p.suffix in EXT}

def diff(a, b):
    fa, fb, out = files(a), files(b), []
    for k in sorted(set(fa) | set(fb)):
        if fa.get(k) != fb.get(k):
            out += difflib.unified_diff(fa.get(k, "").splitlines(True), fb.get(k, "").splitlines(True), f"a/{k}", f"b/{k}")
    return "".join(out)

def _tests(root):
    return {k: t for k, t in files(root).items() if re.search(r"(^|/)(test_[^/]*|[^/]*_test)\.py$", k)}

def _n(t):
    return len(re.findall(r"^\s*assert\b", t, re.M)), len(re.findall(r"^\s*def test_", t, re.M))

def scan(root, orig=None):
    """Rule scan; with orig, also flags existing tests that were weakened or removed."""
    hits = [dict(file=f, line=i, kind=k, severity=sev) for f, t in files(root).items()
            for i, l in enumerate(t.splitlines(), 1) for rx, k, sev, py in RULES
            if (not py or f.endswith(".py")) and re.search(rx, l)]
    if orig:
        now = _tests(root)
        hits += [dict(file=k, line=0, kind="existing tests weakened or removed", severity="high")
                 for k, t in _tests(orig).items() if any(x < y for x, y in zip(_n(now.get(k, "")), _n(t)))]
    return hits

def apply_to_repo(repo, work):
    for k, t in files(work).items():
        p = Path(sandbox.safe_path(repo, k))
        if not p.exists() or p.read_text() != t:
            p.write_text(t)

class Toolbox:
    NAMES = ("read_file", "search_repo", "edit_file", "run_tests", "git_diff", "secret_scan", "create_file")
    def __init__(s, work, orig, timeout=30):
        s.w, s.o, s.t = Path(work), orig, timeout
    def read_file(s, path): return Path(sandbox.safe_path(s.w, path)).read_text()
    def search_repo(s, query=""):
        out = []
        for f, t in files(s.w).items():
            if not query: out.append(f)
            else: out += [f"{f}:{i}: {l.strip()}" for i, l in enumerate(t.splitlines(), 1) if query.lower() in l.lower()]
        return "\n".join(out) or "no matches"
    def edit_file(s, path, old, new):
        p = Path(sandbox.safe_path(s.w, path)); t = p.read_text()
        if t.count(old) != 1: raise ValueError(f"old text must occur exactly once (found {t.count(old)})")
        p.write_text(t.replace(old, new)); return "edited " + path
    def create_file(s, path, content):
        p = Path(sandbox.safe_path(s.w, path))
        if p.exists(): raise ValueError("file exists: use edit_file")
        p.parent.mkdir(parents=True, exist_ok=True); p.write_text(content); return "created " + path
    def run_tests(s): return sandbox.run(s.w, "python -m pytest -q", s.t)
    def git_diff(s): return diff(s.o, s.w) or "no changes"
    def secret_scan(s): return "\n".join(f"{h['severity']} {h['file']}:{h['line']} {h['kind']}" for h in scan(s.w)) or "clean"
    def call(s, name, args):
        """Never raises: tool failure comes back as text so the agent can adapt."""
        if name not in s.NAMES: return "ERROR: unknown tool " + str(name)
        try:
            r = getattr(s, name)(**(args or {}))
            return (f"exit {r['code']}\n{r['out']}" if name == "run_tests" else str(r))[:6000]
        except Exception as e:
            return "ERROR: " + str(e)
