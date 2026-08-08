"""Head-to-head probe: does routing audit judging to the AUTHOR proxy tier
(glm-5.2, reached as claude-sonnet-5) return FULL findings (title/problem/fix
populated), vs the default JUDGE proxy tier (gpt-oss-20b, reached as
claude-haiku-4-5) which run-1 showed emitting sparse findings (line+severity
only)?

Calls FlexFactor's real review_file on src\\App.jsx (2.7KB; run-1 found 2
highs there) through the local fcc proxy (ANTHROPIC_BASE_URL=127.0.0.1:8082,
Bearer freecc). One structured call per tier, spaced >=7s apart to respect the
proxy throttle (PROVIDER_RATE_LIMIT=1 / 6s; back-to-back calls get zero-token
empties from NIM)."""
import os, sys, json, time

# Route through the fcc proxy (same free backend Claude Code uses). Blank the
# real SDK keys so the Anthropic SDK falls back to the Bearer AUTH_TOKEN path.
os.environ['ANTHROPIC_BASE_URL'] = 'http://127.0.0.1:8082'
os.environ['ANTHROPIC_AUTH_TOKEN'] = 'freecc'
os.environ['ANTHROPIC_API_KEY'] = ''
os.environ['OPENAI_API_KEY'] = ''
os.environ['OPENAI_BASE_URL'] = ''

sys.path.insert(0, r'C:\Users\firer\flexfactor')
import flexfactor as ff

REL = r'src\App.jsx'
INC = r'C:\Users\firer\incognito'
with open(os.path.join(INC, REL), encoding='utf-8') as fh:
    text = fh.read()
print(f"[probe] {REL}: {len(text)} chars, {text.count(chr(10))+1} lines", flush=True)

TIERS = [
    ('JUDGE  gpt-oss-20b  (claude-haiku-4-5)', 'claude-haiku-4-5'),  # run-1 default -> sparse
    ('AUTHOR glm-5.2      (claude-sonnet-5)  ', 'claude-sonnet-5'),  # the candidate fix
]


def fullness(findings):
    """Count findings whose title/problem/fix are all populated (non-empty)."""
    n = len(findings or [])
    full = 0
    for f in findings or []:
        if (f.get('title') and f.get('problem') and f.get('fix')
                and f.get('title') != 'None' and f.get('problem') != 'None'):
            full += 1
    return n, full


out = {}
for i, (label, jm) in enumerate(TIERS):
    if i:
        print(f"[probe] sleeping 7s for throttle...", flush=True)
        time.sleep(7)
    # meter=None -> _budget_guard is a no-op; make_provider is the production path.
    prov = ff.make_provider('anthropic', 'claude-sonnet-5', None, judge_model=jm)
    print(f"[probe] review_file via {label.strip()} ...", flush=True)
    try:
        findings, summary = ff.review_file(prov, REL, text)
    except Exception as exc:
        print(f"[probe]   ERROR: {exc}", flush=True)
        out[label.strip()] = {'error': str(exc)}
        continue
    n, full = fullness(findings)
    print(f"[probe]   n={n}  full(title+problem+fix)={full}", flush=True)
    for f in findings[:6]:
        print(f"[probe]     L{f.get('line')} [{f.get('severity')}] "
              f"{str(f.get('title'))[:60]!r} | {str(f.get('problem'))[:70]!r}", flush=True)
    out[label.strip()] = {
        'judge_model_id': jm, 'count': n, 'full_fields': full,
        'findings': findings, 'summary': summary,
    }

print("\n=== PROBE RESULT ===", flush=True)
print(json.dumps({k: {kk: vv for kk, vv in v.items() if kk != 'findings'}
                  for k, v in out.items()}, indent=2, default=str), flush=True)

# Save the full findings so we can inspect field population directly.
with open(r'C:\Users\firer\flexfactor\probe_tiers_out.json', 'w', encoding='utf-8') as fh:
    json.dump(out, fh, indent=2, default=str)
print("[probe] wrote probe_tiers_out.json", flush=True)
