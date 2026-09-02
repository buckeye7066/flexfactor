from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import shutil

from .audio import secure_asset_path
from .backgrounds import background_provider_from_env
from .media import file_sha256, probe_media, validate_generated_video
from .models import Scene, ValidationError, VideoProject
from .runtime import CommandRunner


def _ffmpeg_color(value: str) -> str:
    return "0x" + value.removeprefix("#")


def _ranges(project: VideoProject, duration_s: float) -> list[tuple[float, float, str, int]]:
    if not project.scenes:
        return [(0.0, duration_s, "center", -1)]
    ranges: list[tuple[float, float, str, int]] = []
    cursor = 0.0
    last_layout = project.scenes[0].layout
    last_index = project.scenes[0].index
    for scene in project.scenes:
        start = min(duration_s, max(0.0, scene.start_s))
        end = min(duration_s, max(start, scene.end_s))
        if start > cursor:
            ranges.append((cursor, start, last_layout, last_index))
        if end > start:
            ranges.append((start, end, scene.layout, scene.index))
            cursor = end
            last_layout = scene.layout
            last_index = scene.index
        if cursor >= duration_s:
            break
    if cursor < duration_s:
        ranges.append((cursor, duration_s, last_layout, last_index))
    merged: list[tuple[float, float, str, int]] = []
    for start, end, layout, scene_index in ranges:
        if (merged and merged[-1][2:] == (layout, scene_index)
                and abs(merged[-1][1] - start) < 0.002):
            merged[-1] = (merged[-1][0], end, layout, scene_index)
        else:
            merged.append((start, end, layout, scene_index))
    return merged


