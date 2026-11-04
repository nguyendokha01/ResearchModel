"""G6 unit 6: the domain profile (config/domain/<name>.yaml chosen by system.yaml `domain`)."""
import importlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kbsys import config, domain, evidence, evidence_fill, mesh

OLD_SPECIES = {  # the literal that lived in evidence.py before the profile existed
    "mouse": ["mice", "murine", "mouse"], "rat": ["rats", "rat"], "human": ["human", "humans", "patient", "patients",
    "volunteer", "volunteers", "subjects"], "bovine": ["bovine", "cattle", "cow", "cows", "calf", "calves"],
    "porcine": ["porcine", "pig", "pigs", "swine"], "ovine": ["ovine", "sheep", "ewe", "ewes"],
    "caprine": ["caprine", "goat", "goats"], "equine": ["equine", "horse", "horses", "mare", "mares"],
    "canine": ["canine", "dog", "dogs"], "feline": ["feline", "cat", "cats"], "rabbit": ["rabbit", "rabbits"],
    "primate": ["monkey", "monkeys", "macaque", "macaques", "marmoset", "primate"],
}


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.real = config.CONFIG_DIR
        self.temp = tempfile.TemporaryDirectory(prefix="demo3-domain-")
        self.cfg = Path(self.temp.name) / "config"
        shutil.copytree(self.real / "domain", self.cfg / "domain")
        config.CONFIG_DIR = self.cfg
        domain.profile.cache_clear()

    def tearDown(self):
        config.CONFIG_DIR = self.real
        domain.profile.cache_clear()
        domain.profile()  # refill from the real config: a later test that patches CONFIG_DIR must not re-read it
        importlib.reload(evidence_fill)  # back to the shipped profile
        self.temp.cleanup()

    def choose(self, text):
        (self.cfg / "system.yaml").write_text(text, encoding="utf-8")
        domain.profile.cache_clear()

    def test_biomedical_equals_the_old_literal_and_is_the_default(self):
        self.assertEqual(domain.profile()["species"], OLD_SPECIES)  # no system.yaml at all
        self.choose("system:\n  name: x\n")  # system.yaml without `domain`
        self.assertEqual(domain.profile()["name"], "biomedical")
        self.assertTrue(domain.profile()["use_mesh"])
        self.assertEqual(evidence.SPECIES_SYNONYMS, OLD_SPECIES)  # the loaded dict keeps its name

    def test_shipped_system_yaml_names_biomedical(self):
        config.CONFIG_DIR = self.real
        domain.profile.cache_clear()
        self.assertEqual(domain.profile()["name"], "biomedical")
        self.assertEqual(domain.profile()["species"], OLD_SPECIES)

    def test_generic_has_no_species_fill_and_no_mesh(self):
        self.choose("domain: generic\n")
        self.assertEqual(domain.profile()["species"], {})
        with tempfile.TemporaryDirectory() as t:
            db_file = Path(t) / "mesh.sqlite"
            db_file.write_bytes(b"x")
            with mock.patch.object(mesh, "MESH_DB", db_file):
                self.assertFalse(mesh.available())
                self.assertEqual(mesh.lookup("stroke"), [])
                self.assertEqual(mesh.expand(["stroke"], lambda w: False), [])
            domain.profile.cache_clear()
            self.choose("domain: biomedical\n")
            with mock.patch.object(mesh, "MESH_DB", db_file):
                self.assertTrue(mesh.available())
        self.choose("domain: generic\n")
        with mock.patch.object(evidence, "SPECIES_SYNONYMS", domain.profile()["species"]):
            importlib.reload(evidence_fill)
            sentence = {"sentence_id": "S0001", "text": "Mice were treated with 5 mg.", "kind": "data"}
            unit = {"unit_type": "result", "outcome": {"direction": "x"}}
            self.assertNotIn("species", evidence_fill.fill_fields(unit, [sentence], {"title": "Mice study"}))
        importlib.reload(evidence_fill)
        self.assertIn("species", evidence_fill.fill_fields(unit, [sentence], {"title": "Mice study"}))

    def test_unknown_domain_is_a_clear_error(self):
        self.choose("domain: nonsense\n")
        with self.assertRaises(FileNotFoundError) as caught:
            domain.profile()
        self.assertIn("nonsense", str(caught.exception))

    def test_profile_is_read_at_import_not_under_a_later_config_dir_patch(self):
        """Review 2026-11-03: read lazily, a CONFIG_DIR without domain/ (bench temp root, a test module run alone) crashed mesh.available()."""
        config.CONFIG_DIR = self.real
        importlib.reload(domain)
        config.CONFIG_DIR = Path(self.temp.name) / "empty"
        mesh.available()  # no FileNotFoundError
        self.assertEqual(domain.profile()["name"], "biomedical")

    def test_malformed_profile_is_a_clear_error_never_a_wrong_species_fill(self):
        """Review 2026-11-03: a bare string became letters (m,i,c,e), '' matched every sentence, unquoted no became False."""
        self.choose("domain: x\n")
        for text in ("use_mesh: true\nspecies:\n  mouse: mice\n", "use_mesh: true\nspecies:\n  mouse: [mice, '']\n",
                     "use_mesh: true\nspecies:\n  mouse: [mice, no]\n", "species:\n  rat: [rats]\n", "use_mesh: 'false'\nspecies: {}\n", ""):
            (self.cfg / "domain" / "x.yaml").write_text(text, encoding="utf-8")
            domain.profile.cache_clear()
            with self.assertRaises(ValueError, msg=text) as caught:
                domain.profile()
            self.assertIn("x.yaml", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
