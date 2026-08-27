"""Shared provider-capacity orchestration for concurrent FlexFactor runs."""
from __future__ import annotations
import contextlib, json, os, random, tempfile, threading, time, uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

SCHEMA=1
LEASE_TTL_S=15*60.0
WAITER_TTL_S=24*60*60.0
DEFAULT_WAIT_MAX_S=12*60*60.0

def state_path():
    root=os.environ.get("FLEXFACTOR_STATE_DIR") or os.path.join(os.path.expanduser("~"),".flexfactor")
    return os.environ.get("FLEXFACTOR_PROVIDER_CAPACITY_STATE") or os.path.join(root,"provider-capacity.json")

def _empty():
    return {"schema":SCHEMA,"next_ticket":1,"leases":{},"waiters":{},"cooldowns":{},"runtime":{"state":"running","detail":"","updated_at":0.0}}

class CapacityState:
    """Atomic JSON state protected by an OS-owned cross-process file lock."""
    def __init__(self,path=None): self.path=path or state_path()
    def read(self):
        try:
            with open(self.path,"r",encoding="utf-8") as f: data=json.load(f)
        except (OSError,json.JSONDecodeError): return _empty()
        if not isinstance(data,dict) or data.get("schema")!=SCHEMA: return _empty()
        for k in ("leases","waiters","cooldowns","runtime"):
            if not isinstance(data.get(k),dict): data[k]=_empty()[k]
        data["next_ticket"]=max(1,int(data.get("next_ticket") or 1)); return data
    def _try_lock(self,f):
        if os.name=="nt":
            import msvcrt
            try:
                f.seek(0)
                if os.fstat(f.fileno()).st_size<1: f.write(b"\0"); f.flush()
                f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1); return True
            except OSError: return False
        import fcntl
        try: fcntl.flock(f.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB); return True
        except (BlockingIOError,OSError): return False
    def _unlock(self,f):
        if os.name=="nt":
            import msvcrt
            with contextlib.suppress(OSError): f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            with contextlib.suppress(OSError): fcntl.flock(f.fileno(),fcntl.LOCK_UN)
    def _acquire(self,timeout=10.0):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)),exist_ok=True)
        deadline=time.time()+timeout; delay=.001
        while True:
            f=open(self.path+".lock","a+b")
            if self._try_lock(f): return f
            f.close()
            if time.time()>=deadline: return None
            time.sleep(delay+random.random()*delay); delay=min(.05,delay*2)
    def update(self,fn):
        lock=self._acquire()
        if lock is None: raise TimeoutError("provider capacity state lock unavailable")
        try:
            data=self.read(); result=fn(data); parent=os.path.dirname(os.path.abspath(self.path)); os.makedirs(parent,exist_ok=True)
            fd,tmp=tempfile.mkstemp(prefix="capacity-",suffix=".json",dir=parent)
            try:
                with os.fdopen(fd,"w",encoding="utf-8",newline="\n") as f:
                    json.dump(data,f,indent=1,sort_keys=True); f.write("\n"); f.flush()
                    with contextlib.suppress(OSError): os.fsync(f.fileno())
                os.replace(tmp,self.path)
            finally:
                with contextlib.suppress(OSError):
                    if os.path.exists(tmp): os.unlink(tmp)
            return result
        finally: self._unlock(lock); lock.close()

@dataclass(frozen=True)
class Lease:
    ident:str; allowance:str; app:str
class CapacityTimeout(RuntimeError): pass

