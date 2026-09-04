from __future__ import annotations

from pathlib import Path
from textwrap import dedent, indent


def replace_region(
    text: str, start: str, end: str, replacement: str, label: str
) -> str:
    try:
        left = text.index(start)
        right = text.index(end, left)
    except ValueError as exc:
        raise SystemExit(f"{label}: structural anchor missing") from exc
    return text[:left] + replacement + text[right:]


source_path = Path("flexfactor_purpose.py")
source = source_path.read_text(encoding="utf-8")

evidence_start = (
    "def _v2_local_evidence_hashes_match(entry, evidence_root: str | None) -> bool:\n"
)
evidence_end = "\n\ndef _v2_contract_is_valid(\n"
helpers_and_evidence = dedent(
    '''\
    def _stable_handle_identity(value) -> tuple[int, int, int, int, int, int]:
        """Return identity fields that are comparable across live handles."""
        return (
            int(stat.S_IFMT(value.st_mode)),
            int(value.st_dev),
            int(value.st_ino),
            int(value.st_size),
            int(value.st_mtime_ns),
            int(value.st_ctime_ns),
        )


    def _stable_path_handle_shape(value) -> tuple[int, int]:
        """Compare pathname metadata to handle metadata without inode assumptions."""
        return int(stat.S_IFMT(value.st_mode)), int(value.st_size)


    def _contained_regular_entry(
        root: Path, relative: Path,
    ) -> tuple[Path, object] | None:
        """Resolve one contained regular file while rejecting every symlink component."""
        if relative.is_absolute() or relative.drive:
            return None
        parts = relative.parts
        if not parts or any(
            part in {"", ".", ".."} or "\\x00" in part for part in parts
        ):
            return None
        current = root
        try:
            entry = None
            for index, part in enumerate(parts):
                current = current / part
                entry = os.lstat(current)
                if stat.S_ISLNK(entry.st_mode):
                    return None
                if index < len(parts) - 1:
                    if not stat.S_ISDIR(entry.st_mode):
                        return None
                elif not stat.S_ISREG(entry.st_mode):
                    return None
            resolved = current.resolve(strict=True)
            resolved.relative_to(root)
        except (OSError, ValueError):
            return None
        return resolved, entry


    def _v2_local_evidence_hashes_match(entry, evidence_root: str | None) -> bool:
        """Bind local evidence records to stable, contained regular-file bytes."""
        records = [
            record for record in (entry.get("evidence") or [])
            if isinstance(record, dict)
            and _v2_evidence_requires_local_hash(record, evidence_root)
        ]
        if not records:
            return True
        if not evidence_root:
            return False
        try:
            root = Path(evidence_root).expanduser().resolve(strict=True)
        except (OSError, ValueError):
            return False
        if not root.is_dir():
            return False

        prepared = []
        aggregate_size = 0
        for record in records:
            locator = record.get("locator")
            if not isinstance(locator, str) or not locator.strip():
                return False
            relative = Path(locator.strip())
            prepared_entry = _contained_regular_entry(root, relative)
            if prepared_entry is None:
                return False
            candidate, candidate_info = prepared_entry
            if candidate_info.st_size > V2_LOCAL_EVIDENCE_MAX_BYTES:
                return False
            aggregate_size += candidate_info.st_size
            if aggregate_size > V2_LOCAL_EVIDENCE_TOTAL_MAX_BYTES:
                return False
            prepared.append((record, relative, candidate, candidate_info))

        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        flags |= int(getattr(os, "O_NONBLOCK", 0))
        aggregate_opened_size = 0
        aggregate_read = 0
        for record, relative, candidate, candidate_info in prepared:
            try:
                descriptor = os.open(root / relative, flags)
                try:
                    opened_before = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(opened_before.st_mode)
                        or _stable_path_handle_shape(opened_before)
                        != _stable_path_handle_shape(candidate_info)
                    ):
                        return False
                    if opened_before.st_size > V2_LOCAL_EVIDENCE_MAX_BYTES:
                        return False
                    aggregate_opened_size += opened_before.st_size
                    if aggregate_opened_size > V2_LOCAL_EVIDENCE_TOTAL_MAX_BYTES:
                        return False
                    digest = hashlib.sha256()
                    remaining = V2_LOCAL_EVIDENCE_MAX_BYTES + 1
                    total_read = 0
                    with os.fdopen(descriptor, "rb", closefd=False) as handle:
                        while remaining > 0:
                            chunk = handle.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            total_read += len(chunk)
                            aggregate_read += len(chunk)
                            remaining -= len(chunk)
                            if total_read > V2_LOCAL_EVIDENCE_MAX_BYTES:
                                return False
                            if aggregate_read > V2_LOCAL_EVIDENCE_TOTAL_MAX_BYTES:
                                return False
                            digest.update(chunk)
                    opened_after = os.fstat(descriptor)
                finally:
                    os.close(descriptor)

                current_entry = _contained_regular_entry(root, relative)
                if current_entry is None:
                    return False
                current, current_info = current_entry
                current_descriptor = os.open(root / relative, flags)
                try:
                    current_opened = os.fstat(current_descriptor)
                finally:
                    os.close(current_descriptor)
                latest_entry = _contained_regular_entry(root, relative)
                if latest_entry is None:
                    return False
                latest, latest_info = latest_entry
                if (
                    current != candidate
                    or latest != candidate
                    or not stat.S_ISREG(current_opened.st_mode)
                    or _stable_handle_identity(opened_before)
                    != _stable_handle_identity(opened_after)
                    or _stable_handle_identity(opened_after)
                    != _stable_handle_identity(current_opened)
                    or _stable_path_handle_shape(current_info)
                    != _stable_path_handle_shape(current_opened)
                    or _stable_path_handle_shape(latest_info)
                    != _stable_path_handle_shape(current_opened)
                ):
                    return False
            except (OSError, ValueError):
                return False
            if digest.hexdigest() != record.get("content_hash"):
                return False
        return True
    '''
)
source = replace_region(
    source,
    evidence_start,
    evidence_end,
    helpers_and_evidence,
    "replace evidence authority reader",
)

