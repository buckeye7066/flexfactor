"""Tests for flexfactor_ledger (stdlib unittest). Run: python test_flexfactor_ledger.py"""
from __future__ import annotations

import random
import unittest
from types import SimpleNamespace

from flexfactor_ledger import (
    COMMIT_METADATA_KEY, Chunk, ReviewLedger, chunk_patch, chunk_text,
    file_chunks_for_index, head_matches, sha256_text, split_unified_diff,
)


def _assert_reassembles(tc: unittest.TestCase, text: str, chunks: list[Chunk]) -> None:
    tc.assertEqual("".join(c.text for c in chunks), text, "reassembly must be byte-exact")
    n_lines = len(text.splitlines(keepends=True))
    expected_lines = max(n_lines, 0)
    # contiguous, 1-based, inclusive line ranges
    pos = 1
    for c in chunks:
        tc.assertEqual(c.line_start, pos)
        n = len(c.text.splitlines(keepends=True))
        tc.assertEqual(c.line_end, pos + n - 1)
        pos += n
    tc.assertEqual(pos - 1, expected_lines)
    # ids stable + continuation links
    fsha = sha256_text(text)
    for i, c in enumerate(chunks):
        tc.assertEqual(c.file_sha256, fsha)
        tc.assertEqual(c.index, i)
        tc.assertEqual(c.count, len(chunks))
        tc.assertEqual(c.id, f"{fsha[:12]}:{i}/{len(chunks)}")
        tc.assertEqual(c.sha256, sha256_text(c.text))
        tc.assertEqual(c.continuation_of, None if i == 0 else chunks[i - 1].id)


class ChunkTextTests(unittest.TestCase):
    def test_random_multiline_reassembly(self):
        rng = random.Random(1234)
        for trial in range(25):
            lines = []
            for _ in range(rng.randint(1, 400)):
                body = "".join(rng.choice("abc xyz\t{}[]") for _ in range(rng.randint(0, 120)))
                lines.append(body + rng.choice(["\n", "\n", "\r\n"]))
            if rng.random() < 0.3:
                lines.append("no trailing newline")
            text = "".join(lines)
            max_chars = rng.choice([50, 200, 1000, 5000])
            chunks = chunk_text(text, file=f"f{trial}.py", max_chars=max_chars)
            _assert_reassembles(self, text, chunks)
            for c in chunks:
                if len(c.text.splitlines(keepends=True)) > 1:
                    self.assertLessEqual(len(c.text), max_chars)

    def test_empty_file_yields_single_empty_chunk(self):
        chunks = chunk_text("", file="empty.txt")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].text, "")
        self.assertEqual((chunks[0].line_start, chunks[0].line_end), (1, 0))
        _assert_reassembles(self, "", chunks)

    def test_single_huge_line_is_its_own_chunk_not_cut(self):
        huge = "x" * 250_000 + "\n"
        text = "before\n" + huge + "after\n"
        chunks = chunk_text(text, file="big.txt", max_chars=60_000)
        _assert_reassembles(self, text, chunks)
        self.assertIn(huge, [c.text for c in chunks], "huge line must be a whole chunk")
        self.assertEqual(len(chunks), 3)

    def test_crlf_preserved(self):
        text = "a\r\nb\r\nc\r\n" * 100
        chunks = chunk_text(text, file="crlf.txt", max_chars=30)
        _assert_reassembles(self, text, chunks)
        self.assertGreater(len(chunks), 1)
        for c in chunks:
            self.assertIn("\r\n", c.text)
            self.assertFalse(c.text.endswith("\r"), "never split between \\r and \\n")

    def test_max_lines(self):
        text = "".join(f"line {i}\n" for i in range(100))
        chunks = chunk_text(text, file="ml.txt", max_lines=7)
        _assert_reassembles(self, text, chunks)
        self.assertEqual(len(chunks), 15)
        self.assertTrue(all(len(c.text.splitlines()) <= 7 for c in chunks))

    def test_ids_stable_across_calls(self):
        text = "".join(f"row {i}\n" for i in range(500))
        a = chunk_text(text, file="a", max_chars=300)
        b = chunk_text(text, file="other-name", max_chars=300)
        self.assertEqual([c.id for c in a], [c.id for c in b])

    def test_to_dict_omits_text(self):
        d = chunk_text("hi\n", file="x")[0].to_dict()
        self.assertNotIn("text", d)
        for k in ("id", "file", "file_sha256", "index", "count", "line_start",
                  "line_end", "sha256", "continuation_of"):
            self.assertIn(k, d)


