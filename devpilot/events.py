"""Append-only SQLite event log. Events reference each other via refs."""
import json, sqlite3, threading, time

class Log:
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("""CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
          mission_id TEXT, ts REAL, agent TEXT, type TEXT, refs TEXT, payload TEXT, evidence TEXT)""")

    def add(self, mid, agent, type_, payload, refs=(), evidence=""):
        with self.lock:
            c = self.db.execute("INSERT INTO events(mission_id,ts,agent,type,refs,payload,evidence) VALUES(?,?,?,?,?,?,?)",
                                (mid, time.time(), agent, type_, json.dumps(list(refs)), json.dumps(payload), evidence))
            self.db.commit()
            return c.lastrowid

    def list(self, mid, after=0):
        with self.lock:
            rows = self.db.execute("SELECT id,ts,agent,type,refs,payload,evidence FROM events WHERE mission_id=? AND id>? ORDER BY id",
                                   (mid, after)).fetchall()
        return [dict(id=r[0], ts=r[1], agent=r[2], type=r[3], refs=json.loads(r[4]),
                     payload=json.loads(r[5]), evidence=r[6]) for r in rows]

def mermaid(events):
    """Render the event reference graph as Mermaid."""
    out = ["graph TD"]
    for e in events:
        out.append(f'  e{e["id"]}["{e["agent"]}/{e["type"]}: {e["payload"].get("msg","")[:40]}"]'.replace("\n", " "))
        out += [f"  e{r} --> e{e['id']}" for r in e["refs"]]
    return "\n".join(out)
