from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import unittest

from avatar_twin.configuration import RuntimeConfig
from avatar_twin.models import VideoProject


def run(command: list[str], cwd: Path) -> None:
    completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise RuntimeError(completed.stderr or completed.stdout)


def create_media(root: Path, *, duration_s: float = 2.0) -> dict[str, Path]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise unittest.SkipTest("FFmpeg is not installed")
    face = root / "face.ppm"
    width, height = 32, 32
    face.write_bytes(b"P6\n32 32\n255\n" + bytes([190, 130, 95]) * width * height)
    audio = root / "master.wav"
    run([
        ffmpeg, "-y", "-v", "error", "-threads", "1", "-f", "lavfi",
        "-i", f"sine=frequency=330:sample_rate=22050:duration={duration_s}",
        "-c:a", "pcm_s16le", str(audio),
    ], root)
    driving = root / "driving.mp4"
    run([
        ffmpeg, "-y", "-v", "error", "-threads", "1", "-f", "lavfi",
        "-i", f"testsrc2=size=160x120:rate=12:duration={duration_s}",
        "-c:v", "mpeg4", "-q:v", "4", "-pix_fmt", "yuv420p", str(driving),
    ], root)
    return {"face": face, "audio": audio, "driving": driving}


def fixture_config(*, duration_s: float = 2.0, static: bool = False) -> RuntimeConfig:
    video_source = (
        f"color=c=blue:size=160x120:rate=12:duration={duration_s}"
        if static else
        f"testsrc2=size=160x120:rate=12:duration={duration_s}"
    )
    return RuntimeConfig.from_dict({
        "default_talking_provider": "fixture",
        "default_performance_provider": "fixture",
        "providers": {
            "fixture": {
                "type": "command",
                "test_only": True,
                "timeout_s": 60,
                "options": {
                    "executable": "ffmpeg",
                    "model": "test-fixture-not-production",
                    "output": "{output_dir}/model-video.mp4",
                    "args": [
                        "-y", "-v", "error", "-threads", "1",
                        "-f", "lavfi", "-i", video_source,
                        "-i", "{audio}",
                        "-map", "0:v:0", "-map", "1:a:0",
                        "-c:v", "mpeg4", "-q:v", "4", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-shortest",
                        "{output_dir}/model-video.mp4",
                    ],
                },
            },
        },
    })


def approved_project() -> VideoProject:
    return VideoProject.from_dict({
        "id": "project_verified_render",
        "title": "Verified render",
        "script": "Welcome to the avatar studio.",
        "target_duration_s": 2.0,
        "output_resolution": "480p",
        "status": "approved",
        "avatar": {
            "kind": "photo",
            "image_path": "face.ppm",
            "style": "talking_head",
            "consent": {
                "subject_name": "Fixture Subject",
                "granted": True,
                "recorded_at": "2026-09-02T00:00:00Z",
                "evidence_reference": "test fixture",
                "permitted_uses": ["avatar_video"],
            },
        },
        "narration_audio_path": "master.wav",
        "scenes": [{
            "id": "scene_verified_render",
            "index": 0,
            "start_s": 0.0,
            "duration_s": 2.0,
            "script": "Welcome to the avatar studio.",
            "caption": "Welcome to the avatar studio.",
        }],
    })
