"""kb.py image-audit: every image gets a class and, when it has no address, a cause (scripts/kbsys/image_audit.py)."""
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from kbsys import config, image_audit, notes, result_notes
from kbsys.conform import Vault

PID = "p1"


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def sha(data):
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def bundle(token, kind="figure", data=None):
    crop = {"path": "crops/x.png", "sha256": sha(data), "status": "available"} if data else {"path": None, "sha256": None, "status": "unavailable"}
    return {"object_id": f"{PID}:{token}", "kind": kind, "crop": crop}


class ImageAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="demo3-audit-")
        self.root = Path(self.tmp.name)
        self.papers = self.root / "papers"
        self.vdir = self.root / "v"

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, bundles, images, embeds=(), readings=None, manifest=True):
        for name, data in images.items():
            put(self.vdir / "_Attachments" / PID / name, data)
        body = "".join(f"![[_Attachments/{PID}/{n}|640]]\n" for n in embeds)
        files = [("Papers/Chen 2020.md", f"---\npaper_id: {PID}\n---\n# Chen\n{body}"), ("Concepts/A.md", "# A\n")]
        if manifest is True:
            put(self.papers / PID / "media_manifest.json", json.dumps({"version": 1, "bundles": bundles}))
        elif manifest:
            put(self.papers / PID / "media_manifest.json", manifest)
        for local, reading in (readings or {}).items():
            put(self.papers / PID / "figure_readings" / f"{local}.json", json.dumps(reading))
        vault = Vault("v", self.vdir, notes=files)
        return {Path(r["path"]).name: r for r in image_audit.run([vault], self.papers)}

    def test_classes_and_causes(self):
        rows = self.build(
            [bundle("F1", data="a"), bundle("F2", data="b"), bundle("T1", "table", "c"), bundle("FS1", data="d"), bundle("F4"), bundle("F5", data="w"),
             bundle("M1", "formula", "m")],
            {"F1.png": "a", "F1copy.png": "a", "F2.png": "b", "T1.png": "c", "FS1.png": "d", "F4.png": "stale preview", "F5.png": "w",
             "M1.png": "m", "other.png": "nothing matches"},
            embeds=["F1.png", "F2.png", "T1.png", "FS1.png", "M1.png", "unknown.png"],
            readings={"F5": {"crop_wrong": True, "note": "title strip only"}})
        self.assertEqual([rows[n]["class"] for n in ("F1.png", "F2.png", "T1.png", "FS1.png", "M1.png", "F5.png")],
                         ["figure", "figure", "table", "supplement", "source_media", "wrong_crop"])
        self.assertEqual(rows["F1.png"]["part"], f"{PID}:F1")
        self.assertEqual(rows["F1.png"]["referenced_by"], ["Papers/Chen 2020.md"])
        self.assertIsNone(rows["F1.png"]["cause"])
        clone = rows["F1copy.png"]  # unreferenced, byte-identical to an embedded image
        self.assertEqual((clone["class"], clone["cause"], clone["twin"]), ("orphan", "clone", f"_Attachments/{PID}/F1.png"))
        self.assertIn("candidate", clone["action"])
        trace = rows["F4.png"]  # no twin, name matches an object
        self.assertEqual((trace["cause"], trace["paper_id"], trace["part"], trace["node_to_recheck"]), ("trace", PID, f"{PID}:F4", "Papers/Chen 2020.md"))
        lost = rows["other.png"]
        self.assertEqual((lost["cause"], lost["part"], lost["node_to_recheck"]), ("trace", "unmatched", "unmatched"))

    def test_sha_match_without_twin_names_the_part(self):
        rows = self.build([bundle("F3", data="x")], {"renamed.png": "x"})
        self.assertEqual((rows["renamed.png"]["cause"], rows["renamed.png"]["part"]), ("trace", f"{PID}:F3"))

    def test_unreadable_manifest_does_not_crash(self):
        for manifest in (False, "{not json", "[]"):
            rows = self.build([], {"F1.png": "a", "F2.png": "b"}, embeds=["F2.png"], manifest=manifest)
            self.assertEqual((rows["F1.png"]["class"], rows["F1.png"]["cause"]), ("orphan", "unreadable"))
            self.assertEqual(rows["F2.png"]["class"], "referenced_unknown")
            shutil.rmtree(self.vdir)
            shutil.rmtree(self.papers, ignore_errors=True)

    def test_audit_writes_nothing(self):
        self.build([bundle("F1", data="a")], {"F1.png": "a", "F2.png": "a", "F9.png": "z"}, embeds=["F1.png"])

        def tree():
            return {p.relative_to(self.root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(self.root.rglob("*")) if p.is_file()}
        before = tree()
        out = self.root / "out.json"
        vault = Vault("v", self.vdir, notes=[("Concepts/A.md", "# A\n")])
        rows = image_audit.run([vault], self.papers, out)
        self.assertEqual({k: v for k, v in tree().items() if k != "out.json"}, before)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["rows"], rows)
        self.assertIn("images without an address", image_audit.summary(rows))


    def test_embedded_image_is_never_a_clone(self):
        rows = self.build([bundle("F2", data="b")], {"F2.png": "a", "extra.png": "a"}, embeds=["F2.png", "extra.png"])
        self.assertEqual((rows["extra.png"]["class"], rows["extra.png"]["cause"]), ("referenced_unknown", "trace"))
        self.assertIsNone(rows["extra.png"]["twin"])

    def test_old_crop_wrong_reading_does_not_mark_a_new_crop(self):
        rows = self.build([bundle("F5", data="new")], {"F5.png": "new"}, embeds=["F5.png"],
                          readings={"F5": {"crop_wrong": True, "note": "n", "crop_sha256": sha("old")}})
        self.assertEqual(rows["F5.png"]["class"], "figure")
        rows = self.build([bundle("F5", data="new")], {"F5.png": "new"}, embeds=["F5.png"],
                          readings={"F5": {"crop_wrong": True, "note": "n", "crop_sha256": sha("new")}})
        self.assertEqual(rows["F5.png"]["class"], "wrong_crop")

    def test_researcher_images_are_not_candidates(self):
        put(self.vdir / "Mine" / "pic.png", "a")
        rows = self.build([bundle("F1", data="a")], {"F1.png": "a"}, embeds=["F1.png"])
        self.assertEqual((rows["pic.png"]["class"], rows["pic.png"]["cause"], rows["pic.png"]["twin"]), ("orphan", "human", None))
        self.assertNotIn("delete", rows["pic.png"]["action"])

    def test_out_refuses_data_folders_and_foreign_files(self):
        vault = Vault("v", self.vdir, notes=[("Concepts/A.md", "# A\n")])
        for bad in (config.VAULTS_DIR / "x" / "audit.json", config.STORE / "audit.json"):
            with self.assertRaises(ValueError):
                image_audit.run([vault], self.papers, bad)
            self.assertFalse(bad.exists())
        mine = self.root / "notes.json"
        mine.write_text('{"keep": 1}', encoding="utf-8")
        with self.assertRaises(ValueError):
            image_audit.run([vault], self.papers, mine)
        self.assertEqual(mine.read_text(encoding="utf-8"), '{"keep": 1}')
        out = self.root / "audit.json"
        image_audit.run([vault], self.papers, out)
        image_audit.run([vault], self.papers, out)  # an earlier audit may be replaced
        self.assertNotIn(b"\r", out.read_bytes())

if __name__ == "__main__":
    unittest.main()