contract_loop_start = "    for rel in IN_REPO_CONTRACT_FILES:\n"
contract_parse_start = '        if rel.endswith(".json"):\n'
contract_body = dedent(
    '''\
    for rel in IN_REPO_CONTRACT_FILES:
        unresolved = root / rel
        try:
            authority_entry = os.lstat(unresolved)
        except FileNotFoundError:
            continue
        except OSError:
            return None, True
        if not stat.S_ISREG(authority_entry.st_mode):
            return None, True
        prepared_contract = _contained_regular_entry(root, Path(rel))
        if prepared_contract is None:
            return None, True
        path, info = prepared_contract
        if (
            _stable_path_handle_shape(info)
            != _stable_path_handle_shape(authority_entry)
        ):
            return None, True
        if info.st_size > IN_REPO_CONTRACT_MAX_BYTES:
            return None, True

        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        flags |= int(getattr(os, "O_NONBLOCK", 0))
        try:
            descriptor = os.open(unresolved, flags)
            try:
                opened_before = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened_before.st_mode)
                    or opened_before.st_size > IN_REPO_CONTRACT_MAX_BYTES
                    or _stable_path_handle_shape(opened_before)
                    != _stable_path_handle_shape(info)
                ):
                    return None, True
                with os.fdopen(descriptor, "rb", closefd=False) as fh:
                    raw = fh.read(IN_REPO_CONTRACT_MAX_BYTES + 1)
                if len(raw) > IN_REPO_CONTRACT_MAX_BYTES:
                    return None, True
                body = raw.decode("utf-8")
                opened_after = os.fstat(descriptor)
            finally:
                os.close(descriptor)

            current_entry = _contained_regular_entry(root, Path(rel))
            if current_entry is None:
                return None, True
            current, current_info = current_entry
            current_descriptor = os.open(unresolved, flags)
            try:
                current_opened = os.fstat(current_descriptor)
            finally:
                os.close(current_descriptor)
            latest_entry = _contained_regular_entry(root, Path(rel))
            if latest_entry is None:
                return None, True
            latest, latest_info = latest_entry
            if (
                current != path
                or latest != path
                or not stat.S_ISREG(current_opened.st_mode)
                or _stable_handle_identity(opened_before)
                != _stable_handle_identity(opened_after)
                or _stable_handle_identity(opened_after)
                != _stable_handle_identity(current_opened)
                or _stable_path_handle_shape(current_info)
                != _stable_path_handle_shape(current_opened)
                or _stable_path_handle_shape(latest_info)
                != _stable_path_handle_shape(current_opened)
            ):
                return None, True
        except Exception:
            return None, True
    '''
)
source = replace_region(
    source,
    contract_loop_start,
    contract_parse_start,
    indent(contract_body, "    "),
    "replace in-repo authority reader",
)
source_path.write_text(source, encoding="utf-8", newline="\n")

