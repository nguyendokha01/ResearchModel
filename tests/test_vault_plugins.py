import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import config, vault_plugins


class VaultPluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = patch.multiple(config, ROOT=self.root, STORE=self.root / "store",
                                    VAULTS_DIR=self.root / "vaults", LOGS_DIR=self.root / "logs")
        self.patch.start()
        files = {"main.js": b"console.log(1)", "manifest.json": b'{"id":"demo"}', "styles.css": b""}
        self.spec = {"repo": "x/y", "version": "1.0",
                     "files": {n: vault_plugins.hashlib.sha256(b).hexdigest() for n, b in files.items()},
                     "default_settings": {"enableDataviewJs": False,
                                          "shapes": {"square": ["paper"]}}}
        folder = self.root / "runtime" / "obsidian_plugins" / "demo" / "1.0"
        folder.mkdir(parents=True)
        for name, data in files.items():
            (folder / name).write_bytes(data)
        self.approved = patch.object(vault_plugins, "approved", return_value={"demo": self.spec})
        self.approved.start()
        self.vault = self.root / "vaults" / "v"
        (self.vault / ".obsidian").mkdir(parents=True)

    def tearDown(self):
        self.approved.stop()
        self.patch.stop()
        self.temp.cleanup()

    def test_install_enables_and_writes_settings_once(self):
        enabled = self.vault / ".obsidian" / "community-plugins.json"
        enabled.write_text('["researcher-plugin"]', encoding="utf-8")
        report = vault_plugins.install(self.vault)
        self.assertEqual(report["installed"], ["demo 1.0"])
        self.assertEqual(json.loads(enabled.read_text(encoding="utf-8")), ["researcher-plugin", "demo"])
        data = self.vault / ".obsidian" / "plugins" / "demo" / "data.json"
        settings = json.loads(data.read_text(encoding="utf-8"))
        self.assertFalse(settings["enableDataviewJs"])
        self.assertEqual(settings["shapeQueries"]["square"]["rules"][0],
                         {"source": "property", "property": "type", "value": "paper", "logic": "is"})
        data.write_text('{"researcher": true}', encoding="utf-8")
        report = vault_plugins.install(self.vault)
        self.assertEqual(report["settings_kept"], ["demo"])
        self.assertEqual(json.loads(data.read_text(encoding="utf-8")), {"researcher": True})
        self.assertEqual(json.loads(enabled.read_text(encoding="utf-8")), ["researcher-plugin", "demo"])

    def test_hash_mismatch_refuses_install(self):
        (self.root / "runtime" / "obsidian_plugins" / "demo" / "1.0" / "main.js").write_bytes(b"tampered")
        report = vault_plugins.install(self.vault)
        self.assertEqual(report["installed"], [])
        self.assertIn("demo: SHA-256 mismatch for main.js", report["problems"])
        self.assertFalse((self.vault / ".obsidian" / "plugins" / "demo").exists())

    def test_unapproved_plugin_refused(self):
        report = vault_plugins.install(self.vault, ["other"])
        self.assertTrue(report["problems"][0].startswith("other: not approved"))

    def test_real_config_pins_versions_and_hashes(self):
        self.approved.stop()
        try:
            specs = vault_plugins.approved()
        finally:
            self.approved.start()
        self.assertEqual(set(specs), {"dataview", "extended-graph"})
        for spec in specs.values():
            self.assertTrue(all(len(h) == 64 for h in spec["files"].values()))
        self.assertFalse(specs["dataview"]["default_settings"]["enableDataviewJs"])
        colors = specs["extended-graph"]["default_settings"]["interactiveSettings"]["link"]["colors"]
        self.assertIn({"type": "conflicts_with", "color": "#dc2626"}, colors)


if __name__ == "__main__":
    unittest.main()
