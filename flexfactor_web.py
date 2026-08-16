#!/usr/bin/env python3
r"""
FlexFactor web dashboard - the phone-reachable port of flexfactor_dashboard_v2.

Why this exists: the v2 dashboard is Tkinter, so it can only ever be watched
from the desk it runs on. A multi-hour audit is exactly the thing you want to
check from another room, so this serves the SAME seven questions over HTTP to
anything with a browser (in practice: the Android client apps).

It answers the identical questions, in the identical scopes, and inherits the
v2 design rules verbatim - see flexfactor_dashboard_v2's docstring:

  1. Is it alive, or has it wedged?           -> live/stalled, WITH the reason
  2. How far through the WHOLE program?       -> program bar (never the batch's)
  3. What has DURABLY landed?                 -> commits on the branch
  4. How many attempts did this take?         -> survives restarts
  5. What is it doing right now, for how long? -> current file + time on it
  6. Is anything going wrong?                 -> errors/timeouts, called out
  7. What is it costing?                      -> spend vs cap

READ-ONLY, exactly like v2: it only ever reads status.json, the run checkpoints
and `git log`. It can never disturb a running audit. The single file it writes
is its own auth token, which lives outside the audit's state.

    python flexfactor_web.py                     # 127.0.0.1:8765, local only
    python flexfactor_web.py --host 100.95.159.8 # reachable over the tailnet
    python flexfactor_web.py --print-url         # show the tokenised URL

Pure stdlib. No dependencies.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FLEX_DIR = os.path.join(os.path.expanduser("~"), ".flexfactor")
STATUS_PATH = os.path.join(FLEX_DIR, "status.json")
TOKEN_PATH = os.path.join(FLEX_DIR, "web-token.txt")
ACCESS_LOG = os.path.join(FLEX_DIR, "web-access.log")

# Inherited from flexfactor_dashboard_v2: quiet longer than this is worth
# SAYING, but it is explicitly NOT death - long fix loops legitimately go
# silent for 20+ minutes while the author model works.
STALL_S = 20 * 60

SAMPLE_EVERY_S = 5.0      # velocity sampling tick (independent of any client)
HIST_MAX = 120            # ~10 min of history at the tick above
DURABLE_TTL_S = 30.0      # durable_facts shells out to git; do not do it per-request

SEV_ORDER = ["critical", "high", "medium", "low", "info"]


def _load_dashboard_module():
    """Reuse v2's read_status/durable_facts/human_eta rather than forking them.

    v2 binds STATUS_PATH from sys.argv[1] AT IMPORT TIME, so importing it from a
    process that has its own CLI arguments would silently repoint it at, say,
    "--host". We neuter argv across the import. tkinter is imported inside v2's
    main() only, so importing the module headless is safe.
    """
    saved = sys.argv
    sys.argv = [saved[0] if saved else "flexfactor_web"]
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import flexfactor_dashboard_v2 as dash  # noqa: E402
        return dash
    finally:
        sys.argv = saved


dash = _load_dashboard_module()


# ---------------------------------------------------------------- auth

def load_or_create_token() -> str:
    """A stable token so the phone apps do not need re-pairing on restart."""
    try:
        with open(TOKEN_PATH, "r", encoding="utf-8") as fh:
            tok = fh.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    os.makedirs(FLEX_DIR, exist_ok=True)
    with open(TOKEN_PATH, "w", encoding="utf-8") as fh:
        fh.write(tok + "\n")
    try:
        os.chmod(TOKEN_PATH, 0o600)
    except OSError:
        pass
    return tok


# ------------------------------------------------- velocity + liveness

class Sampler:
    """Background poller building the velocity series and the liveness verdict.

    Sampling on a fixed tick (not per HTTP request) is load-bearing: sampling
    per-request would make the sparkline a picture of the CLIENT's poll rate
    instead of the audit's throughput, and two phones watching at once would
    each distort the other's graph.
    """

    def __init__(self, status_path: str) -> None:
        self.status_path = status_path
        self._lock = threading.Lock()
        # program -> [(t, fix_done)]. fix_done, NOT reviewed: fix_done/fix_total
        # is the PROGRAM scope in v2, and the velocity graph and the ETA must be
        # driven by the same counter as the progress bar they sit under.
        self._hist: dict[str, list[tuple[float, int]]] = {}
        self._moved_at: dict[str, float] = {}          # program -> last counter move
        self._durable: dict[str, tuple[float, dict]] = {}
        self._stop = threading.Event()

    def start(self) -> None:
        t = threading.Thread(target=self._loop, name="ff-web-sampler", daemon=True)
        t.start()

    def _loop(self) -> None:
        while not self._stop.wait(SAMPLE_EVERY_S):
            try:
                self._sample()
            except Exception:
                # A sampler crash must never take the dashboard down; the next
                # tick retries and the UI degrades to "no velocity yet".
                pass

    def _sample(self) -> None:
        """Mirrors v2's history rule exactly: record on every CHANGE, plus a
        keepalive point every 20s so a wedged run draws a visibly flat line
        rather than simply stopping (a frozen graph and an idle graph must not
        look the same)."""
        progs, _mt = dash.read_status(self.status_path)
        now = time.time()
        with self._lock:
            for p in progs:
                name = str(p.get("name") or "?")
                fix_done = int(p.get("fix_done") or 0)
                h = self._hist.setdefault(name, [])
                if not h or h[-1][1] != fix_done:
                    h.append((now, fix_done))
                    self._moved_at[name] = now
                elif now - h[-1][0] > 20:
                    h.append((now, fix_done))
                if len(h) > HIST_MAX:
                    del h[: len(h) - HIST_MAX]

    def velocity(self, name: str) -> list[float]:
        """Per-interval files/min, the series v2 sparklines."""
        with self._lock:
            h = list(self._hist.get(name, []))
        out = []
        for j in range(1, len(h)):
            dt = max(1e-6, (h[j][0] - h[j - 1][0]) / 60.0)
            out.append(max(0.0, (h[j][1] - h[j - 1][1]) / dt))
        return out

    def counters_moved_ago(self, name: str) -> float | None:
        with self._lock:
            t = self._moved_at.get(name)
        return (time.time() - t) if t else None

    def durable(self, p: dict) -> dict:
        """Cached durable_facts - it runs `git log`, too slow for every request."""
        name = str(p.get("name") or "?")
        now = time.time()
        with self._lock:
            hit = self._durable.get(name)
            if hit and (now - hit[0]) < DURABLE_TTL_S:
                return hit[1]
        facts = dash.durable_facts(p)
        with self._lock:
            self._durable[name] = (now, facts)
        return facts

    def rate_per_min(self, name: str) -> float:
        """Throughput across the whole retained window, as v2 computes it.

        Deliberately NOT a short tail average: fixes land in bursts, and a tail
        window reads 0 during any normal gap between them, which would render a
        healthy run as stalled and an ETA as infinite.
        """
        with self._lock:
            h = list(self._hist.get(name, []))
        if len(h) < 2:
            return 0.0
        dt = (h[-1][0] - h[0][0]) / 60.0
        if dt <= 0.2:                 # too short a baseline to divide by
            return 0.0
        return max(0.0, (h[-1][1] - h[0][1]) / dt)


# ------------------------------------------------------------ payload

def build_state(sampler: Sampler) -> dict:
    progs, mtime = dash.read_status(sampler.status_path)
    now = time.time()
    quiet_s = (now - mtime) if mtime else None

    out = []
    for p in progs:
        name = str(p.get("name") or "?")
        # SCOPES, and they are NOT interchangeable (v2 design rule #1 - showing
        # one where the other belongs is the misread that made a 0.6%-complete
        # program read as "100% reviewed"):
        #   PROGRAM scope = fix_done / fix_total
        #   BATCH   scope = reviewed / files_total   (files_total IS the batch)
        fix_done = int(p.get("fix_done") or 0)
        fix_total = int(p.get("fix_total") or 0)
        reviewed = int(p.get("reviewed") or 0)
        batch_n = int(p.get("files_total") or 0)
        rate = sampler.rate_per_min(name)
        moved_ago = sampler.counters_moved_ago(name)
        facts = sampler.durable(p)

        sev_raw = p.get("severity") or {}
        severity = {}
        if isinstance(sev_raw, dict):
            for k in SEV_ORDER:
                v = int(sev_raw.get(k) or 0)
                if v:
                    severity[k] = v

        # Liveness is deliberately TWO signals reported separately. v2's design
        # rule: a quiet status file is not death, so never collapse these into a
        # single boolean the reader will mistake for "dead".
        if p.get("done"):
            live = "done"
        elif quiet_s is not None and quiet_s > STALL_S:
            live = "quiet"
        else:
            live = "live"

        out.append({
            "name": name,
            "dir": p.get("dir") or "",
            "phase": p.get("phase") or "",
            "done": bool(p.get("done")),
            "liveness": live,
            # both reasons, so the UI can say WHICH signal is quiet
            "status_quiet_s": round(quiet_s) if quiet_s is not None else None,
            "counters_quiet_s": round(moved_ago) if moved_ago is not None else None,
            "stall_threshold_s": STALL_S,

            # 2. PROGRAM scope - the honest denominator
            "fix_done": fix_done,
            "fix_total": fix_total,
            "program_pct": (round(100.0 * fix_done / fix_total, 1)
                            if fix_total else None),
            "eta": dash.human_eta(fix_done, fix_total, rate),

            # 3. BATCH scope - named, so it can never be misread as the program
            "reviewed": reviewed,
            "batch_n": batch_n,

            # 4. defects, scope-qualified
            "defects": int(p.get("defects") or 0),
            "defects_fixed": int(p.get("defects_fixed") or 0),
            "fixed": int(p.get("fixed") or 0),
            "errors": int(p.get("errors") or 0),
            "severity": severity,

            # 5. durable - the only numbers a restart cannot fake
            "attempts": facts.get("attempts") or 0,
            "resumes": facts.get("resumes") or 0,
            "landed": facts.get("landed"),

            # 6. cost
            "cost": float(p.get("cost") or 0.0),
            "cap": float(p.get("cap") or 0.0),

            # 7. what it is doing right now
            "current_file": p.get("current_file") or "",
            "cycles": p.get("cycles"),

            # Exact final-run proof: repository summary, purpose map, gates,
            # function/workflow execution, blast radius, and trace artifacts.
            "evidence": p.get("evidence") if isinstance(p.get("evidence"), dict) else {},

            "rate_per_min": round(rate, 2),
            "velocity": [round(v, 2) for v in sampler.velocity(name)],
        })

    return {
        "now": now,
        "status_mtime": mtime or None,
        "status_quiet_s": round(quiet_s) if quiet_s is not None else None,
        "programs": out,
        "host": os.environ.get("COMPUTERNAME") or "pc",
    }


# --------------------------------------------------------------- HTTP

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0b0f14">
<title>FlexFactor</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:#0b0f14;color:#e6edf3;
 font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
 padding:env(safe-area-inset-top) 12px calc(24px + env(safe-area-inset-bottom))}
h1{font-size:17px;margin:14px 0 4px;letter-spacing:.3px}
.sub{color:#7d8590;font-size:12px;margin-bottom:14px}
.card{background:#131a22;border:1px solid #243040;border-radius:14px;
 padding:14px;margin-bottom:14px}
.row{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.name{font-size:17px;font-weight:600}
.pill{font-size:11px;font-weight:700;padding:3px 9px;border-radius:999px;
 text-transform:uppercase;letter-spacing:.5px;white-space:nowrap}
.live{background:#12331c;color:#3fb950}.quiet{background:#3a2d0c;color:#d29922}
.done{background:#16304d;color:#58a6ff}
.lbl{color:#7d8590;font-size:11px;text-transform:uppercase;letter-spacing:.6px;
 margin:14px 0 5px}
.bar{height:9px;background:#1c2530;border-radius:999px;overflow:hidden}
.bar>i{display:block;height:100%;border-radius:999px;transition:width .4s}
.n{font-variant-numeric:tabular-nums}
.big{font-size:22px;font-weight:650}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:6px}
.dim{color:#7d8590;font-size:12px}
.file{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;
 color:#58a6ff;word-break:break-all}
svg{display:block;width:100%;height:38px}
.sev{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.sev span{font-size:11px;padding:2px 8px;border-radius:999px;background:#1c2530}
.err{color:#f85149;font-weight:600}
.ok{color:#3fb950;font-weight:600}
.warnbox{background:#3a1d1d;border:1px solid #f85149;color:#ffb4ae;
 padding:9px 11px;border-radius:10px;font-size:12px;margin-top:10px}
.empty{text-align:center;color:#7d8590;padding:44px 12px}
</style></head><body>
<h1>FlexFactor</h1>
<div class="sub" id="sub">connecting...</div>
<div id="app"></div>
<script>
var TOKEN = new URLSearchParams(location.search).get("t") || "";
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,
  function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];});}
function dur(s){ if(s==null) return "?";
  if(s<90) return Math.round(s)+"s";
  if(s<5400) return (s/60).toFixed(0)+"m";
  return (s/3600).toFixed(1)+"h"; }
function bar(frac,color){
  var pct=Math.max(0,Math.min(1,frac||0))*100;
  return '<div class="bar"><i style="width:'+pct.toFixed(1)+'%;background:'+color+'"></i></div>';
}
function spark(v){
  if(!v||v.length<2) return '<div class="dim">collecting velocity…</div>';
  var max=Math.max.apply(null,v)||1, w=100, n=v.length, pts=[];
  for(var i=0;i<n;i++){
    pts.push((i/(n-1)*w).toFixed(2)+","+(34-(v[i]/max)*30).toFixed(2));
  }
  return '<svg viewBox="0 0 100 38" preserveAspectRatio="none">'+
    '<polyline fill="none" stroke="#58a6ff" stroke-width="1.4" '+
    'vector-effect="non-scaling-stroke" points="'+pts.join(" ")+'"/></svg>';
}
function card(p){
  var h="";
  var cls = p.liveness==="done"?"done":(p.liveness==="quiet"?"quiet":"live");
  var lab = p.liveness==="done"?"done":(p.liveness==="quiet"?"quiet":"live");
  h+='<div class="card">';
  h+='<div class="row"><div class="name">'+esc(p.name)+'</div>'+
     '<div class="pill '+cls+'">'+lab+'</div></div>';
  h+='<div class="dim">'+esc(p.phase||"—")+'</div>';

  // 1. liveness, WITH which signal is quiet (never a bare "dead")
  if(p.liveness==="quiet"){
    h+='<div class="warnbox">status.json quiet '+dur(p.status_quiet_s)+
       ' (threshold '+dur(p.stall_threshold_s)+'). Counters last moved '+
       dur(p.counters_quiet_s)+' ago. A long fix loop can legitimately be '+
       'silent this long — this is not proof it died.</div>';
  }

  // 2. PROGRAM scope — fix_done/fix_total, never the batch's numbers
  h+='<div class="lbl">Program — whole program</div>';
  h+='<div class="row"><span class="big n">'+
     (p.program_pct==null?"—":p.program_pct+"%")+'</span>'+
     '<span class="dim n">'+p.fix_done+' / '+p.fix_total+' files</span></div>';
  h+=bar(p.fix_total?p.fix_done/p.fix_total:0,"#58a6ff");
  if(p.eta) h+='<div class="dim" style="margin-top:4px">'+esc(p.eta)+'</div>';

  // velocity — driven by the same counter as the bar above it
  h+='<div class="lbl">Velocity — '+p.rate_per_min+' files/min</div>'+spark(p.velocity);

  // 3. BATCH scope, explicitly named so it cannot be read as the program
  if(p.batch_n){
    h+='<div class="lbl">Current batch only — not the program</div>';
    h+='<div class="row"><span class="n">'+p.reviewed+' / '+p.batch_n+
       ' files reviewed</span></div>';
    h+=bar(p.reviewed/p.batch_n,"#3fb950");
  }

  // 4. defects, scope-qualified against what was actually reviewed
  h+='<div class="lbl">Defects — found in '+p.reviewed+' files reviewed</div>';
  h+='<div class="row"><span class="n">'+p.defects+' found · '+
     p.defects_fixed+' fixed</span>'+
     (p.errors?'<span class="err n">'+p.errors+' errors</span>':'')+'</div>';
  if(Object.keys(p.severity||{}).length){
    h+='<div class="sev">';
    for(var k in p.severity){ h+='<span>'+k+' '+p.severity[k]+'</span>'; }
    h+='</div>';
  }

  // 5. durable
  h+='<div class="lbl">Durable (survives restarts)</div>';
  h+='<div class="grid"><div><div class="big n">'+
     (p.landed==null?"—":p.landed)+'</div><div class="dim">commits landed</div></div>'+
     '<div><div class="big n">'+p.attempts+'</div><div class="dim">attempts'+
     (p.resumes?' · '+p.resumes+' resumes':'')+'</div></div></div>';

  // 6. cost
  h+='<div class="lbl">Cost</div>';
  h+='<div class="row"><span class="n">$'+p.cost.toFixed(2)+'</span>'+
     '<span class="dim n">cap $'+p.cap.toFixed(2)+'</span></div>';
  h+=bar(p.cap?p.cost/p.cap:0, p.cap&&p.cost/p.cap>0.8?"#d29922":"#3fb950");

  // Exact evidence views. Compact on mobile, but every primary proof surface
  // remains named and its denominator stays visible.
  var e=p.evidence||{}, g=e.gates||{}, c=e.coverage||{}, r=e.repository||{},
      im=e.impact||{}, pu=e.purpose||{};
  if(Object.keys(e).length){
    h+='<div class="lbl">Executable evidence</div>'+
       '<div class="row"><span class="n">gates '+(g.pass||0)+' pass · '+
       (g.fail||0)+' fail · '+(g.blocked||0)+' blocked</span>'+
       '<span class="'+(g.passed?'ok':'err')+'">'+(g.passed?'VERIFIED':'INCOMPLETE')+'</span></div>'+
       '<div class="dim">Repo '+(r.files||0)+' files · '+(r.functions||0)+' functions · purpose '+
       (pu.confidence||'unknown')+' ('+(pu.nodes||0)+' nodes)</div>'+
       '<div class="dim">Coverage '+(c.functions_executed||0)+'/'+(c.functions||0)+' functions · '+
       (c.routes_executed||0)+'/'+(c.routes||0)+' routes · '+
       (c.controls_executed||0)+'/'+(c.controls||0)+' controls</div>'+
       '<div class="dim">Impact '+(im.affected_files||0)+' files · '+(im.tests||0)+' tests · commit '+
       esc((e.final_commit||'').slice(0,12)||'—')+'</div>';
  }

  // 7. right now
  if(p.current_file){
    h+='<div class="lbl">Working on</div><div class="file">'+
       esc(p.current_file)+'</div>';
  }
  h+='</div>';
  return h;
}
function tick(){
  fetch("/api/state?t="+encodeURIComponent(TOKEN),{cache:"no-store"})
   .then(function(r){ if(!r.ok) throw new Error("HTTP "+r.status); return r.json(); })
   .then(function(d){
     document.getElementById("sub").textContent =
       d.host+" · status "+(d.status_quiet_s==null?"never seen":
       "updated "+dur(d.status_quiet_s)+" ago");
     var a=document.getElementById("app");
     if(!d.programs.length){
       a.innerHTML='<div class="empty">No active programs.<br>'+
         '<span style="font-size:12px">FlexFactor writes status.json when a run starts.</span></div>';
       return;
     }
     a.innerHTML = d.programs.map(card).join("");
   })
   .catch(function(e){
     document.getElementById("sub").textContent = "offline — "+e.message;
   });
}
tick(); setInterval(tick, 5000);
document.addEventListener("visibilitychange", function(){
  if(!document.hidden) tick();
});
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "FlexFactorWeb/1.0"
    token = ""
    sampler: Sampler = None  # type: ignore[assignment]

    def log_message(self, fmt, *args):
        """Append peer + request to an access log.

        The stdlib default writes to stderr, which is invisible for a
        windowless background process. Having the peer address on disk is what
        makes "the phone reached the server" an observation rather than an
        inference.
        """
        try:
            line = "{} {} {}\n".format(
                time.strftime("%Y-%m-%d %H:%M:%S"),
                self.client_address[0],
                (fmt % args) if args else fmt,
            )
            with open(ACCESS_LOG, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass  # logging must never take the dashboard down

    def _authorized(self) -> bool:
        supplied = ""
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            supplied = auth[7:].strip()
        if not supplied:
            _, _, qs = self.path.partition("?")
            for part in qs.split("&"):
                k, _, v = part.partition("=")
                if k == "t":
                    from urllib.parse import unquote
                    supplied = unquote(v)
                    break
        return bool(supplied) and hmac.compare_digest(supplied, self.token)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # phone navigated away mid-response; not an error worth noise

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]

        if path == "/healthz":
            # Deliberately unauthenticated and contentless: lets the phone app
            # distinguish "server down" from "wrong token" without leaking state.
            self._send(200, b'{"ok":true}', "application/json")
            return

        if not self._authorized():
            self._send(401, b'{"error":"bad or missing token"}', "application/json")
            return

        if path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/api/state":
            payload = json.dumps(build_state(self.sampler)).encode("utf-8")
            self._send(200, payload, "application/json")
            return

        self._send(404, b'{"error":"not found"}', "application/json")


def main() -> int:
    ap = argparse.ArgumentParser(description="FlexFactor web dashboard (read-only)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; use the Tailscale IP to reach phones")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--status", default=STATUS_PATH)
    ap.add_argument("--print-url", action="store_true",
                    help="print the tokenised URL and exit")
    args = ap.parse_args()

    token = load_or_create_token()
    url = "http://{}:{}/?t={}".format(args.host, args.port, token)
    if args.print_url:
        print(url)
        return 0

    sampler = Sampler(args.status)
    sampler.start()

    Handler.token = token
    Handler.sampler = sampler

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    print("[flexfactor-web] serving {}".format(url))
    print("[flexfactor-web] read-only; token in {}".format(TOKEN_PATH))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[flexfactor-web] stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
