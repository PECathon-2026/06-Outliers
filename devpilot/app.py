
import json
import shutil
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import graph, llm, sandbox, tools
from .events import Log, mermaid

ROOT = Path(__file__).resolve().parents[1]
TARGETS = ROOT / "targets"
SEEDS = ROOT / "seeds"
RUNS = ROOT / "runs"

RUNS.mkdir(parents=True, exist_ok=True)

log = Log(RUNS / "events.db")
MISSIONS = {}
MISSION_LOCK = threading.RLock()
app = FastAPI(title="DevPilot")

MISSION_STORE = RUNS / "missions.json"


def _save_missions():
    """Persist mission metadata atomically."""
    with MISSION_LOCK:
        tmp = MISSION_STORE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(MISSIONS, default=str, indent=2),
            encoding="utf-8",
        )
        tmp.replace(MISSION_STORE)


def _load_missions():
    """Restore persisted mission metadata after a server restart."""
    if not MISSION_STORE.exists():
        return

    try:
        data = json.loads(MISSION_STORE.read_text(encoding="utf-8"))

        if not isinstance(data, dict):
            return

        with MISSION_LOCK:
            for mid, mission in data.items():
                if not isinstance(mission, dict):
                    continue

                # Worker threads cannot resume automatically after a restart.
                if mission.get("status") == "running":
                    mission["status"] = "escalated"
                    mission["report"] = mission.get("report") or {
                        "verified": [],
                        "claims": [],
                        "unresolved": [
                            "Server restarted while this mission was running. "
                            "Launch a new mission and review the interrupted run."
                        ],
                    }

                MISSIONS[mid] = mission

    except (OSError, json.JSONDecodeError):
        # A damaged cache must not prevent the API from starting.
        return


_load_missions()


def reset_targets():
    """Restore the working repositories from their seed copies."""
    if not SEEDS.is_dir():
        raise RuntimeError(f"Seed directory does not exist: {SEEDS}")

    shutil.rmtree(TARGETS, ignore_errors=True)
    shutil.copytree(SEEDS, TARGETS)


if not TARGETS.exists():
    reset_targets()


class New(BaseModel):
    repo: str
    request: str


def _work(mission):
    """Run a mission in the background and persist its final state."""
    try:
        Path(mission["run_dir"]).mkdir(parents=True, exist_ok=True)
        graph.run_mission(mission, log, llm.CFG["limits"])

        # Ensure the final state is saved even if graph execution returns
        # without producing a report.
        if mission.get("status") == "running":
            mission["status"] = "escalated"
            mission["report"] = mission.get("report") or {
                "verified": [],
                "claims": [],
                "unresolved": [
                    "Mission execution ended without a final report."
                ],
            }

    except Exception as exc:
        log.add(
            mission["id"],
            "head",
            "escalation",
            {"msg": f"Mission crashed: {exc}"},
        )
        mission["status"] = "escalated"
        mission["report"] = mission.get("report") or {
            "verified": [],
            "claims": [],
            "unresolved": [f"Mission crashed: {exc}"],
        }

    finally:
        _save_missions()


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "service": "DevPilot"}


@app.get("/repos")
def repos():
    if not TARGETS.is_dir():
        return []
    return sorted(
        path.name for path in TARGETS.iterdir() if path.is_dir()
    )


@app.post("/repos/reset")
def reset():
    try:
        reset_targets()
        return {"ok": True, "repos": repos()}
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not reset repositories: {exc}",
        )


@app.post("/missions")
def create(mission_request: New):
    repo_name = mission_request.repo.strip()
    request_text = mission_request.request.strip()

    if not repo_name:
        raise HTTPException(status_code=400, detail="Repository is required")

    if not request_text:
        raise HTTPException(status_code=400, detail="Mission request is required")

    try:
        repo = sandbox.safe_path(TARGETS, repo_name)
    except sandbox.Blocked as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    repo_path = Path(repo)

    if not repo_path.is_dir() or repo_path.resolve() == TARGETS.resolve():
        raise HTTPException(status_code=404, detail="Unknown repository")

    mid = uuid.uuid4().hex[:8]

    mission = {
        "id": mid,
        "repo": str(repo_path),
        "request": request_text,
        "status": "running",
        "plan": None,
        "diff": "",
        "report": None,
        "iters": 0,
        "run_dir": str(RUNS / mid),
    }

    with MISSION_LOCK:
        MISSIONS[mid] = mission

    _save_missions()

    worker = threading.Thread(
        target=_work,
        args=(mission,),
        daemon=True,
    )
    worker.start()

    return {"id": mid, "status": "running"}


def _m(mid):
    with MISSION_LOCK:
        mission = MISSIONS.get(mid)

    if mission is None:
        raise HTTPException(status_code=404, detail="Unknown mission")

    return mission


@app.get("/missions/{mid}")
def get(mid: str):
    mission = _m(mid)
    return {
        key: value
        for key, value in mission.items()
        if key not in ("repo", "run_dir")
    }


@app.get("/missions/{mid}/report")
def mission_report(mid: str):
    """Return the report and proposed diff for a known mission."""
    mission = _m(mid)

    if mission.get("report") is None:
        if mission.get("status") == "running":
            raise HTTPException(
                status_code=409,
                detail="Mission is still running; report is not ready",
            )

        raise HTTPException(
            status_code=404,
            detail="No report is available for this mission",
        )

    return {
        "id": mission["id"],
        "status": mission["status"],
        "report": mission["report"],
        "diff": mission.get("diff", ""),
    }


@app.get("/missions/{mid}/events")
def stream(mid: str):
    mission = _m(mid)

    def generate():
        last_id = 0

        while True:
            rows = log.list(mid, last_id)

            for row in rows:
                last_id = row["id"]
                yield f"data: {json.dumps(row)}\n\n"

            if not rows and mission.get("status") != "running":
                yield "event: done\ndata: {}\n\n"
                return

            time.sleep(0.25)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/missions/{mid}/graph")
def mission_graph(mid: str):
    _m(mid)
    return {"mermaid": mermaid(log.list(mid))}


def _decide(mid, approved):
    mission = _m(mid)

    with MISSION_LOCK:
        if mission["status"] != "awaiting_approval":
            raise HTTPException(
                status_code=409,
                detail=f"Mission is {mission['status']}, not awaiting approval",
            )

        if approved:
            try:
                work_path = Path(mission["run_dir"]) / "work"

                if not work_path.is_dir():
                    raise HTTPException(
                        status_code=409,
                        detail="Mission workspace is missing; patch was not applied",
                    )

                tools.apply_to_repo(mission["repo"], work_path)

            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"Could not apply patch: {exc}",
                )

        log.add(
            mid,
            "human",
            "approval",
            {
                "msg": (
                    "Approved: changes applied to the repository"
                    if approved
                    else "Rejected: changes were not applied"
                )
            },
        )

        mission["status"] = "approved" if approved else "rejected"

    _save_missions()
    return {"status": mission["status"]}


@app.post("/missions/{mid}/approve")
def approve(mid: str):
    return _decide(mid, True)


@app.post("/missions/{mid}/reject")
def reject(mid: str):
    return _decide(mid, False)
