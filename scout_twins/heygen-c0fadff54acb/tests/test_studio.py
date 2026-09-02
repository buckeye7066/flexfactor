from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import _bootstrap as _source_layout  # noqa: F401

from avatar_twin.models import BrandKit, ValidationError
from avatar_twin.studio import BrandKitStore, StudioProjectStore

from tests.support import approved_project


class StudioProjectStoreTests(unittest.TestCase):
    def test_project_revisions_conflicts_restore_and_archive(self):
        with TemporaryDirectory() as directory:
            store = StudioProjectStore(Path(directory) / "projects")
            project = approved_project()
            project.status = "draft"
            first = store.save(project)
            self.assertEqual(1, first["revision"])
            project.title = "Second title"
            second = store.save(project, expected_revision=1)
            self.assertEqual(2, second["revision"])
            with self.assertRaisesRegex(ValidationError, "revision conflict"):
                store.save(project, expected_revision=1)
            self.assertEqual([2, 1], [row["revision"] for row in store.revisions(project.id)])
            restored = store.restore(project.id, 1)
            self.assertEqual(3, restored["revision"])
            self.assertEqual("Verified render", restored["project"]["title"])
            archived = store.archive(project.id, expected_revision=3)
            self.assertTrue(archived["archived"])
            self.assertEqual([], store.list())
            self.assertEqual(1, len(store.list(include_archived=True)))

    def test_project_id_cannot_escape_store(self):
        with TemporaryDirectory() as directory:
            store = StudioProjectStore(directory)
            with self.assertRaises(ValidationError):
                store.get("../../outside")


class BrandKitStoreTests(unittest.TestCase):
    def test_brand_colors_logo_font_and_glossary_round_trip(self):
        with TemporaryDirectory() as directory:
            store = BrandKitStore(directory)
            record = store.save("axiom", BrandKit.from_dict({
                "name": "Axiom",
                "primary_color": "#112233",
                "secondary_color": "#445566",
                "background_color": "#000000",
                "text_color": "#ffffff",
                "font_family": "Inter",
                "logo_path": "logo.png",
                "glossary": {"Axiom": "AK-see-um", "PCR": "P C R"},
            }))
            self.assertEqual("axiom", record["id"])
            loaded = store.get("axiom")
            self.assertEqual("#112233", loaded.primary_color)
            self.assertEqual("AK-see-um", loaded.glossary["Axiom"])
            self.assertEqual(2, store.list()[0]["glossary_terms"])


if __name__ == "__main__":
    unittest.main()