def _filter_path(path: Path) -> str:
    return str(path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


@dataclass(frozen=True, slots=True)
class CompositionArtifact:
    video_path: Path
    probe: dict[str, Any]
    receipts: tuple[dict[str, Any], ...]
    background: dict[str, Any]
    layouts: tuple[dict[str, Any], ...]


@dataclass(slots=True)
class VisualComposer:
    allowed_asset_root: Path
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    runner: CommandRunner | None = None

    def _background_asset(self, project: VideoProject, output_dir: Path) -> Path | None:
        background = project.background
        if background.kind in {"image", "video"}:
            return secure_asset_path(background.path, self.allowed_asset_root)
        if background.kind == "generated":
            width, height = project.dimensions
            return background_provider_from_env().generate(
                background.prompt,
                width,
                height,
                output_dir / "generated-background.png",
            )
        return None

    @staticmethod
    def _encoding(output_format: str, *, transparent: bool) -> list[str]:
        if output_format == "webm":
            return [
                "-c:v", "libvpx-vp9",
                "-crf", "26",
                "-b:v", "0",
                "-row-mt", "1",
                "-auto-alt-ref", "0" if transparent else "1",
                "-pix_fmt", "yuva420p" if transparent else "yuv420p",
            ]
        if output_format == "mov" and transparent:
            return ["-c:v", "prores_ks", "-profile:v", "4", "-pix_fmt", "yuva444p10le"]
        if output_format in {"mp4", "mov"}:
            return [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-threads", "1", "-x264-params", "threads=1:lookahead_threads=1",
                "-pix_fmt", "yuv420p",
            ]
        raise ValidationError(f"unsupported composition format: {output_format}")

    def _background_input(
        self,
        project: VideoProject,
        asset: Path | None,
        *,
        start_s: float,
        duration_s: float,
        width: int,
        height: int,
    ) -> list[str]:
        background = project.background
        if background.kind in {"brand_color", "color"}:
            color = project.brand.background_color if background.kind == "brand_color" else background.color
            return [
                "-f", "lavfi",
                "-i", f"color=c={_ffmpeg_color(color)}:s={width}x{height}:r=24:d={duration_s:.6f}",
            ]
        if background.kind in {"image", "generated"}:
            assert asset is not None
            return ["-loop", "1", "-framerate", "24", "-i", str(asset)]
        if background.kind == "video":
            assert asset is not None
            return ["-stream_loop", "-1", "-ss", f"{start_s:.6f}", "-i", str(asset)]
        raise ValidationError(f"background input is not applicable to kind {background.kind!r}")

    @staticmethod
    def _background_filter(project: VideoProject, width: int, height: int, label: str) -> str:
        fit = project.background.fit
        color = _ffmpeg_color(project.brand.background_color)
        if fit == "cover":
            chain = (
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1"
            )
        elif fit == "contain":
            chain = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={color},setsar=1"
            )
        else:
            chain = f"scale={width}:{height},setsar=1"
        if project.background.blur > 0:
            chain += f",gblur=sigma={min(50.0, project.background.blur):.3f}"
        return f"[0:v]{chain},setpts=PTS-STARTPTS[{label}]"

    @staticmethod
    def _avatar_scale(width: int, height: int, layout: str) -> tuple[int, int, str, str]:
        if layout == "presenter_left":
            return int(width * 0.50) // 2 * 2, int(height * 0.88) // 2 * 2, "W*0.035", "H-h-H*0.025"
        if layout == "presenter_right":
            return int(width * 0.50) // 2 * 2, int(height * 0.88) // 2 * 2, "W-w-W*0.035", "H-h-H*0.025"
        return int(width * 0.62) // 2 * 2, int(height * 0.90) // 2 * 2, "(W-w)/2", "H-h-H*0.025"

    def _segment(
        self,
        project: VideoProject,
        source_video: Path,
        background_asset: Path | None,
        logo: Path | None,
        destination: Path,
        *,
        index: int,
        start_s: float,
        duration_s: float,
        layout: str,
        scene: Scene | None,
    ) -> dict[str, Any]:
        width, height = project.dimensions
        transparent = project.background.kind == "transparent"
        if transparent and project.output_format not in {"webm", "mov"}:
            raise ValidationError("transparent composition requires webm or mov output")
        # A bounded worker should not let FFmpeg scale filter/encoder threads to
        # every host CPU.  One composition already operates on a complete frame
        # graph; keeping its filter pools bounded prevents parallel render jobs
        # from exhausting process or address-space limits.
        argv = [
            self.ffmpeg, "-y", "-v", "error",
            "-filter_threads", "1", "-filter_complex_threads", "1",
        ]
        avatar_index = 0
        if not transparent:
            argv.extend(self._background_input(
                project,
                background_asset,
                start_s=start_s,
                duration_s=duration_s,
                width=width,
                height=height,
            ))
            avatar_index = 1
        argv.extend(["-ss", f"{start_s:.6f}", "-t", f"{duration_s:.6f}", "-i", str(source_video)])
        logo_index: int | None = None
        if logo:
            logo_index = avatar_index + 1
            argv.extend(["-loop", "1", "-framerate", "24", "-i", str(logo)])
        media_index: int | None = None
        media: Path | None = None
        if scene and scene.media_path:
            media = secure_asset_path(scene.media_path, self.allowed_asset_root)
            if media.suffix.lower() not in {
                ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".ppm",
                ".mp4", ".mov", ".mkv", ".webm", ".m4v",
            }:
                raise ValidationError("scene media must be an image or video")
            media_index = avatar_index + 1 + (1 if logo_index is not None else 0)
            if media.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".ppm"}:
                argv.extend(["-loop", "1", "-framerate", "24", "-i", str(media)])
            else:
                argv.extend(["-stream_loop", "-1", "-i", str(media)])

        filters: list[str] = []
        if transparent:
            filters.append(f"color=c=black@0.0:s={width}x{height}:r=24:d={duration_s:.6f},format=rgba[bg]")
        else:
            filters.append(self._background_filter(project, width, height, "bg"))

        avatar_prefix = f"[{avatar_index}:v]setpts=PTS-STARTPTS"
        if project.background.key_color:
            avatar_prefix += f",chromakey={_ffmpeg_color(project.background.key_color)}:0.16:0.08"

        if layout in {"full_bleed", "performance"} and not transparent:
            filters.append(
                avatar_prefix
                + f",scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1[avatar]"
            )
            filters.append("[bg][avatar]overlay=x=0:y=0:shortest=1:format=auto[stage0]")
        else:
            avatar_width, avatar_height, x, y = self._avatar_scale(width, height, layout)
            filters.append(
                avatar_prefix
                + f",scale={avatar_width}:{avatar_height}:force_original_aspect_ratio=decrease,setsar=1[avatar]"
            )
            filters.append(f"[bg][avatar]overlay=x={x}:y={y}:shortest=1:format=auto[stage0]")

        stage_number = 0
        final_label = "stage0"
        if media_index is not None and scene is not None:
            stage_number += 1
            media_width = max(80, int(width * scene.media_scale)) // 2 * 2
            if scene.media_position == "left":
                media_x, media_y = "W*0.035", "(H-h)/2"
            elif scene.media_position == "center":
                media_x, media_y = "(W-w)/2", "(H-h)/2"
            elif scene.media_position == "full_bleed":
                media_width, media_x, media_y = width, "0", "0"
            else:
                media_x, media_y = "W-w-W*0.035", "(H-h)/2"
            filters.append(
                f"[{media_index}:v]setpts=PTS-STARTPTS,scale={media_width}:-1:"
                "force_original_aspect_ratio=decrease[scene_media]")
            filters.append(
                f"[{final_label}][scene_media]overlay=x={media_x}:y={media_y}:"
                f"shortest=1:format=auto[stage{stage_number}]")
            final_label = f"stage{stage_number}"
        if scene and scene.title_text.strip():
            title_file = destination.parent / f"scene-title-{index:03d}.txt"
            title_file.write_text(scene.title_text.strip(), encoding="utf-8")
            stage_number += 1
            title_y = {"top": "H*0.06", "center": "(H-text_h)/2", "bottom": "H-text_h-H*0.08"}[
                scene.title_position]
            filters.append(
                f"[{final_label}]drawtext=textfile='{_filter_path(title_file)}':"
                f"fontcolor={_ffmpeg_color(scene.title_color)}:fontsize={scene.title_size}:"
                f"x=(W-text_w)/2:y={title_y}:box=1:boxcolor=black@0.35:boxborderw=12"
                f"[stage{stage_number}]")
            final_label = f"stage{stage_number}"
        accent = _ffmpeg_color(project.brand.primary_color)
        stage_number += 1
        filters.append(
            f"[{final_label}]drawbox=x=0:y=ih-10:w=iw:h=10:color={accent}@0.92:t=fill"
            f"[stage{stage_number}]")
        final_label = f"stage{stage_number}"
        if logo_index is not None:
            stage_number += 1
            filters.append(
                f"[{logo_index}:v]scale={max(64, width // 7)}:-1:force_original_aspect_ratio=decrease[logo]"
            )
            filters.append(
                f"[{final_label}][logo]overlay=W-w-W*0.025:H*0.025:shortest=1:format=auto"
                f"[stage{stage_number}]")
            final_label = f"stage{stage_number}"
        pixel_format = "yuva444p10le" if transparent and project.output_format == "mov" else (
            "yuva420p" if transparent else "yuv420p"
        )
        filters.append(f"[{final_label}]format={pixel_format}[out]")
        argv.extend([
            "-filter_complex", ";".join(filters),
            "-map", "[out]",
            "-an",
            "-t", f"{duration_s:.6f}",
            "-r", "24",
            *self._encoding(project.output_format, transparent=transparent),
        ])
        if project.output_format in {"mp4", "mov"}:
            argv.extend(["-movflags", "+faststart"])
        argv.append(str(destination))
        receipt = (self.runner or CommandRunner()).run(
            argv,
            cwd=destination.parent,
            receipt_dir=destination.parent / "receipts",
            label=f"compose-segment-{index:03d}",
            timeout_s=1200,
        )
        return receipt.to_dict()

    def compose(
        self,
        project: VideoProject,
        source_video: str | Path,
        output_dir: str | Path,
        *,
        expected_duration_s: float,
    ) -> CompositionArtifact:
        source = Path(source_video).resolve()
        output = Path(output_dir).resolve()
        output.mkdir(parents=True, exist_ok=True)
        source_probe = probe_media(source, ffprobe=self.ffprobe)
        if project.background.kind == "transparent" and not project.background.key_color and not source_probe.has_alpha:
            raise ValidationError(
                "transparent output requires an alpha-capable model artifact or background.key_color for chroma removal"
            )
        background_asset = self._background_asset(project, output)
        logo = secure_asset_path(project.brand.logo_path, self.allowed_asset_root) if project.brand.logo_path else None
        ranges = _ranges(project, expected_duration_s)
        suffix = "." + project.output_format
        segments_dir = output / "composition-segments"
        segments_dir.mkdir(parents=True, exist_ok=True)
        segments: list[Path] = []
        receipts: list[dict[str, Any]] = []
        layout_records: list[dict[str, Any]] = []
        for index, (start, end, layout, scene_index) in enumerate(ranges):
            segment = segments_dir / f"segment-{index:03d}{suffix}"
            duration = end - start
            receipts.append(self._segment(
                project,
                source,
                background_asset,
                logo,
                segment,
                index=index,
                start_s=start,
                duration_s=duration,
                layout=layout,
                scene=(project.scenes[scene_index]
                       if 0 <= scene_index < len(project.scenes) else None),
            ))
            segments.append(segment)
            layout_records.append({
                "start_s": start, "end_s": end, "layout": layout,
                "scene_index": scene_index,
            })

        destination = output / f"composed-video{suffix}"
        if len(segments) == 1:
            shutil.copyfile(segments[0], destination)
        else:
            concat_list = segments_dir / "concat.txt"
            concat_list.write_text(
                "".join(f"file '{segment.name}'\n" for segment in segments),
                encoding="utf-8",
            )
            temporary = output / f"composed-video.concat{suffix}"
            concat_receipt = (self.runner or CommandRunner()).run(
                [
                    self.ffmpeg, "-y", "-v", "error",
                    "-f", "concat", "-safe", "0", "-i", str(concat_list),
                    "-c", "copy",
                    *(["-movflags", "+faststart"] if project.output_format in {"mp4", "mov"} else []),
                    str(temporary),
                ],
                cwd=output,
                receipt_dir=output / "receipts",
                label="compose-concat",
                timeout_s=1200,
            )
            receipts.append(concat_receipt.to_dict())
            temporary.replace(destination)
        probe = validate_generated_video(
            destination,
            ffprobe=self.ffprobe,
            ffmpeg=self.ffmpeg,
            expected_duration_s=expected_duration_s,
            require_audio=False,
            require_motion=True,
        )
        background_record = {
            "kind": project.background.kind,
            "fit": project.background.fit,
            "blur": project.background.blur,
            "color": project.brand.background_color
            if project.background.kind == "brand_color" else project.background.color,
            "source_sha256": file_sha256(background_asset) if background_asset else None,
            "generated_prompt": project.background.prompt if project.background.kind == "generated" else None,
            "transparent": project.background.kind == "transparent",
            "key_color": project.background.key_color or None,
            "brand_logo_sha256": file_sha256(logo) if logo else None,
        }
        return CompositionArtifact(
            video_path=destination,
            probe=probe.to_dict(),
            receipts=tuple(receipts),
            background=background_record,
            layouts=tuple(layout_records),
        )
