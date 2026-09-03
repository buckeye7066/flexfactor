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

def startup():
    result=recall(limit=1)
    print("[obsidian-memory] recall available" if result.get("ok") else f"[obsidian-memory] unavailable: {result.get('detail',result.get('code'))}",file=sys.stderr)
    return result

if __name__=="__main__":
    result=startup()
    raise SystemExit(0)
