from __future__ import annotations

from ..configuration import ProviderSpec
from ..runtime import CommandRunner
from .base import AvatarBackend
from .command import CommandAvatarBackend
from .liveportrait import LivePortraitBackend
from .musetalk import MuseTalkBackend
from .remote import RemoteWorkerBackend
from .sadtalker import SadTalkerBackend
from .wan import WanAnimateBackend, WanSpeechToVideoBackend


def build_backend(
    spec: ProviderSpec,
    *,
    runner: CommandRunner | None = None,
    max_download_bytes: int = 4 * 1024 * 1024 * 1024,
) -> AvatarBackend:
    if spec.type == "wan_animate":
        return WanAnimateBackend(spec, runner)
    if spec.type == "wan_s2v":
        return WanSpeechToVideoBackend(spec, runner)
    if spec.type == "sadtalker":
        return SadTalkerBackend(spec, runner)
    if spec.type == "musetalk":
        return MuseTalkBackend(spec, runner)
    if spec.type == "liveportrait":
        return LivePortraitBackend(spec, runner)
    if spec.type == "remote_worker":
        return RemoteWorkerBackend(spec, max_download_bytes=max_download_bytes)
    if spec.type == "command":
        return CommandAvatarBackend(spec, runner)
    raise AssertionError(f"unhandled provider type: {spec.type}")
