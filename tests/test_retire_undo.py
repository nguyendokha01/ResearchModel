"""G4.4 U1: every machine note retired into knowledge_store/removed/ leaves <kind>_<stamp>/<vault>.undo.json,
and `vault_layout.undo(map)` puts it back byte-identical (never over a file that reappeared). Temp store and vault only."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import concept_notes, concepts, config, gaps, notes, result_notes, vault_layout

V = "v"
FP = f"<!-- kb:concept-fingerprint:{'0' * 40} -->"
CONCEPT = f"---\ntype: organism\n---\n# Old\n\n{notes.AUTO_START}\n{FP}\n\n## Description\n\n- Cells.\n{notes.AUTO_END}\n\n## My notes\n"
RESULT = f"---\ntype: result\n---\n# EVD - x\n\n{notes.AUTO_START}\nbody\n{notes.AUTO_END}\n"
OLD_MAP = f"{notes.INDEX_FRONTMATTER}{notes.AUTO_START}\n{notes.INDEX_MARK}\n# Candidate gaps\n\n- item\n{notes.AUTO_END}\n"


class RetireUndoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name).resolve()  # config.rel() resolves: an 8.3 TEMP would not match
        self.assertTrue(root.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.patch = patch.multiple(config, ROOT=root, STORE=root / "store", LOGS_DIR=root / "store" / "logs",
                                    CONFIG_DIR=root / "config", VAULTS_DIR=root / "vaults")
        self.patch.start()
        self.assertTrue(config.STORE.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.vdir = config.vault_dir(V)

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def write(self, rel, text):
        path = self.vdir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path

    def maps(self, kind):
        found = list((config.STORE / "removed").glob(f"{kind}_*/{V}.undo.json"))
        self.assertEqual(len(found), 1, found)
        return found[0]

    def check_undo(self, kind, rel, text):
        """The map has from/to/sha256, undo restores the bytes, and a reappeared file is a conflict, not overwritten."""
        map_path = self.maps(kind)
        (entry,) = json.loads(map_path.read_text(encoding="utf-8"))["entries"]
        self.assertEqual((entry["action"], entry["from"]), ("retire", rel))
        self.assertTrue(entry["sha256"] and (config.ROOT / entry["to"]).is_file())
        self.assertFalse((self.vdir / rel).exists())
        self.write(rel, "reappeared")
        self.assertEqual(len(vault_layout.undo(map_path)["conflicts"]), 1)
        self.assertEqual((self.vdir / rel).read_text(encoding="utf-8"), "reappeared")  # not overwritten
        (self.vdir / rel).unlink()
        self.assertEqual(vault_layout.undo(map_path)["restored"], 1)
        self.assertEqual((self.vdir / rel).read_bytes(), text.encode("utf-8"))

    def test_concept_note_orphan(self):
        self.write("Organisms/Old.md", CONCEPT)
        self.assertEqual(concept_notes._retire_orphans(V, set()), ["Old.md"])
        self.check_undo("concept_notes", "Organisms/Old.md", CONCEPT)

    def test_vault_stub(self):
        stub = notes._stub_template("Concepts", "Clinical data")
        self.write("Concepts/Clinical data.md", stub)
        report = notes.tidy_vault(None, V, registry=concepts.Registry([]))
        self.assertEqual(len(report["moved"]), 1)
        self.check_undo("vault_stubs", "Concepts/Clinical data.md", stub)

    def test_result_note(self):
        path = self.write("Results/EVD - x.md", RESULT)
        self.assertEqual(result_notes._retire(V, set(), {"a": path}), 1)
        self.check_undo("result_notes", "Results/EVD - x.md", RESULT)

    def test_same_stamp_clash_gets_a_suffix_and_undo_restores_the_latest_first(self):
        for text in ("one", "two"):
            vault_layout.retire(V, self.write("Results/N.md", text), "k", "s")
        map_path = config.STORE / "removed" / "k_s" / f"{V}.undo.json"
        entries = json.loads(map_path.read_text(encoding="utf-8"))["entries"]
        self.assertEqual([e["to"].rsplit("/", 1)[1] for e in entries], ["N.md", "N_1.md"])
        out = vault_layout.undo(map_path)  # reversed: "two" takes the place, "one" meets it and is reported
        self.assertEqual((out["restored"], len(out["conflicts"])), (1, 1))
        self.assertEqual((self.vdir / "Results/N.md").read_text(encoding="utf-8"), "two")

    def test_an_occupied_target_is_never_overwritten(self):
        """Even when the clash check is beaten (race), the move refuses instead of copying over the earlier file."""
        kept = config.STORE / "removed" / "k_s" / V / "Results" / "N.md"
        kept.parent.mkdir(parents=True)
        kept.write_text("keep", encoding="utf-8")
        source = self.write("Results/N.md", "new")
        with patch.object(Path, "exists", return_value=False), self.assertRaises(OSError):
            vault_layout.retire(V, source, "k", "s")
        self.assertEqual((kept.read_text(encoding="utf-8"), source.read_text(encoding="utf-8")), ("keep", "new"))

    def test_old_catalogue_note(self):
        self.write("Home.md", "# Home\n")
        self.write(f"_Maps/{gaps.OLD_CATALOGUE_NOTE}.md", OLD_MAP)
        gaps.generate(V)
        gaps.write_catalogue(V)
        self.check_undo("renamed_maps", f"_Maps/{gaps.OLD_CATALOGUE_NOTE}.md", OLD_MAP)


if __name__ == "__main__":
    unittest.main()
