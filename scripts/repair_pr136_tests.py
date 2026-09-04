from pathlib import Path

path = Path("test_flexfactor_purpose.py")
text = path.read_text(encoding="utf-8")
old = '''    def test_local_evidence_descriptor_must_still_match_directory_entry(self):
        source = Path(self.root) / "src" / "receipt.py"
        source.parent.mkdir()
        source.write_text("trusted bytes\\n", encoding="utf-8")
        digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
        candidate = self._contract(evidence=[self._evidence(
            kind="schema", locator="src/receipt.py", content_hash=digest,
        )])
        resolved_source = source.resolve()
        real_stat = fp.os.stat

        def replaced(path, *args, **kwargs):
            observed = real_stat(path, *args, **kwargs)
            if Path(path) == resolved_source \\
                    and kwargs.get("follow_symlinks") is False:
                changed = mock.Mock(wraps=observed)
                changed.st_dev = observed.st_dev
                changed.st_ino = observed.st_ino + 1
                return changed
            return observed

        with mock.patch.object(fp.os, "stat", side_effect=replaced):
            self.assertIsNone(fp._contract_from_registry(
                candidate, evidence_root=self.root
            ))

'''
new = '''    def test_local_evidence_descriptor_must_still_match_directory_entry(self):
        source = Path(self.root) / "src" / "receipt.py"
        source.parent.mkdir()
        source.write_text("trusted bytes\\n", encoding="utf-8")
        digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
        candidate = self._contract(evidence=[self._evidence(
            kind="schema", locator="src/receipt.py", content_hash=digest,
        )])
        real_fstat = fp.os.fstat
        calls = 0

        def replaced(descriptor):
            nonlocal calls
            observed = real_fstat(descriptor)
            calls += 1
            if calls >= 3:
                changed = mock.Mock(wraps=observed)
                changed.st_dev = observed.st_dev
                changed.st_ino = observed.st_ino + 1
                return changed
            return observed

        # The pathname and handle metadata shapes legitimately differ on
        # Windows. Simulate an actual directory-entry replacement instead by
        # changing the identity seen through the independently opened handle.
        with mock.patch.object(fp.os, "fstat", side_effect=replaced):
            self.assertIsNone(fp._contract_from_registry(
                candidate, evidence_root=self.root
            ))

'''
count = text.count(old)
if count != 1:
    raise SystemExit(f"expected one descriptor race regression, found {count}")
path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
