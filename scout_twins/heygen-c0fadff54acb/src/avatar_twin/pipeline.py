from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import shutil

from .animation import TimelineCompiler
from .audio import (
    HttpSpeechProvider,
    apply_local_voice_controls,
    file_sha256,
    secure_asset_path,
    speech_provider_from_env,
)
from .backends import AvatarRenderRequest, build_backend
from .configuration import RuntimeConfig
from .composition import VisualComposer
from .media import MediaComposer, probe_media, validate_generated_video
from .models import ValidationError, VideoProject, utc_now


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _captions(timeline: dict[str, Any]) -> str:
    blocks: list[str] = []
    for index, caption in enumerate(timeline.get("captions") or [], start=1):
        text = str(caption.get("text") or "").replace("--> ", "→ ").strip()
        if not text:
            continue
        blocks.append(
            f"{index}\n{_srt_time(float(caption['start_s']))} --> "
            f"{_srt_time(float(caption['end_s']))}\n{text}\n"
        )
    return "\n".join(blocks)


def _preview(project: VideoProject, provider: str, model: str, video_name: str) -> str:
    title = json.dumps(project.title)[1:-1].replace("<", "&lt;")
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
body{{margin:0;background:#0d1117;color:#f0f3f6;font:16px system-ui;display:grid;min-height:100vh;place-items:center}}
main{{width:min(1100px,94vw)}}video{{width:100%;max-height:78vh;background:#000;border-radius:14px}}
.meta{{color:#9da7b3;margin-top:12px}}code{{color:#7ee787}}
</style></head><body><main><h1>{title}</h1><video controls playsinline src="{video_name}"></video>
<div class="meta">Verified render from <code>{provider}</code> / <code>{model}</code>. See artifact.json for hashes and receipts.</div>
</main></body></html>"""


@dataclass(slots=True)
class AvatarVideoPipeline:
    config: RuntimeConfig
    allowed_asset_root: Path
    allow_test_backends: bool = False

    def _audio(self, project: VideoProject, output: Path) -> tuple[Path, str]:
        if project.narration_audio_path:
            source = secure_asset_path(project.narration_audio_path, self.allowed_asset_root)
            destination = output / f"authoritative-audio{source.suffix.lower()}"
            if source.resolve() != destination.resolve():
                shutil.copyfile(source, destination)
            expected = str(project.metadata.get("master_audio_hash_sha256") or "")
            if expected and file_sha256(destination) != expected:
                raise ValidationError("authoritative audio changed before avatar generation")
            return destination, "authoritative_audio" if expected else "user_audio"
        text = " ".join(scene.script.strip() for scene in project.scenes if scene.script.strip())
        if not text:
            text = project.script.strip() or project.prompt.strip()
        provider = speech_provider_from_env(
            provider_hint=project.voice.provider,
            sample_path=project.voice.sample_path,
            allowed_root=self.allowed_asset_root,
        )
        destination = output / "synthesized-speech.wav"
        provider_output = destination if isinstance(provider, HttpSpeechProvider) else output / "synthesized-speech.raw.wav"
        provider.synthesize(
            text,
            project.voice.language or project.language,
            project.voice.id or project.avatar.voice_id,
            provider_output,
            settings={
                "locale": project.voice.locale,
                "accent": project.voice.accent,
                "style": project.voice.style,
                "gender": project.voice.gender,
                "rate": project.voice.rate,
                "pitch_semitones": project.voice.pitch_semitones,
                "emotion": project.voice.emotion,
                "traits": project.voice.traits,
            },
        )
        if provider_output != destination:
            apply_local_voice_controls(
                provider_output,
                destination,
                rate=project.voice.rate,
                pitch_semitones=project.voice.pitch_semitones,
                ffmpeg=self.config.ffmpeg,
            )
        return destination, provider.__class__.__name__

    def _driving_video(self, project: VideoProject) -> Path | None:
        value = str(project.metadata.get("performance_video_path")
                    or project.metadata.get("driving_video_path") or "")
        if not value:
            return None
        video = secure_asset_path(value, self.allowed_asset_root)
        expected = str(project.metadata.get("performance_video_hash_sha256") or "")
        if expected and file_sha256(video) != expected:
            raise ValidationError("driving performance video changed before avatar generation")
        return video

    def render(
        self,
        source: VideoProject,
        output_dir: str | Path,
        *,
        provider_name: str = "",
    ) -> dict[str, Any]:
        project = source.clone()
        project.validate(require_approved=True)
        output = Path(output_dir).expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        avatar_image = secure_asset_path(project.avatar.image_path, self.allowed_asset_root)
        audio, audio_kind = self._audio(project, output)
        audio_probe = probe_media(audio, ffprobe=self.config.ffprobe)
        if audio_probe.duration_s <= 0 or not audio_probe.has_audio:
            raise ValidationError("speech/master input has no decodable audio stream")
        driving_video = self._driving_video(project)
        is_performance = bool(driving_video)
        spec = self.config.choose(
            performance=is_performance,
            requested=provider_name,
            allow_test=self.allow_test_backends,
        )
        backend = build_backend(
            spec,
            max_download_bytes=self.config.max_download_bytes,
        )
        readiness = backend.readiness()
        if not readiness.get("ready"):
            raise ValidationError(
                f"avatar provider {spec.name!r} is not operational: {readiness.get('reason') or readiness}"
            )
        expected_duration = project.duration_s if is_performance else audio_probe.duration_s
        job = AvatarRenderRequest(
            job_id=project.id.replace("project_", "")[:64],
            avatar_image=avatar_image,
            audio=audio,
            driving_video=driving_video,
            output_dir=output,
            prompt=project.prompt or project.script,
            expected_duration_s=expected_duration,
            mode="performance_transfer" if is_performance else "talking_avatar",
            options={"aspect_ratio": project.aspect_ratio, "language": project.language},
        )
        backend_artifact = backend.render(job)
        raw_probe = validate_generated_video(
            backend_artifact.video_path,
            ffprobe=self.config.ffprobe,
            ffmpeg=self.config.ffmpeg,
            expected_duration_s=expected_duration,
            source_video=driving_video,
            require_audio=False,
            require_motion=True,
        )

        timeline = TimelineCompiler().compile(project)
        project_path = project.dump(output / "project.json")
        timeline_path = output / "timeline.json"
        timeline_path.write_text(json.dumps(timeline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        captions_path = output / "captions.srt"
        captions_path.write_text(_captions(timeline), encoding="utf-8")
        composition = VisualComposer(
            allowed_asset_root=self.allowed_asset_root,
            ffmpeg=self.config.ffmpeg,
            ffprobe=self.config.ffprobe,
        ).compose(
            project,
            backend_artifact.video_path,
            output,
            expected_duration_s=expected_duration,
        )
        background_music = (
            secure_asset_path(project.background_music_path, self.allowed_asset_root)
            if project.background_music_path else None
        )
        expected_audio_hash = str(project.metadata.get("master_audio_hash_sha256") or "")
        final_video_path = output / f"video.{project.output_format}"
        assembly = MediaComposer(
            ffmpeg=self.config.ffmpeg,
            ffprobe=self.config.ffprobe,
        ).assemble(
            composition.video_path,
            audio,
            final_video_path,
            captions=captions_path if project.captions_enabled and captions_path.stat().st_size else None,
            background_music=background_music,
            background_music_volume=project.background_music_volume,
            expected_duration_s=expected_duration,
            expected_audio_sha256=expected_audio_hash,
        )
        preview_path = output / "preview.html"
        preview_path.write_text(
            _preview(project, backend_artifact.provider, backend_artifact.model, final_video_path.name),
            encoding="utf-8",
        )
        files = [
            project_path,
            timeline_path,
            captions_path,
            preview_path,
            audio,
            composition.video_path,
            Path(assembly["video_uri"]),
        ]
        artifact = {
            "schema_version": "2.0",
            "project_id": project.id,
            "created_at": utc_now(),
            "status": "completed_verified",
            "claim": "model-backed avatar video generated and independently probed",
            "provider": backend_artifact.provider,
            "model": backend_artifact.model,
            "provider_readiness": readiness,
            "provider_receipts": list(backend_artifact.receipts),
            "provider_metadata": backend_artifact.metadata,
            "audio_kind": audio_kind,
            "raw_model_probe": raw_probe.to_dict(),
            "composition": {
                "probe": composition.probe,
                "background": composition.background,
                "layouts": list(composition.layouts),
                "receipts": list(composition.receipts),
            },
            "final_probe": assembly["probe"],
            "assembly": assembly,
            "performance_timeline_fingerprint": project.metadata.get("performance_timeline_fingerprint"),
            "files": {
                path.relative_to(output).as_posix(): {
                    "sha256": file_sha256(path),
                    "bytes": path.stat().st_size,
                }
                for path in files
            },
        }
        artifact_path = output / "artifact.json"
        artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        artifact["artifact_path"] = str(artifact_path)
        artifact["video_uri"] = assembly["video_uri"]
        return artifact