class CapacityManager:
    """Fair persistent admission controller for real provider allowances."""
    def __init__(self,store=None,sleep=time.sleep,clock=time.time): self.store=store or CapacityState(); self.sleep=sleep; self.clock=clock
    @staticmethod
    def allowance(route):
        try:
            import flexfactor_rotation as r; return r.allowance_key(route)
        except Exception: return f"{getattr(route,'backend','unknown')}:{getattr(route,'cost_class','unknown')}"
    @staticmethod
    def limit(route):
        x=os.environ.get("FLEXFACTOR_PROVIDER_MAX_INFLIGHT")
        if x:
            with contextlib.suppress(ValueError): return max(1,int(x))
        c=str(getattr(route,"cost_class","")); return 1 if c in ("free-tier","subscription") else (2 if c=="local-unlimited" else 4)
    def _cleanup(self,d,now):
        for i,row in list(d.setdefault("leases",{}).items()):
            if float(row.get("expires_at") or 0)<=now: d["leases"].pop(i,None)
        for i,row in list(d.setdefault("waiters",{}).items()):
            if now-float(row.get("created_at") or 0)>WAITER_TTL_S: d["waiters"].pop(i,None)
        for k,u in list(d.setdefault("cooldowns",{}).items()):
            if float(u or 0)<=now: d["cooldowns"].pop(k,None)
    def acquire(self,route,app="flexfactor",*,timeout=None):
        timeout=float(timeout if timeout is not None else os.environ.get("FLEXFACTOR_PROVIDER_WAIT_MAX_S",DEFAULT_WAIT_MAX_S)); allowance=self.allowance(route); ident=uuid.uuid4().hex; created=self.clock(); deadline=created+max(0,timeout); delay=.05
        while True:
            now=self.clock(); out={"granted":False,"wait":.05}
            def mutate(d):
                self._cleanup(d,now); w=d.setdefault("waiters",{})
                if ident not in w:
                    t=int(d.get("next_ticket") or 1); d["next_ticket"]=t+1; w[ident]={"ticket":t,"allowance":allowance,"app":app,"created_at":created,"pid":os.getpid()}
                ticket=int(w[ident]["ticket"]); cool=float(d.setdefault("cooldowns",{}).get(allowance) or 0)
                if cool>now:
                    out["wait"]=max(.05,min(30,cool-now)); d["runtime"]={"state":"waiting-for-provider","detail":f"{app} waiting for shared allowance {allowance} cooldown","updated_at":now}; return
                active=[x for x in d.setdefault("leases",{}).values() if x.get("allowance")==allowance]
                ahead=[x for wid,x in w.items() if wid!=ident and x.get("allowance")==allowance and int(x.get("ticket") or 0)<ticket]
                if len(active)<self.limit(route) and not ahead:
                    d["leases"][ident]={"allowance":allowance,"app":app,"pid":os.getpid(),"thread":threading.get_ident(),"started_at":now,"expires_at":now+LEASE_TTL_S}; w.pop(ident,None); out["granted"]=True; d["runtime"]={"state":"running","detail":"provider capacity granted","updated_at":now}
                else: d["runtime"]={"state":"waiting-for-provider","detail":f"{app} queued for shared allowance {allowance}","updated_at":now}
            self.store.update(mutate)
            if out["granted"]: return Lease(ident,allowance,app)
            if now>=deadline: self.cancel_waiter(ident); raise CapacityTimeout(f"provider capacity wait exceeded {timeout:.0f}s for {allowance}")
            s=min(out["wait"],delay,max(0,deadline-now)); self.sleep(max(.01,s+random.random()*min(.05,s))); delay=min(5,delay*2)
    def cancel_waiter(self,ident):
        with contextlib.suppress(Exception): self.store.update(lambda d:d.setdefault("waiters",{}).pop(ident,None))
    def renew(self,lease):
        now=self.clock(); ok={"v":False}
        def mutate(d):
            row=d.setdefault("leases",{}).get(lease.ident)
            if row and row.get("allowance")==lease.allowance: row["expires_at"]=now+LEASE_TTL_S; ok["v"]=True
        try: self.store.update(mutate)
        except Exception: return False
        return ok["v"]
    def release(self,lease):
        now=self.clock()
        def mutate(d):
            self._cleanup(d,now); d.setdefault("leases",{}).pop(lease.ident,None)
            if not d["leases"] and not d.setdefault("waiters",{}): d["runtime"]={"state":"running","detail":"provider capacity available","updated_at":now}
        with contextlib.suppress(Exception): self.store.update(mutate)
    def note_outcome(self,route,outcome,retry_after_seconds=None,*,scope="pool",reset_at=None):
        if outcome not in ("rate_limited","quota_exhausted"): return
        now=self.clock(); allowance=self.allowance(route)
        if scope!="account":
            def mark(d): self._cleanup(d,now); d["runtime"]={"state":"waiting-for-provider","detail":f"{outcome}: route/pool cooldown remains scoped in rotation","updated_at":now}
            with contextlib.suppress(Exception): self.store.update(mark)
            return
        until=float(reset_at) if reset_at and reset_at>now else now+max(1,float(retry_after_seconds)) if retry_after_seconds else now+(3600 if outcome=="quota_exhausted" else 60)
        def mark(d):
            self._cleanup(d,now); cd=d.setdefault("cooldowns",{}); cd[allowance]=max(float(cd.get(allowance) or 0),until); d["runtime"]={"state":"waiting-for-provider","detail":f"{outcome}: {allowance} cooling until {until:.0f}","updated_at":now}
        with contextlib.suppress(Exception): self.store.update(mark)
    def snapshot(self):
        now=self.clock()
        def snap(d): self._cleanup(d,now); return json.loads(json.dumps(d))
        return self.store.update(snap)