SYNTHETIC_SHOW = (
    "commit 0123456789abcdef0123456789abcdef01234567\n"
    "Author:     Dev <dev@example.com>\n"
    "AuthorDate: Thu Aug 21 10:00:00 2026 -0400\n"
    "Commit:     Dev <dev@example.com>\n"
    "CommitDate: Thu Aug 21 10:00:00 2026 -0400\n"
    "\n"
    "    feat: three files\n"
    "\n"
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -1,3 +1,4 @@\n"
    " import os\n"
    "+import sys\n"
    " \n"
    " def main():\n"
    "@@ -10,2 +11,3 @@ def main():\n"
    "     pass\n"
    "+    return 0\n"
    " \n"
    "diff --git a/old/name.txt b/new/name.txt\n"
    "similarity index 90%\n"
    "rename from old/name.txt\n"
    "rename to new/name.txt\n"
    "index 3333333..4444444 100644\n"
    "--- a/old/name.txt\n"
    "+++ b/new/name.txt\n"
    "@@ -1 +1 @@\n"
    "-old\n"
    "+new\n"
    "diff --git a/assets/logo.png b/assets/logo.png\n"
    "new file mode 100644\n"
    "index 0000000..5555555\n"
    "Binary files /dev/null and b/assets/logo.png differ\n"
    "diff --git a/gone.md b/gone.md\n"
    "deleted file mode 100644\n"
    "index 6666666..0000000\n"
    "--- a/gone.md\n"
    "+++ /dev/null\n"
    "@@ -1,2 +0,0 @@\n"
    "-bye\n"
    "-bye\n"
)


class SplitUnifiedDiffTests(unittest.TestCase):
    def test_synthetic_git_show_split(self):
        pieces = split_unified_diff(SYNTHETIC_SHOW)
        keys = [k for k, _ in pieces]
        self.assertEqual(keys, [COMMIT_METADATA_KEY, "src/app.py", "new/name.txt",
                                "assets/logo.png", "gone.md"])
        self.assertEqual("".join(p for _, p in pieces), SYNTHETIC_SHOW)
        self.assertTrue(pieces[0][1].startswith("commit 0123"))
        self.assertIn("Binary files", pieces[3][1])
        self.assertIn("rename to new/name.txt", pieces[2][1])
        self.assertIn("deleted file mode", pieces[4][1])
        for _, p in pieces[1:]:
            self.assertTrue(p.startswith("diff --git "))

    def test_no_metadata_when_patch_starts_with_diff(self):
        patch = SYNTHETIC_SHOW[SYNTHETIC_SHOW.index("diff --git"):]
        pieces = split_unified_diff(patch)
        self.assertEqual(pieces[0][0], "src/app.py")
        self.assertEqual("".join(p for _, p in pieces), patch)

    def test_empty_patch(self):
        self.assertEqual(split_unified_diff(""), [])

    def test_quoted_paths(self):
        patch = 'diff --git "a/sp ace.txt" "b/sp ace.txt"\n--- "a/sp ace.txt"\n+++ "b/sp ace.txt"\n@@ -1 +1 @@\n-a\n+b\n'
        self.assertEqual(split_unified_diff(patch)[0][0], "sp ace.txt")


def _big_patch(n_files: int = 6, hunks_per_file: int = 40, lines_per_hunk: int = 60) -> str:
    rng = random.Random(99)
    parts = ["commit deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\nAuthor: X <x@y>\n\n    big\n\n"]
    for f in range(n_files):
        parts.append(f"diff --git a/pkg/mod{f}.py b/pkg/mod{f}.py\n"
                     f"index {f:07d}..{f + 1:07d} 100644\n"
                     f"--- a/pkg/mod{f}.py\n+++ b/pkg/mod{f}.py\n")
        for h in range(hunks_per_file):
            parts.append(f"@@ -{h * 100 + 1},{lines_per_hunk} +{h * 100 + 1},{lines_per_hunk} @@ def fn{h}():\n")
            for i in range(lines_per_hunk):
                sign = rng.choice([" ", "+", "-"])
                body = "".join(rng.choice("abcdefghij ()=_") for _ in range(rng.randint(10, 50)))
                parts.append(f"{sign}    {body}  # {f}-{h}-{i}\n")
    return "".join(parts)


