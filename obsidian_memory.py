"""FlexFactor bridge to the shared Obsidian AI Bus.

Recall project continuity before an audit/repair/model task and remember only
non-sensitive verified conclusions. The shared vault never receives credentials
or end-user/source data.
"""
from __future__ import annotations
import os, subprocess, sys
from pathlib import Path

APP="flexfactor"
VAULT=os.environ.get("AIBUS_VAULT", r"G:\\Obsidian Vault").strip() or r"G:\\Obsidian Vault"
SCRIPT=os.environ.get("OBSIDIAN_MEMORY_AIBUS_PATH", "").strip() or str(Path(VAULT) / "AI Bus" / "aibus.py")
PYTHON=os.environ.get("OBSIDIAN_MEMORY_PYTHON", "").strip() or sys.executable

def recall(query: str="continuity decisions blockers", limit: int=8):
    if not Path(SCRIPT).is_file():
        return {"ok":False,"code":"aibus_unavailable","detail":f"AI Bus engine not found at {SCRIPT}"}
    try:
        done=subprocess.run([PYTHON,SCRIPT,"recall","--limit",str(max(1,min(25,int(limit)))),APP,*str(query).split()],shell=False,capture_output=True,text=True,timeout=30,check=False)
    except (OSError,subprocess.TimeoutExpired) as exc:
        return {"ok":False,"code":"aibus_spawn_failed","detail":str(exc)}
    return {"ok":True,"results":done.stdout.strip()} if done.returncode==0 else {"ok":False,"code":"aibus_failed","detail":(done.stderr or done.stdout or f"exit {done.returncode}").strip()[:500]}

def remember(title: str, content: str, tag: str = "project"):
    heading, body = str(title or "").strip(), str(content or "").strip()
    combined = heading + "\n" + body
    blocked = ("api_key", "secret", "token", "password", "authorization", "private key")
    if not heading or not body:
        return {"ok": False, "code": "empty_memory", "detail": "A title and non-empty project lesson are required."}
    if len(combined) > 4000 or any(item in combined.lower() for item in blocked):
        return {"ok": False, "code": "unsafe_memory", "detail": "Shared memory rejects sensitive or oversized content."}
    result = run_aibus(["note", "--from", os.environ.get("OBSIDIAN_MEMORY_AGENT", APP), "--title", f"[{APP}] {heading}", "--tag", tag or "project", body])
    return {"ok": True, "title": heading, "detail": result.get("output")} if result.get("ok") else result

def startup():
    result=recall(limit=1)
    print("[obsidian-memory] recall available" if result.get("ok") else f"[obsidian-memory] unavailable: {result.get('detail',result.get('code'))}",file=sys.stderr)
    return result

if __name__=="__main__":
    result=startup()
    raise SystemExit(0)
