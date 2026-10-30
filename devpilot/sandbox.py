"""Sandbox: path guard + command allowlist + timeout + output cap."""
import os, shlex, subprocess, sys

class Blocked(Exception):
    pass

def safe_path(root, p):
    r = os.path.realpath(root)
    t = os.path.realpath(os.path.join(r, p))  # resolves .. and symlinks
    if t != r and not t.startswith(r + os.sep):
        raise Blocked(f"path escapes repo root: {p}")
    return t

GIT_OK = {"diff", "status", "log"}

def run(root, cmd, timeout=30, cap=8000):
    a = shlex.split(cmd)
    if not a:
        raise Blocked("empty command")
    n = a[0]
    if n == "python":
        if a[1:3] != ["-m", "pytest"]:
            raise Blocked("only 'python -m pytest' is allowed")
        argv, args = [sys.executable, "-m", "pytest"], a[3:]
    elif n == "pytest":
        argv, args = [sys.executable, "-m", "pytest"], a[1:]
    elif n in ("ls", "cat"):
        argv, args = [n], a[1:]
    elif n == "git":
        if a[1:2] and a[1] in GIT_OK:
            argv, args = ["git", a[1]], a[2:]
        else:
            raise Blocked("git subcommand not allowed")
    else:
        raise Blocked(f"command not allowlisted: {n}")
    for x in args:
        if not x.startswith("-"):
            safe_path(root, x)
    try:
        p = subprocess.run(argv + args, cwd=root, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"code": -1, "out": f"timeout after {timeout}s"}
    except FileNotFoundError:
        return {"code": -2, "out": f"tool unavailable: {argv[0]}"}
    return {"code": p.returncode, "out": (p.stdout + p.stderr)[-cap:]}
