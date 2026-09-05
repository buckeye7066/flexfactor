"""Fail-closed guard: a TEST RUN must never reach a real, billable provider.

THE DEFECT THIS EXISTS TO PREVENT
---------------------------------
``flexfactor_cli_provider_tests.py`` made **real authenticated HTTPS requests to
the ChatGPT backend** on any host that had an exportable Codex OAuth file.
``CliProvider.__init__`` for ``api="codex-cli"`` called ``load_exportable_oauth()``
and built a live ``ChatGPTSubscriptionClient``; ``_complete`` then took the
subscription branch and **never reached ``subprocess`` at all**, so every
subprocess-level double in that suite patched a seam the call did not use.

Measured on this repo at ``e4ef8b6`` with a *fake* exportable OAuth file in a
temporary ``CODEX_HOME`` (never the owner's real credential)::

    Ran 31 tests in 7.381s
    OK
    === NETPROBE: 8 outbound attempt(s) ===
      DNS chatgpt.com:443
      CONNECT ('104.18.32.47', 443)      [x4 pairs]

Four live requests, and the suite still reported ``OK`` - because a rejected
credential is folded into ``CliUnavailable`` and the assertions never noticed.
On a host with a VALID credential those same four calls spend the owner's real
ChatGPT entitlement. CI was green only because the runner has no OAuth file,
which is the shape of a check that cannot fail.

WHY IT IS WRITTEN THIS WAY
--------------------------
1. ``LiveProviderCallBlocked`` derives from ``BaseException``, NOT ``Exception``.
   That is load-bearing and deliberate. The whole call path this guards is
   wrapped in broad handlers - ``providers.chatgpt_subscription._open`` catches
   ``(URLError, TimeoutError, OSError)`` and ``CliProvider._complete`` turns the
   result into ``CliUnavailable`` - so an ``Exception`` here would be swallowed
   and the run would go green having merely *failed* to bill. A silent skip is
   the single worst outcome available; this raises past every ``except
   Exception`` in the repo instead. ``unittest`` still records it as an error,
   so the suite fails loudly and exits non-zero.

2. IT NAMES THE SEAM THAT LEAKED. The message carries the nearest frame inside
   this repository, so the failure reads as "this line built a network-capable
   client" rather than "something, somewhere, tried to open a socket".

3. LOOPBACK IS ALLOWED. A local socket is never a billable provider call, and
   several suites legitimately talk to 127.0.0.1. Everything else is refused.

4. IT IS NOT A SKIP AND IT IS NOT A DRY RUN. Nothing here makes a provider
   pretend to answer. It only makes "this test run was about to spend money" an
   immediate, named failure instead of an invisible one.
"""
from __future__ import annotations

import contextlib
import os
import socket
import traceback
from typing import Any, Callable, Iterator, Optional


class LiveProviderCallBlocked(BaseException):
    """A test run tried to reach a real provider credential or endpoint.

    Deliberately a ``BaseException``: see the module docstring. Never catch
    this to keep a test green - fix the double instead.
    """


_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_SELF = os.path.abspath(__file__)

_LOOPBACK_HOSTS = {"localhost", "localhost.localdomain", "127.0.0.1", "::1", "0.0.0.0", "::"}


def _is_loopback(host: Any) -> bool:
    if host is None:
        return True
    text = str(host).strip().strip("[]").lower()
    if not text:
        return True
    if text in _LOOPBACK_HOSTS:
        return True
    return text.startswith("127.")


#: How many repository frames to name. One is not enough: the innermost frame
#: is the seam, and the frame that CALLED it is the test that forgot to double
#: it. A failure that names only one of the two is half a diagnosis.
_FRAMES_TO_NAME = 3