class ChunkPatchTests(unittest.TestCase):
    def test_never_cuts_hunk_header_and_prefers_hunk_boundaries(self):
        patch = _big_patch(n_files=2, hunks_per_file=12, lines_per_hunk=30)
        chunks = chunk_patch(patch, max_chars=4_000)
        per_file: dict[str, list[Chunk]] = {}
        for c in chunks:
            per_file.setdefault(c.file, []).append(c)
        pieces = dict(split_unified_diff(patch))
        self.assertEqual(set(per_file), set(pieces))
        for key, cs in per_file.items():
            self.assertEqual("".join(c.text for c in cs), pieces[key])
            _assert_reassembles(self, pieces[key], cs)
            for c in cs:
                self.assertTrue(c.text.endswith("\n"))
                for ln in c.text.splitlines(keepends=True):
                    if ln.startswith("@@"):
                        self.assertRegex(ln, r"^@@ -\d+(,\d+)? \+\d+(,\d+)? @@")
            # every chunk after the first of a file with hunks starts on a hunk header
            if key != COMMIT_METADATA_KEY:
                for c in cs[1:]:
                    self.assertTrue(c.text.startswith("@@"),
                                    f"{c.id} does not start at a hunk boundary")
                self.assertGreater(len(cs), 1)

    def test_hunk_larger_than_budget_falls_back_to_line_boundaries(self):
        patch = _big_patch(n_files=1, hunks_per_file=1, lines_per_hunk=400)
        chunks = chunk_patch(patch, max_chars=3_000)
        piece = dict(split_unified_diff(patch))["pkg/mod0.py"]
        cs = [c for c in chunks if c.file == "pkg/mod0.py"]
        self.assertGreater(len(cs), 3)
        self.assertEqual("".join(c.text for c in cs), piece)
        self.assertEqual(sum(1 for c in cs for l in c.text.splitlines() if l.startswith("@@")), 1)

    def test_realistic_400k_patch_complete_ledger_no_text_lost(self):
        patch = _big_patch()
        self.assertGreater(len(patch), 400_000, f"fixture only {len(patch)} chars")
        chunks = chunk_patch(patch, max_chars=60_000)
        self.assertGreater(len(chunks), 7)
        per_file: dict[str, str] = {}
        for c in chunks:
            per_file[c.file] = per_file.get(c.file, "") + c.text
        self.assertEqual(per_file, dict(split_unified_diff(patch)))
        self.assertEqual("".join(c.text for c in chunks), patch)
        self.assertEqual(sum(len(c.text) for c in chunks), len(patch))
        led = ReviewLedger(baseline_sha="a" * 40, candidate_sha="b" * 40, chunks=chunks)
        self.assertEqual(len(led.missing()), len(chunks))
        self.assertEqual(set(led.summary()["missing"]), {c.id for c in chunks})
        self.assertEqual(led.chunk_ids, [c.id for c in chunks])
        self.assertEqual(len(set(led.chunk_ids)), len(chunks), "chunk ids must be unique")
        for c in chunks:
            led.record(c.id, status="clean", reviewer="r1")
        self.assertTrue(led.complete())
        ok, why = led.verdict_allowed()
        self.assertTrue(ok, why)
        self.assertEqual(led.summary()["expected"], len(chunks))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        text = "".join(f"l{i}\n" for i in range(30))
        self.chunks = chunk_text(text, file="f.py", max_chars=40)
        self.assertEqual(len(self.chunks), 3)
        self.led = ReviewLedger(baseline_sha="base", candidate_sha="cand", chunks=self.chunks)

    def test_incomplete_with_one_missing(self):
        a, b, c = self.chunks
        self.led.record(a.id, status="clean", reviewer="r")
        self.led.record(b.id, status="findings", reviewer="r", findings=[{"msg": "x"}])
        self.assertFalse(self.led.complete())
        self.assertEqual(self.led.missing(), [c.id])
        ok, why = self.led.verdict_allowed()
        self.assertFalse(ok)
        self.assertIn(c.id, why)
        s = self.led.summary()
        self.assertEqual((s["reviewed_clean"], s["reviewed_findings"], s["blocked"]), (1, 1, 0))
        self.assertEqual(s["missing"], [c.id])
        self.assertFalse(s["complete"])

    def test_complete_when_all_three_statuses_sum(self):
        a, b, c = self.chunks
        self.led.record(a.id, status="clean", reviewer="r")
        self.led.record(b.id, status="findings", reviewer="r", findings=[{"msg": "x"}])
        self.led.record(c.id, status="blocked", reviewer="r", reason="model 500")
        self.assertTrue(self.led.complete())
        self.assertEqual(self.led.missing(), [])
        s = self.led.summary()
        self.assertEqual(s["expected"], 3)
        self.assertEqual(s["reviewed_clean"] + s["reviewed_findings"] + s["blocked"], 3)
        self.assertTrue(s["complete"])
        self.assertEqual([r["status"] for r in s["chunks"]], ["clean", "findings", "blocked"])
        self.assertEqual(s["chunks"][2]["reason"], "model 500")
        # blocked → no verdict
        ok, why = self.led.verdict_allowed()
        self.assertFalse(ok)
        self.assertIn("blocked", why)
        self.assertIn("model 500", why)
        # findings carry chunk/file attribution
        fs = self.led.all_findings()
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["chunk_id"], b.id)
        self.assertEqual(fs[0]["file"], "f.py")

    def test_verdict_allowed_when_all_clean_or_findings(self):
        for c in self.chunks:
            self.led.record(c.id, status="clean", reviewer="r")
        self.assertEqual(self.led.verdict_allowed()[0], True)

    def test_bad_inputs(self):
        with self.assertRaises(ValueError):
            self.led.record("nope:0/1", status="clean", reviewer="r")
        with self.assertRaises(ValueError):
            self.led.record(self.chunks[0].id, status="approved", reviewer="r")
        self.assertEqual(self.led.missing(), [c.id for c in self.chunks])

    def test_overwrite_keeps_history(self):
        a = self.chunks[0]
        self.led.record(a.id, status="findings", reviewer="r1", findings=[{"m": 1}])
        self.led.record(a.id, status="clean", reviewer="r2")
        self.assertEqual(self.led.summary()["reviewed_clean"], 1)
        self.assertEqual(self.led.summary()["reviewed_findings"], 0)
        h = self.led.history(a.id)
        self.assertEqual(len(h), 1)
        self.assertEqual(h[0]["status"], "findings")
        self.assertEqual(h[0]["reviewer"], "r1")

    def test_json_round_trip(self):
        a, b, c = self.chunks
        self.led.record(a.id, status="clean", reviewer="r", response_sha256="abc")
        self.led.record(b.id, status="findings", reviewer="r", findings=[{"msg": "x", "line": 3}])
        self.led.record(b.id, status="clean", reviewer="r2")
        s = self.led.to_json()
        back = ReviewLedger.from_json(s)
        self.assertEqual(back.to_json(), s)
        self.assertEqual(back.summary(), self.led.summary())
        self.assertEqual(back.missing(), [c.id])
        self.assertEqual(back.history(b.id), self.led.history(b.id))
        self.assertEqual(back.verdict_allowed(), self.led.verdict_allowed())
        back.record(c.id, status="clean", reviewer="r")
        self.assertTrue(back.complete())

    def test_empty_ledger_never_allows_verdict(self):
        led = ReviewLedger(baseline_sha=None, candidate_sha=None, chunks=[])
        self.assertFalse(led.verdict_allowed()[0])


