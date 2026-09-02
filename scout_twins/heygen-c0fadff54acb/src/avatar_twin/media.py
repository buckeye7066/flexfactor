from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any
import hashlib
import json
import shutil
import subprocess

from .models import ValidationError
from .runtime import CommandRunner, resolve_executable


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _rate(value: str | None) -> float:
    if not value or value in {"0/0", "N/A"}:
        return 0.0
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return 0.0


@dataclass(frozen=True, slots=True)
class MediaProbe:
    path: str
    bytes: int
    sha256: str
    duration_s: float
    width: int
    height: int
    fps: float
    video_codec: str
    pixel_format: str
    has_alpha: bool
    video_frames: int
    has_audio: bool
    audio_codec: str
    audio_sample_rate: int
    audio_channels: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def probe_media(path: str | Path, *, ffprobe: str = "ffprobe") -> MediaProbe:
    media = Path(path).resolve()
    if not media.is_file() or media.stat().st_size < 1:
        raise ValidationError(f"media artifact does not exist or is empty: {media}")
    executable = resolve_executable(ffprobe)
    completed = subprocess.run(
        [
            executable,
            "-v", "error",
            "-count_frames",
            "-show_streams",
            "-show_format",
            "-of", "json",
            str(media),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
        check=False,
    )
    if completed.returncode != 0:
        raise ValidationError(f"ffprobe rejected {media.name}: {(completed.stderr or '').strip()}")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"ffprobe returned invalid JSON for {media.name}") from exc
    streams = list(value.get("streams") or [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    format_data = value.get("format") or {}
    duration = float(format_data.get("duration") or video.get("duration") or audio.get("duration") or 0.0)
    frames = int(video.get("nb_read_frames") or video.get("nb_frames") or 0)
    fps = _rate(video.get("avg_frame_rate") or video.get("r_frame_rate"))
    if not frames and duration > 0 and fps > 0:
        frames = int(round(duration * fps))
    pixel_format = str(video.get("pix_fmt") or "")
    has_alpha = pixel_format.startswith(("rgba", "bgra", "argb", "abgr", "yuva", "gbrap"))
    return MediaProbe(
        path=str(media),
        bytes=media.stat().st_size,
        sha256=file_sha256(media),
        duration_s=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=fps,
        video_codec=str(video.get("codec_name") or ""),
        pixel_format=pixel_format,
        has_alpha=has_alpha,
        video_frames=frames,
        has_audio=bool(audio),
        audio_codec=str(audio.get("codec_name") or ""),
        audio_sample_rate=int(audio.get("sample_rate") or 0),
        audio_channels=int(audio.get("channels") or 0),
    )


def _sample_motion_score(path: Path, *, ffmpeg: str) -> float:
    executable = resolve_executable(ffmpeg)
    completed = subprocess.run(
        [
            executable,
            "-v", "error",
            "-i", str(path),
            "-vf", "fps=4,scale=64:64",
            "-frames:v", "24",
            "-pix_fmt", "gray",
            "-f", "rawvideo",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or b"").decode("utf-8", "replace").strip()
        raise ValidationError(f"cannot decode generated video frames: {detail}")
    frame_bytes = 64 * 64
    frames = [
        completed.stdout[offset:offset + frame_bytes]
        for offset in range(0, len(completed.stdout), frame_bytes)
        if len(completed.stdout[offset:offset + frame_bytes]) == frame_bytes
    ]
    if len(frames) < 2:
        return 0.0
    scores = []
    for left, right in zip(frames, frames[1:]):
        scores.append(sum(abs(a - b) for a, b in zip(left, right)) / frame_bytes)
    return max(scores, default=0.0)


def validate_generated_video(
    path: str | Path,
    *,
    ffprobe: str = "ffprobe",
    ffmpeg: str = "ffmpeg",
    expected_duration_s: float | None = None,
    source_video: str | Path | None = None,
    require_audio: bool = False,
    require_motion: bool = True,
) -> MediaProbe:
    media = Path(path).resolve()
    probe = probe_media(media, ffprobe=ffprobe)
    if probe.bytes < 1024:
        raise ValidationError("generated video is too small to be a valid render")
    if probe.width < 64 or probe.height < 64 or probe.video_frames < 2 or probe.duration_s <= 0:
        raise ValidationError("generated artifact does not contain a usable video stream")
    if require_audio and not probe.has_audio:
        raise ValidationError("completed render has no audio stream")
    if expected_duration_s:
        tolerance = max(1.0, float(expected_duration_s) * 0.12)
        if abs(probe.duration_s - float(expected_duration_s)) > tolerance:
            raise ValidationError(
                f"generated duration {probe.duration_s:.3f}s differs from expected "
                f"{float(expected_duration_s):.3f}s by more than {tolerance:.3f}s"
            )
    if source_video:
        source = Path(source_video).resolve()
        if source.is_file() and file_sha256(source) == probe.sha256:
            raise ValidationError("provider returned the unmodified driving video")
    if require_motion:
        motion_score = _sample_motion_score(media, ffmpeg=ffmpeg)
        if motion_score < 2.0:
            raise ValidationError(
                f"generated video has no meaningful temporal visual change (score={motion_score:.3f})"
            )
    return probe


@dataclass(slots=True)
class MediaComposer:
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    runner: CommandRunner | None = None

    def assemble(
        self,
        video: str | Path,
        audio: str | Path,
        destination: str | Path,
        *,
        captions: str | Path | None = None,
        background_music: str | Path | None = None,
        background_music_volume: float = 0.12,
        expected_duration_s: float | None = None,
        expected_audio_sha256: str = "",
    ) -> dict[str, Any]:
        source_video = Path(video).resolve()
        source_audio = Path(audio).resolve()
        output = Path(destination).resolve()
        if not source_video.is_file():
            raise ValidationError(f"generated video is missing: {source_video}")
        if not source_audio.is_file():
            raise ValidationError(f"authoritative audio is missing: {source_audio}")
        actual_audio_sha = file_sha256(source_audio)
        if expected_audio_sha256 and actual_audio_sha != expected_audio_sha256:
            raise ValidationError("authoritative audio hash changed before final assembly")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.stem + ".assembling" + output.suffix)
        if temporary.exists():
            temporary.unlink()
        command = [
            self.ffmpeg,
            "-y",
            "-v", "error",
            "-i", str(source_video),
            "-i", str(source_audio),
        ]
        music_path: Path | None = None
        if background_music:
            music_path = Path(background_music).resolve()
            if not music_path.is_file():
                raise ValidationError(f"background music file is missing: {music_path}")
            if not 0.0 <= background_music_volume <= 1.0:
                raise ValidationError("background music volume must be between 0 and 1")
            command.extend(["-stream_loop", "-1", "-i", str(music_path)])
        caption_path: Path | None = None
        if captions:
            caption_path = Path(captions).resolve()
            if not caption_path.is_file():
                raise ValidationError(f"caption file is missing: {caption_path}")
            command.extend(["-i", str(caption_path)])
        command.extend(["-map", "0:v:0"])
        if music_path:
            command.extend([
                "-filter_complex",
                f"[1:a:0]volume=1.0[voice];[2:a:0]volume={background_music_volume:.6f}[music];"
                "[voice][music]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[mixed]",
                "-map", "[mixed]",
            ])
        else:
            command.extend(["-map", "1:a:0"])
        if caption_path:
            caption_input = 3 if music_path else 2
            subtitle_codec = "webvtt" if output.suffix.lower() == ".webm" else "mov_text"
            command.extend(["-map", f"{caption_input}:0", "-c:s", subtitle_codec])
        suffix = output.suffix.lower()
        source_probe = probe_media(source_video, ffprobe=self.ffprobe)
        if suffix == ".webm":
            video_options = ["-c:v", "copy"] if source_probe.video_codec in {"vp8", "vp9", "av1"} else [
                "-c:v", "libvpx-vp9", "-crf", "28", "-b:v", "0",
            ]
            audio_options = ["-c:a", "libopus", "-b:a", "160k"]
        elif suffix in {".mp4", ".mov"}:
            video_options = ["-c:v", "copy"] if source_probe.video_codec in {
                "h264", "hevc", "mpeg4", "prores", "av1",
            } else [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-threads", "1", "-x264-params", "threads=1:lookahead_threads=1",
            ]
            audio_options = ["-c:a", "aac", "-b:a", "192k"]
        else:
            raise ValidationError("final video destination must end in .mp4, .webm, or .mov")
        command.extend([
            *video_options,
            *audio_options,
            "-shortest",
        ])
        if suffix in {".mp4", ".mov"}:
            command.extend(["-movflags", "+faststart"])
        command.append(str(temporary))
        runner = self.runner or CommandRunner()
        receipt = runner.run(
            command,
            cwd=output.parent,
            receipt_dir=output.parent / "receipts",
            label="ffmpeg-assembly",
            timeout_s=900,
        )
        probe = validate_generated_video(
            temporary,
            ffprobe=self.ffprobe,
            ffmpeg=self.ffmpeg,
            expected_duration_s=expected_duration_s,
            require_audio=True,
            require_motion=True,
        )
        temporary.replace(output)
        final_probe = probe_media(output, ffprobe=self.ffprobe)
        return {
            "video_uri": str(output),
            "probe": final_probe.to_dict(),
            "source_video_sha256": file_sha256(source_video),
            "authoritative_audio_sha256": actual_audio_sha,
            "captions_sha256": file_sha256(caption_path) if caption_path else None,
            "background_music_sha256": file_sha256(music_path) if music_path else None,
            "background_music_volume": background_music_volume if music_path else None,
            "command_receipt": receipt.to_dict(),
        }


def ffmpeg_available(ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe") -> bool:
    return bool(shutil.which(ffmpeg) and shutil.which(ffprobe))
