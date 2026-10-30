import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from health_bank import vault
from health_bank.publish import check_publication, main
from health_bank.store import Store


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "synthetic@example.invalid")
        self.git("config", "user.name", "Synthetic test")
        home = patch("health_bank.vault.Path.home", return_value=self.base / "home")
        home.start()
        self.addCleanup(home.stop)
        self.key = Path(vault.initialize(self.root)["key_file"]).read_bytes()

    def git(self, *arguments):
        return subprocess.run(["git", *arguments], cwd=self.root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, path, content):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def seal(self):
        original = self.base / "synthetic-source.txt"
        original.write_bytes(b"synthetic evidence; no real patient")
        store = Store(self.root)
        receipt = store.add_artifact(original, "synthetic")
        store.add_record({"patient_id": "synthetic-1", "lane": "documents", "kind": "synthetic",
                          "source": {"system": "synthetic", "artifact_sha256": receipt["artifact_sha256"], "locator": "whole-file"},
                          "data": {"text": "synthetic evidence"}})
        return vault.seal(self.root)

    def test_code_and_docs_pass_without_printing_names_or_content(self):
        self.write("src/example.py", b"VALUE = 1\n")
        self.write("README.md", b"Synthetic project instructions.\n")
        self.write("pyproject.toml", b'[project]\nname="synthetic"\n')
        self.git("add", "src", "README.md", "pyproject.toml")
        result = check_publication(self.root)
        self.assertTrue(result["allowed"])
        self.assertEqual(result["staged_paths"], 3)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--root", str(self.root)]), 0)
        self.assertNotIn("example.py", output.getvalue())
        self.assertNotIn("Synthetic project", output.getvalue())

    def test_plaintext_medical_and_key_paths_blocked(self):
        self.write(".local/raw/private.jsonl", b'{"synthetic": true}\n')
        self.write("docs/source.pdf", b"synthetic PDF source")
        self.write("src/backup.key", self.key)
        self.git("add", "-f", ".local/raw/private.jsonl", "docs/source.pdf", "src/backup.key")
        result = check_publication(self.root)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["violations"]["disallowed_paths"], 3)
        self.assertEqual(result["violations"]["key_matches"], 1)

    def test_key_bytes_and_mixed_case_hex_blocked_inside_allowed_files(self):
        self.write("src/accidental.py", b"KEY=" + self.key)
        hexadecimal = "".join(character.upper() if index % 2 else character for index, character in enumerate(self.key.hex()))
        self.write("docs/key-backup.md", hexadecimal.encode("ascii"))
        self.git("add", "src", "docs")
        result = check_publication(self.root)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["violations"]["key_matches"], 2)
        self.assertNotIn("disallowed_paths", result["violations"])

    def test_authenticated_staged_vault_passes(self):
        self.seal()
        self.git("add", "vault")
        result = check_publication(self.root)
        self.assertTrue(result["allowed"], result)
        self.assertTrue(result["vault_validated"])

    def test_staged_ciphertext_differs_from_working_copy_is_rejected(self):
        self.seal()
        self.git("add", "vault")
        path = next((self.root / "vault/objects").glob("*.aesgcm"))
        content = bytearray(path.read_bytes())
        content[-1] ^= 1
        path.write_bytes(content)
        result = check_publication(self.root)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["violations"]["vault_validation_failures"], 1)
        self.assertEqual(result["violations"]["staged_vault_objects_differ_from_working_copy"], 1)

    def test_manifest_referencing_unstaged_objects_is_rejected(self):
        self.seal()
        self.git("add", "vault/manifest.json")
        result = check_publication(self.root)
        self.assertFalse(result["allowed"])
        self.assertGreater(result["violations"]["manifest_objects_missing_from_index"], 0)

    def test_unreferenced_new_ciphertext_is_rejected(self):
        self.seal()
        existing = next((self.root / "vault/objects").glob("*.aesgcm"))
        self.write("vault/objects/" + "0" * 64 + ".aesgcm", existing.read_bytes())
        self.git("add", "vault")
        result = check_publication(self.root)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["violations"]["unreferenced_staged_vault_objects"], 1)

    def test_changed_working_manifest_cannot_publish_older_staged_manifest(self):
        self.seal()
        self.git("add", "vault")
        path = self.root / "vault/manifest.json"
        manifest = json.loads(path.read_bytes())
        manifest["objects"] = []
        path.write_text(json.dumps(manifest))
        result = check_publication(self.root)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["violations"]["staged_manifest_differs_from_validated_copy"], 1)

    def test_prior_tracked_snapshot_objects_can_remain_in_history(self):
        self.seal()
        self.git("add", "vault")
        self.git("commit", "-qm", "Synthetic encrypted snapshot")
        Store(self.root).add_record({"patient_id": "synthetic-1", "lane": "documents", "kind": "synthetic-later",
                                     "source": {"system": "synthetic", "locator": "synthetic"}, "data": {"text": "new synthetic evidence"}})
        vault.seal(self.root)
        self.git("add", "vault")
        result = check_publication(self.root)
        self.assertTrue(result["allowed"], result)

    def test_size_guard_rejects_large_staged_blobs(self):
        self.write("src/oversized.py", b"x" * 2048)
        self.git("add", "src")
        with patch("health_bank.publish.MAX_BLOB_BYTES", 1024):
            result = check_publication(self.root)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["violations"]["oversized_blobs"], 1)
        self.assertEqual(result["scanned_blobs"], 0)

    def test_secret_config_and_nonregular_entries_blocked(self):
        self.write("src/secrets.json", b'{"unrelated_secret": "synthetic"}')
        self.write("src/ordinary.py", b"VALUE=1\n")
        (self.root / "src/symlink.py").symlink_to("ordinary.py")
        self.git("add", "src")
        result = check_publication(self.root)
        self.assertFalse(result["allowed"])
        self.assertEqual(result["violations"]["disallowed_paths"], 1)
        self.assertEqual(result["violations"]["nonregular_or_unmerged_entries"], 1)

    def test_malformed_manifest_fails_closed_with_aggregate_output(self):
        self.write("vault/manifest.json", b"[]")
        self.git("add", "vault")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main(["--root", str(self.root)]), 1)
        self.assertFalse(json.loads(output.getvalue())["allowed"])
        self.assertNotIn(str(self.root), output.getvalue())


if __name__ == "__main__":
    unittest.main()