_MANAGER=CapacityManager(); _INSTALLED=False; _INSTALL_LOCK=threading.Lock(); _PROVIDER_WRAP_LOCK=threading.Lock()
def manager(): return _MANAGER

def recommended_program_parallelism(requested,model_mode="free"):
    requested=max(1,int(requested or 1))
    if str(model_mode or "free").lower()!="free": return requested
    try:
        import flexfactor_rotation as r; catalog=r.load_catalog()
        if catalog is None: return 1
        state=r.StateStore().read(); now=time.time(); capacities={}
        for route in catalog.enabled():
            if not route.is_free or route.tier not in (r.FRONTIER,r.STRONG): continue
            if r._cooling(state,route.pool,now) or r._cooling(state,f"route:{route.id}",now) or r._cooling(state,f"allowance:{r.allowance_key(route)}",now): continue
            key=r.allowance_key(route); capacities[key]=max(capacities.get(key,0),CapacityManager.limit(route))
        return max(1,min(requested,sum(capacities.values()) or 1))
    except Exception: return 1

def _waitable_rotation_error(exc):
    text=f"{type(exc).__name__} {exc}".lower(); return any(x in text for x in ("no strong route available","no frontier route available","no light route available","every strong pool failed","every frontier pool failed","every light pool failed","rate limit","quota","allowance","cooling down"))

def _renewing_call(manager,lease,fn,*args,**kwargs):
    """Run a provider call while renewing its lease until completion."""
    stop=threading.Event(); interval=max(1.0,LEASE_TTL_S/3.0)
    def heartbeat():
        while not stop.wait(interval):
            if not manager.renew(lease): return
    t=threading.Thread(target=heartbeat,name="flexfactor-capacity-renew",daemon=True); t.start()
    try: return fn(*args,**kwargs)
    finally: stop.set(); t.join(timeout=.2)

def install():
    """Install capacity guards into flexfactor_rotation once per interpreter."""
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED: return
        import flexfactor_rotation as r
        prior_provider_for=r.RotatingProvider._provider_for; prior_report=r.Rotator.report; prior_init=r.RotatingProvider.__init__; prior_run=r.RotatingProvider._run
        def guarded_provider_for(self,route):
            provider=prior_provider_for(self,route)
            if getattr(provider,"_flexfactor_capacity_wrapped",False): return provider
            with _PROVIDER_WRAP_LOCK:
                if getattr(provider,"_flexfactor_capacity_wrapped",False): return provider
                for name in ("complete","structured","grade","ping"):
                    original=getattr(provider,name,None)
                    if not callable(original): continue
                    def make_guard(fn):
                        def guarded(*a,**kw):
                            app=str(getattr(self,"_purpose","") or self.rotator.app); lease=_MANAGER.acquire(route,app=app)
                            try: return _renewing_call(_MANAGER,lease,fn,*a,**kw)
                            finally: _MANAGER.release(lease)
                        return guarded
                    setattr(provider,name,make_guard(original))
                setattr(provider,"_flexfactor_capacity_wrapped",True)
            return provider
        def report(self,route,outcome,retry_after_seconds=None,now=None,scope="pool",reset_at=None):
            _MANAGER.note_outcome(route,outcome,retry_after_seconds,scope=scope,reset_at=reset_at); return prior_report(self,route,outcome,retry_after_seconds,now,scope=scope,reset_at=reset_at)
        def init(self,*a,**kw):
            prior_init(self,*a,**kw); original=getattr(self,"_on_error",None)
            if original is not None:
                def infra(route,exc):
                    outcome=r._classify(exc)
                    if outcome in ("rate_limited","quota_exhausted"):
                        scope,reset_at=r.limit_scope(exc); _MANAGER.note_outcome(route,outcome,r._retry_after(exc),scope=scope,reset_at=reset_at); return
                    return original(route,exc)
                self._on_error=infra
        def run(self,method,tier,*a,**kw):
            wait=float(os.environ.get("FLEXFACTOR_PROVIDER_WAIT_MAX_S",DEFAULT_WAIT_MAX_S)); deadline=time.time()+max(0,wait); delay=1.0
            while True:
                try: return prior_run(self,method,tier,*a,**kw)
                except r.RotationError as exc:
                    if not _waitable_rotation_error(exc) or time.time()>=deadline: raise
                    rt=(_MANAGER.snapshot().get("runtime") or {}); print(f"  [capacity] waiting for provider: {rt.get('detail') or str(exc)}"); time.sleep(min(30,delay)+random.random()*.25); delay=min(30,delay*2)
        r.RotatingProvider._provider_for=guarded_provider_for; r.Rotator.report=report; r.RotatingProvider.__init__=init; r.RotatingProvider._run=run; _INSTALLED=True