test_path = Path("test_flexfactor_purpose.py")
tests = test_path.read_text(encoding="utf-8")
marker = "    def test_checked_in_contract_passes_runtime_validator(self):\n"
if marker not in tests:
    raise SystemExit("test insertion anchor missing")
regression_body = dedent(
    '''\
    @unittest.skipUnless(hasattr(os, "symlink"), "symlink support required")
    def test_local_evidence_symlink_is_rejected_consistently(self):
        target = Path(self.root) / "src" / "receipt.py"
        target.parent.mkdir()
        target.write_text("trusted\\n", encoding="utf-8")
        link = Path(self.root) / "receipt-evidence"
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        digest = __import__("hashlib").sha256(target.read_bytes()).hexdigest()
        candidate = self._contract(evidence=[self._evidence(
            kind="source", locator="receipt-evidence", content_hash=digest,
        )])

        self.assertIsNone(fp._contract_from_registry(
            candidate, evidence_root=self.root
        ))

    def test_path_handle_identity_differences_do_not_reject_regular_authority(self):
        source = Path(self.root) / "src" / "receipt.py"
        source.parent.mkdir()
        source.write_text("trusted\\n", encoding="utf-8")
        digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
        contract_doc = self._contract(evidence=[self._evidence(
            kind="source", locator="src/receipt.py", content_hash=digest,
        )])
        _w(self.root, ".flexfactor-purpose.json", json.dumps(contract_doc))
        targets = {
            os.path.normcase(os.path.abspath(str(source))),
            os.path.normcase(os.path.abspath(
                os.path.join(self.root, ".flexfactor-purpose.json")
            )),
        }
        real_lstat = fp.os.lstat

        class DivergentPathStat:
            def __init__(self, original):
                self._original = original
                self.st_dev = int(original.st_dev) + 100003
                self.st_ino = int(original.st_ino) + 100019

            def __getattr__(self, name):
                return getattr(self._original, name)

            def __getitem__(self, index):
                return self._original[index]

            def __iter__(self):
                return iter(self._original)

            def __len__(self):
                return len(self._original)

        def is_target(path) -> bool:
            try:
                return os.path.normcase(
                    os.path.abspath(os.fspath(path))
                ) in targets
            except (TypeError, ValueError):
                return False

        def divergent_lstat(path, *args, **kwargs):
            value = real_lstat(path, *args, **kwargs)
            return DivergentPathStat(value) if is_target(path) else value

        with mock.patch.object(fp.os, "lstat", side_effect=divergent_lstat):
            contract, rejected = fp.find_contract_with_status(
                "Receipt Maker", self.root, registry={}
            )

        self.assertFalse(rejected)
        self.assertIsNotNone(contract)

    '''
)
tests = tests.replace(marker, indent(regression_body, "    ") + marker, 1)
test_path.write_text(tests, encoding="utf-8", newline="\n")
