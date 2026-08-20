"""Paste this after imports in flexfactor.py to enable directed orchestration.

The monolith is too large for a single Contents-API rewrite; keep this file as
the install-hook reference until flexfactor.py is patched locally.

One-liner (paste after imports in flexfactor.py):

    try:
        import flexfactor_directed as _ff_directed
        _ff_directed.install(globals())
    except Exception:
        pass

Until then, use: python flexfactor_run.py  (loads flexfactor + install + main).
"""