def _offending_frame() -> str:
    """The nearest repository frames, innermost first, seam then caller."""
    named = []
    for frame in reversed(traceback.extract_stack()):
        path = os.path.abspath(frame.filename)
        # `os.path.abspath` happily turns a synthetic name like
        # "<frozen runpy>" into a path under the CWD, which is usually this
        # repo. Require a file that exists, or the report names a frame that
        # is not ours.
        if path == _SELF or not path.startswith(_REPO_ROOT):
            continue
        if not os.path.isfile(path):
            continue
        named.append(
            f"{os.path.relpath(path, _REPO_ROOT)}:{frame.lineno} in {frame.name}()")
        if len(named) >= _FRAMES_TO_NAME:
            break
    return " <- ".join(named) or "<no repository frame on the stack>"


def _refuse(what: str, detail: str) -> "LiveProviderCallBlocked":
    return LiveProviderCallBlocked(
        f"BLOCKED: this test run was about to {what} ({detail}). "
        f"Leaking seam: {_offending_frame()}. "
        "A unit test must never reach a real provider - inject the double at "
        "the seam that actually serves the call (CliProvider(subscription=...) "
        "for codex-cli), not at subprocess."
    )


class _Guard:
    """Installed process-wide for the duration of a suite."""

    def __init__(self) -> None:
        self._undo: list[Callable[[], None]] = []
        self.installed = False

    # -- the two seams ----------------------------------------------------
    def _guard_sockets(self) -> None:
        real_getaddrinfo = socket.getaddrinfo
        real_connect = socket.socket.connect

        def getaddrinfo(host, port, *args, **kwargs):
            if not _is_loopback(host):
                raise _refuse("resolve a remote provider host",
                              f"getaddrinfo {host}:{port}")
            return real_getaddrinfo(host, port, *args, **kwargs)

        def connect(sock, address):
            host = address[0] if isinstance(address, tuple) and address else None
            if isinstance(address, tuple) and not _is_loopback(host):
                raise _refuse("open a remote connection", f"connect {address!r}")
            return real_connect(sock, address)

        socket.getaddrinfo = getaddrinfo
        socket.socket.connect = connect
        self._undo.append(lambda: setattr(socket, "getaddrinfo", real_getaddrinfo))
        self._undo.append(lambda: setattr(socket.socket, "connect", real_connect))

    def _guard_host_credential(self) -> None:
        from providers import chatgpt_subscription as cs

        real = cs.load_exportable_oauth

        def blocked(path=None):
            if path is not None:
                # An explicit path is a fixture the test built itself; reading
                # it is local and intentional.
                return real(path)
            raise _refuse(
                "read the HOST's real ChatGPT OAuth credential",
                f"load_exportable_oauth() -> {cs.codex_auth_path()}")

        cs.load_exportable_oauth = blocked
        self._undo.append(lambda: setattr(cs, "load_exportable_oauth", real))

    # -- lifecycle --------------------------------------------------------
    def install(self) -> None:
        if self.installed:
            return
        self._guard_sockets()
        self._guard_host_credential()
        self.installed = True

    def uninstall(self) -> None:
        while self._undo:
            self._undo.pop()()
        self.installed = False


_GUARD = _Guard()


def install() -> None:
    """Refuse remote sockets and host-credential reads for this process."""
    _GUARD.install()


def uninstall() -> None:
    _GUARD.uninstall()


@contextlib.contextmanager
def guarded() -> Iterator[None]:
    install()
    try:
        yield
    finally:
        uninstall()


@contextlib.contextmanager
def declared_subscription(client: Optional[Any]) -> Iterator[None]:
    """Declare, for one block, what the codex-cli subscription seam returns.

    ``None`` means "this test exercises the CLI subprocess path"; a fake client
    means "this test exercises the subscription path". Either way the choice is
    written down in the test instead of being decided by whether the machine
    running it happens to be signed in to ChatGPT.
    """
    from providers import cli_provider as cp

    real = cp.build_subscription_client
    cp.build_subscription_client = lambda *_a, **_k: client
    try:
        yield
    finally:
        cp.build_subscription_client = real
