"""G5 step 9: `retire-machine-files` (machine-only Mine/ index notes, orphan traced images), the Home line that names the
Mine/ folders, and the shared WIKI_LINK regex (table-escaped alias). Temp store and vault only."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import config, notes, vault_layout

V = "v"
PID = "p1"
IDEAS, QUESTIONS, GAPS = vault_layout.MINE_INDEX_NOTES


class WikiLinkTests(unittest.TestCase):
    def test_table_escaped_alias_keeps_no_backslash(self):
        found = notes.WIKI_LINK.findall(r"[[A\|b]] [[A|b]] [[A#h\|b]] [[A]] [[P\T|x]] [[A^blk]]")
        self.assertEqual(found, ["A", "A", "A", "A", "P\\T", "A"])

    def test_canonical_links_rename_a_table_escaped_link_and_keep_the_escape(self):
        from kbsys import concept_notes

        class Registry:  # the one call _canonical_note_links makes
            def lookup(self, name):
                return {"name": "Lyve1"} if name.casefold() == "lyve-1" else None
        out = concept_notes._canonical_note_links(r"| [[Lyve-1\|LYVE-1]] | [[Lyve-1|x]] | [[Other\|y]] |", Registry())
        self.assertEqual(out, r"| [[Lyve1\|LYVE-1]] | [[Lyve1|x]] | [[Other\|y]] |")


class RetireMachineFilesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name).resolve()
        self.patch = patch.multiple(config, ROOT=root, STORE=root / "store", LOGS_DIR=root / "store" / "logs",
                                    CONFIG_DIR=root / "config", VAULTS_DIR=root / "vaults")
        self.patch.start()
        self.vdir = config.vault_dir(V)

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def write(self, rel, text):
        path = self.vdir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path

    def tree(self):
        return {p.relative_to(config.ROOT).as_posix(): p.read_bytes() for p in sorted(config.ROOT.rglob("*"))
                if p.is_file() and p.relative_to(config.ROOT).parts[:2] not in (("store", "logs"), ("store", "removed"))}

    def images(self):
        """F1: orphan, part found, crop unavailable (moves). F2: orphan, crop available (stays). F3: embedded (stays)."""
        manifest = {"version": 1, "bundles": [
            {"object_id": f"{PID}:{t}", "kind": "figure", "crop": {"path": "c.png", "sha256": s, "status": "available"} if s else
             {"path": None, "sha256": None, "status": "unavailable"}} for t, s in (("F1", None), ("F2", "0" * 64), ("F3", None))]}
        mpath = config.STORE / "papers" / PID / "media_manifest.json"
        mpath.parent.mkdir(parents=True)
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        for name in ("F1", "F2", "F3"):
            self.write(f"_Attachments/{PID}/{name}.png", f"img {name}")
        self.write("Papers/P.md", f"---\npaper_id: {PID}\n---\n![[_Attachments/{PID}/F3.png]]\n")

    def test_dry_run_changes_nothing_and_apply_moves_only_machine_files(self):
        self.write(IDEAS, vault_layout.mine_placeholder("My Ideas"))
        self.write(QUESTIONS, vault_layout.mine_placeholder("Open Questions") + "my own question\n")  # human text stays
        self.write(GAPS, vault_layout.mine_placeholder("Knowledge Gaps").replace("\n", "\r\n"))  # a CRLF copy (as one real vault has) moves
        self.images()
        before = self.tree()
        dry = vault_layout.retire_machine_files(V, None)
        self.assertEqual(self.tree(), before)
        self.assertEqual((dry["applied"], dry["undo_map"], dry["kept_human_text"]), (False, [], [QUESTIONS]))
        want = [("mine_index", IDEAS), ("mine_index", GAPS), ("images", f"_Attachments/{PID}/F1.png")]
        self.assertEqual([(r["kind"], r["path"]) for r in dry["retire"]], want)
        self.assertTrue(all(len(r["sha256"]) == 64 for r in dry["retire"]))

        done = vault_layout.retire_machine_files(V, None, apply=True)
        self.assertEqual((done["applied"], len(done["undo_map"])), (True, 2))
        for _, rel in want:
            self.assertFalse((self.vdir / rel).exists())
        for rel in (QUESTIONS, f"_Attachments/{PID}/F2.png", f"_Attachments/{PID}/F3.png"):
            self.assertTrue((self.vdir / rel).is_file(), rel)
        for map_path in done["undo_map"]:  # undo restores every byte
            self.assertEqual(vault_layout.undo(config.ROOT / map_path)["conflicts"], [])
        self.assertEqual(self.tree(), before)

    def test_any_change_to_the_placeholder_keeps_the_note(self):
        """Regression (review of 84614a0): a custom property or a line typed inside the kb:auto block is the researcher's."""
        ideas, questions, gaps = (vault_layout.mine_placeholder(Path(rel).stem) for rel in (IDEAS, QUESTIONS, GAPS))
        self.write(IDEAS, ideas.replace("tags: [kb-index]\n", "tags: [kb-index]\nstatus: draft\n"))
        self.write(QUESTIONS, questions.replace("*Empty for now.*\n", "*Empty for now.*\nmy question typed here\n"))
        self.write(GAPS, gaps + notes.AUTO_END + "\n")  # two end markers
        out = vault_layout.retire_machine_files(V, None)
        self.assertEqual((out["retire"], out["kept_human_text"]), ([], [IDEAS, QUESTIONS, GAPS]))

    def test_home_names_the_mine_folders_and_links_no_retired_note(self):
        self.write("Mine/Ideas/Idea.md", "x")
        vault_layout._write_home(self.vdir, V, [])
        home = (self.vdir / "Home.md").read_text(encoding="utf-8")
        for name in ("Mine/Ideas/", "Mine/Questions/", "Mine/Knowledge Gaps/", "Mine/Decisions/"):
            self.assertIn(f"`{name}`", home)
        for name in ("My Ideas", "Open Questions"):
            self.assertNotIn(f"[[{name}", home)


if __name__ == "__main__":
    unittest.main()
