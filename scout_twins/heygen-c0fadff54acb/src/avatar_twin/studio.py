from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from threading import RLock
from typing import Any
import json
import os
import re
import tempfile

from .models import BrandKit, ValidationError, VideoProject, utc_now


_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")


def safe_id(value: str, label: str) -> str:
    cleaned = str(value or "").strip()
    if not _SAFE_ID.fullmatch(cleaned):
        raise ValidationError(f"{label} must be 1-96 safe identifier characters")
    return cleaned


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError(f"stored record is unreadable: {path.name}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"stored record is malformed: {path.name}")
    return value


class StudioProjectStore:
    """Durable editable projects with optimistic concurrency and revision restore."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def _project_root(self, project_id: str) -> Path:
        return self.root / safe_id(project_id, "project_id")

    def _current_path(self, project_id: str) -> Path:
        return self._project_root(project_id) / "current.json"

    def _revision_path(self, project_id: str, revision: int) -> Path:
        if revision < 1:
            raise ValidationError("revision must be positive")
        return self._project_root(project_id) / "revisions" / f"{revision:08d}.json"

    def save(self, project: VideoProject, *, expected_revision: int | None = None) -> dict[str, Any]:
        project.validate()
        project_id = safe_id(project.id, "project_id")
        with self._lock:
            current_path = self._current_path(project_id)
            prior = _read_json(current_path) if current_path.exists() else None
            prior_revision = int((prior or {}).get("revision", 0))
            if expected_revision is not None and expected_revision != prior_revision:
                raise ValidationError(
                    f"project revision conflict: expected {expected_revision}, current {prior_revision}")
            revision = prior_revision + 1
            if prior:
                project.created_at = str((prior.get("project") or {}).get("created_at")
                                         or project.created_at)
            project.updated_at = utc_now()
            record = {
                "schema_version": "1.0",
                "project_id": project_id,
                "revision": revision,
                "saved_at": project.updated_at,
                "archived": False,
                "project": project.to_dict(),
            }
            _atomic_json(self._revision_path(project_id, revision), record)
            _atomic_json(current_path, record)
            return record

    def get(self, project_id: str, *, revision: int | None = None) -> dict[str, Any]:
        path = (self._revision_path(project_id, revision)
                if revision is not None else self._current_path(project_id))
        if not path.is_file():
            raise KeyError(project_id)
        return _read_json(path)

    def list(self, *, include_archived: bool = False) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in sorted(self.root.glob("*/current.json")):
            record = _read_json(path)
            if record.get("archived") and not include_archived:
                continue
            project = record.get("project") or {}
            rows.append({
                "id": record.get("project_id"),
                "title": project.get("title"),
                "status": project.get("status"),
                "language": project.get("language"),
                "aspect_ratio": project.get("aspect_ratio"),
                "duration_s": VideoProject.from_dict(project).duration_s,
                "revision": record.get("revision"),
                "updated_at": project.get("updated_at"),
                "archived": bool(record.get("archived")),
            })
        rows.sort(key=lambda row: str(row.get("updated_at") or ""), reverse=True)
        return rows

    def revisions(self, project_id: str) -> list[dict[str, Any]]:
        root = self._project_root(project_id) / "revisions"
        rows = []
        for path in sorted(root.glob("*.json"), reverse=True):
            record = _read_json(path)
            project = record.get("project") or {}
            rows.append({
                "revision": record.get("revision"),
                "saved_at": record.get("saved_at"),
                "status": project.get("status"),
                "title": project.get("title"),
            })
        return rows

    def restore(self, project_id: str, revision: int) -> dict[str, Any]:
        historical = self.get(project_id, revision=revision)
        current = self.get(project_id)
        project = VideoProject.from_dict(historical.get("project") or {})
        return self.save(project, expected_revision=int(current.get("revision", 0)))

    def archive(self, project_id: str, *, expected_revision: int | None = None) -> dict[str, Any]:
        with self._lock:
            current_path = self._current_path(project_id)
            if not current_path.is_file():
                raise KeyError(project_id)
            record = _read_json(current_path)
            revision = int(record.get("revision", 0))
            if expected_revision is not None and expected_revision != revision:
                raise ValidationError(
                    f"project revision conflict: expected {expected_revision}, current {revision}")
            record["archived"] = True
            record["archived_at"] = utc_now()
            _atomic_json(current_path, record)
            return record


class BrandKitStore:
    """Reusable colors, logo, font, and pronunciation/translation glossary."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def _path(self, brand_id: str) -> Path:
        return self.root / f"{safe_id(brand_id, 'brand_id')}.json"

    def save(self, brand_id: str, brand: BrandKit) -> dict[str, Any]:
        brand_id = safe_id(brand_id, "brand_id")
        brand.validate()
        record = {
            "schema_version": "1.0", "id": brand_id,
            "updated_at": utc_now(), "brand": asdict(brand),
        }
        with self._lock:
            _atomic_json(self._path(brand_id), record)
        return record

    def get(self, brand_id: str) -> BrandKit:
        path = self._path(brand_id)
        if not path.is_file():
            raise KeyError(brand_id)
        return BrandKit.from_dict(_read_json(path).get("brand") or {})

    def list(self) -> list[dict[str, Any]]:
        rows = []
        for path in sorted(self.root.glob("*.json")):
            record = _read_json(path)
            brand = record.get("brand") or {}
            rows.append({
                "id": record.get("id"), "name": brand.get("name"),
                "primary_color": brand.get("primary_color"),
                "logo_path": brand.get("logo_path"),
                "glossary_terms": len(brand.get("glossary") or {}),
                "updated_at": record.get("updated_at"),
            })
        return rows