class HeadMatchesTests(unittest.TestCase):
    @staticmethod
    def _runner(rc: int, out: str, stderr: str = ""):
        calls = []

        def run(args, cwd):
            calls.append((list(args), cwd))
            return SimpleNamespace(returncode=rc, stdout=out, stderr=stderr)
        run.calls = calls
        return run

    def test_match(self):
        sha = "c" * 40
        r = self._runner(0, sha + "\n")
        ok, why = head_matches(r, "/proj", sha)
        self.assertTrue(ok, why)
        self.assertEqual(r.calls, [(["git", "rev-parse", "HEAD"], "/proj")])

    def test_short_sha_prefix_match(self):
        sha = "abcdef0123456789" + "0" * 24
        self.assertTrue(head_matches(self._runner(0, sha), "/p", sha[:12])[0])

    def test_mismatch(self):
        ok, why = head_matches(self._runner(0, "a" * 40), "/proj", "b" * 40)
        self.assertFalse(ok)
        self.assertIn("HEAD moved", why)

    def test_git_failure_never_true(self):
        ok, why = head_matches(self._runner(128, "", "fatal: not a git repository"), "/proj", "a" * 40)
        self.assertFalse(ok)
        self.assertIn("fatal", why)
        ok, why = head_matches(self._runner(0, ""), "/proj", "a" * 40)
        self.assertFalse(ok)

        def boom(args, cwd):
            raise OSError("git missing")
        ok, why = head_matches(boom, "/proj", "a" * 40)
        self.assertFalse(ok)
        self.assertIn("git missing", why)
        self.assertFalse(head_matches(self._runner(0, "a" * 40), "/p", None)[0])


class EvidenceAdapterTests(unittest.TestCase):
    def test_file_chunks_for_index(self):
        text = "".join(f"def f{i}():\n    return {i}\n" for i in range(5000))
        rows = file_chunks_for_index("src/big.py", text, max_bytes=20_000)
        self.assertGreater(len(rows), 1)
        self.assertTrue(all("text" not in r for r in rows))
        self.assertEqual([r["index"] for r in rows], list(range(len(rows))))
        self.assertTrue(all(r["file"] == "src/big.py" for r in rows))
        self.assertEqual(rows[-1]["line_end"], 10000)
        self.assertEqual(sum(r["chars"] for r in rows), len(text))
        self.assertEqual(rows[0]["file_sha256"], sha256_text(text))


if __name__ == "__main__":
    unittest.main(verbosity=2)
