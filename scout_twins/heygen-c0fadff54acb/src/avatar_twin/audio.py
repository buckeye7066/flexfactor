from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.request import Request, urlopen
import hashlib
import json
import os
import math
import wave

from .models import ValidationError
from .runtime import CommandRunner, require_file, resolve_executable


class SpeechProvider(Protocol):
    def synthesize(self, text: str, language: str, voice_id: str, output: Path,
                   settings: dict | None = None) -> Path: ...


def secure_asset_path(value: str, allowed_root: Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = allowed_root / candidate
    resolved = candidate.resolve()
    root = allowed_root.resolve()
    if not resolved.is_relative_to(root):
        raise ValidationError(f"asset escapes allowed root: {value}")
    if not resolved.is_file():
        raise ValidationError(f"asset does not exist: {value}")
    return resolved


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as reader:
        return reader.getnframes() / float(reader.getframerate())


def _prefix(path: Path, size: int = 12) -> bytes:
    with path.open("rb") as stream:
        return stream.read(size)


@dataclass(slots=True)
class PiperSpeechProvider:
    """Concrete local Piper CLI adapter. It fails when a real voice model is absent."""

    executable: str
    model: Path
    config: Path | None = None
    timeout_s: float = 600.0
    runner: CommandRunner = field(default_factory=CommandRunner)

    def synthesize(self, text: str, language: str, voice_id: str, output: Path,
                   settings: dict | None = None) -> Path:
        del language, voice_id
        if not text.strip():
            raise ValidationError("speech synthesis requires non-empty text")
        executable = resolve_executable(self.executable)
        model = require_file(self.model, "Piper voice model")
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        command = [executable, "--model", str(model), "--output_file", str(output)]
        if self.config:
            command.extend(["--config", str(require_file(self.config, "Piper voice config"))])
        self.runner.run(
            command,
            cwd=output.parent,
            receipt_dir=output.parent / "receipts",
            label="piper-tts",
            timeout_s=self.timeout_s,
            input_text=text,
        )
        if not output.is_file() or _prefix(output, 4) != b"RIFF":
            raise ValidationError("Piper did not produce a WAV artifact")
        return output


@dataclass(slots=True)
class HttpSpeechProvider:
    """Provider-neutral JSON TTS adapter returning WAV bytes."""

    endpoint: str
    api_key: str = ""
    timeout_s: float = 120.0
    extra_headers: dict[str, str] = field(default_factory=dict)

    def synthesize(self, text: str, language: str, voice_id: str, output: Path,
                   settings: dict | None = None) -> Path:
        body = json.dumps({
            "text": text,
            "language": language,
            "voice_id": voice_id,
            "format": "wav",
            "voice": dict(settings or {}),
        }).encode()
        headers = {"Content-Type": "application/json", "Accept": "audio/wav", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(self.endpoint, data=body, headers=headers, method="POST")
        with urlopen(request, timeout=self.timeout_s) as response:
            content_type = response.headers.get("Content-Type", "")
            payload = response.read()
        if "json" in content_type:
            parsed = json.loads(payload.decode())
            import base64
            encoded = parsed.get("audio_base64")
            if not encoded:
                raise ValidationError("speech provider returned JSON without audio_base64")
            payload = base64.b64decode(encoded, validate=True)
        if not payload.startswith(b"RIFF"):
            raise ValidationError("speech provider did not return a WAV artifact")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)
        return output


@dataclass(slots=True)
class XttsSpeechProvider:
    """Concrete Coqui XTTS-v2 CLI adapter for consented multilingual voice cloning."""

    executable: str
    speaker_wav: Path
    model_name: str = "tts_models/multilingual/multi-dataset/xtts_v2"
    use_cuda: bool = True
    timeout_s: float = 1200.0
    runner: CommandRunner = field(default_factory=CommandRunner)

    def synthesize(self, text: str, language: str, voice_id: str, output: Path,
                   settings: dict | None = None) -> Path:
        del voice_id, settings
        if not text.strip():
            raise ValidationError("voice cloning requires non-empty text")
        executable = resolve_executable(self.executable)
        speaker = require_file(self.speaker_wav, "XTTS speaker sample")
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        self.runner.run(
            [
                executable,
                "--model_name", self.model_name,
                "--text", text,
                "--speaker_wav", str(speaker),
                "--language_idx", language.split("-")[0].lower(),
                "--use_cuda", "true" if self.use_cuda else "false",
                "--out_path", str(output),
            ],
            cwd=output.parent,
            receipt_dir=output.parent / "receipts",
            label="xtts-v2",
            timeout_s=self.timeout_s,
        )
        if not output.is_file() or _prefix(output, 4) != b"RIFF":
            raise ValidationError("XTTS did not produce a WAV artifact")
        return output


def apply_local_voice_controls(
    source: Path,
    output: Path,
    *,
    rate: float,
    pitch_semitones: float,
    ffmpeg: str = "ffmpeg",
    runner: CommandRunner | None = None,
) -> Path:
    """Apply real rate and pitch controls to local TTS without a shell.

    Pitch uses an asetrate/resample/tempo chain so duration is preserved before
    the independent speaking-rate adjustment. The profile validator keeps every
    atempo factor inside FFmpeg's supported 0.5–2.0 range.
    """

    with wave.open(str(source), "rb") as reader:
        sample_rate = reader.getframerate()
    filters: list[str] = []
    if abs(pitch_semitones) > 0.001:
        factor = math.pow(2.0, pitch_semitones / 12.0)
        filters.extend([
            f"asetrate={sample_rate}*{factor:.9f}",
            f"aresample={sample_rate}",
            f"atempo={1.0 / factor:.9f}",
        ])
    if abs(rate - 1.0) > 0.001:
        filters.append(f"atempo={rate:.9f}")
    if not filters:
        if source.resolve() != output.resolve():
            import shutil
            shutil.copyfile(source, output)
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    (runner or CommandRunner()).run(
        [
            ffmpeg, "-y", "-v", "error", "-i", str(source),
            "-af", ",".join(filters),
            "-c:a", "pcm_s16le", str(output),
        ],
        cwd=output.parent,
        receipt_dir=output.parent / "receipts",
        label="voice-prosody",
        timeout_s=600,
    )
    if not output.is_file() or _prefix(output, 4) != b"RIFF":
        raise ValidationError("voice prosody processing did not produce a WAV artifact")
    return output


def speech_provider_from_env(*, provider_hint: str = "auto", sample_path: str = "",
                             allowed_root: Path | None = None) -> SpeechProvider:
    hint = provider_hint.strip().lower() or "auto"
    endpoint = os.environ.get("AVATAR_TWIN_TTS_URL", "").strip()
    if endpoint and hint in {"auto", "http", "remote"}:
        return HttpSpeechProvider(
            endpoint=endpoint,
            api_key=os.environ.get("AVATAR_TWIN_TTS_KEY", ""),
        )
    xtts_sample = sample_path or os.environ.get("AVATAR_TWIN_XTTS_SPEAKER_WAV", "").strip()
    if xtts_sample and hint in {"auto", "xtts", "xtts_v2", "voice_clone"}:
        speaker = (
            secure_asset_path(xtts_sample, allowed_root)
            if allowed_root is not None else Path(xtts_sample).expanduser().resolve()
        )
        return XttsSpeechProvider(
            executable=os.environ.get("AVATAR_TWIN_XTTS_EXECUTABLE", "tts"),
            speaker_wav=speaker,
            model_name=os.environ.get(
                "AVATAR_TWIN_XTTS_MODEL",
                "tts_models/multilingual/multi-dataset/xtts_v2",
            ),
            use_cuda=os.environ.get("AVATAR_TWIN_XTTS_CUDA", "1").strip().lower()
            not in {"0", "false", "no"},
        )
    piper_model = os.environ.get("AVATAR_TWIN_PIPER_MODEL", "").strip()
    if piper_model and hint in {"auto", "piper", "local"}:
        config = os.environ.get("AVATAR_TWIN_PIPER_CONFIG", "").strip()
        return PiperSpeechProvider(
            executable=os.environ.get("AVATAR_TWIN_PIPER_EXECUTABLE", "piper"),
            model=Path(piper_model),
            config=Path(config) if config else None,
        )
    raise ValidationError(
        "the project has text but no real speech source; provide narration_audio_path, "
        "AVATAR_TWIN_TTS_URL, AVATAR_TWIN_XTTS_SPEAKER_WAV, or AVATAR_TWIN_PIPER_MODEL"
    )
