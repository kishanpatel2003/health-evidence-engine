import base64
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zlib

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from health_bank.store import Store, canonical_bytes, content_hash
from health_bank import vault


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        self.store = Store(self.root)
        self.key_path = self.base / "outside.key"
        self.key_path.write_bytes(os.urandom(32))
        self.key_path.chmod(0o600)
        original = self.base / "PATIENT-NAME-sensitive.xml"
        original.write_text("<sensitive>SECRET-CLINICAL-VALUE</sensitive>")
        receipt = self.store.add_artifact(original, "synthetic", "https://synthetic.invalid/SECRET-URL")
        self.store.add_record({"patient_id": "SECRET-PATIENT-ID", "lane": "observations", "kind": "synthetic",
                               "source": {"system": "synthetic", "artifact_sha256": receipt["artifact_sha256"],
                                          "locator": "/document/entry", "record_key": "source-1"},
                               "data": {"narrative": "SECRET-CLINICAL-VALUE"}})

    def seal(self):
        return vault.seal(self.root, self.key_path)

    def test_round_trip_matches_all_source_bytes_and_records(self):
        self.seal()
        destination = self.base / "restored"
        result = vault.unseal(self.root, self.key_path, destination)
        self.assertTrue(result["restored"])
        self.assertEqual(result["records"], 1)
        for path in self.store.local.rglob("*"):
            if path.is_file() and path.name != ".write.lock":
                self.assertEqual(path.read_bytes(), (destination / ".local" / path.relative_to(self.store.local)).read_bytes())
        self.assertTrue(vault.verify_vault(self.root, self.key_path)["verified"])

    def test_unchanged_seal_reuses_identical_ciphertext(self):
        first = self.seal()
        files = {path.relative_to(self.root): (path.read_bytes(), path.stat().st_mtime_ns) for path in (self.root / "vault").rglob("*") if path.is_file()}
        second = self.seal()
        self.assertGreater(first["new_objects"], 0)
        self.assertEqual(second["new_objects"], 0)
        for relative, (content, mtime) in files.items():
            path = self.root / relative
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_mtime_ns, mtime)

    def test_vault_contains_no_plaintext_names_values_urls_or_key(self):
        self.seal()
        sensitive = [b"SECRET-CLINICAL-VALUE", b"SECRET-PATIENT-ID", b"SECRET-URL", b"PATIENT-NAME", self.key_path.read_bytes()]
        for path in (self.root / "vault").rglob("*"):
            for secret in sensitive:
                self.assertNotIn(secret, str(path.relative_to(self.root)).encode())
                if path.is_file():
                    self.assertNotIn(secret, path.read_bytes())

    def test_generated_key_is_private_outside_repo(self):
        with patch("health_bank.vault.Path.home", return_value=self.base / "home"):
            info = vault.initialize(self.root)
        path = Path(info["key_file"])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(path.read_bytes()), 32)
        self.assertNotIn(self.root, path.parents)

    def test_key_inside_repository_rejected(self):
        bad_key = self.root / "key"
        bad_key.write_bytes(os.urandom(32))
        with self.assertRaisesRegex(ValueError, "outside"):
            vault.seal(self.root, bad_key)

    def test_wrong_key_and_ciphertext_tamper_fail(self):
        self.seal()
        other_key = self.base / "other.key"
        other_key.write_bytes(os.urandom(32))
        with self.assertRaisesRegex(ValueError, "does not match"):
            vault.unseal(self.root, other_key, self.base / "wrong-restore")
        encrypted = next((self.root / "vault/objects").glob("*.aesgcm"))
        content = bytearray(encrypted.read_bytes())
        content[-1] ^= 1
        encrypted.write_bytes(content)
        with self.assertRaisesRegex(ValueError, "authentication"):
            vault.unseal(self.root, self.key_path, self.base / "tamper-restore")
        self.assertFalse((self.base / "tamper-restore/.local/raw").exists())
        with self.assertRaisesRegex(ValueError, "authentication"):
            vault.verify_vault(self.root, self.key_path)

    def test_existing_store_is_not_overwritten(self):
        self.seal()
        before = (self.store.local / "canonical/observations.jsonl").read_bytes()
        with self.assertRaisesRegex(ValueError, "empty local store"):
            vault.unseal(self.root, self.key_path)
        self.assertEqual(before, (self.store.local / "canonical/observations.jsonl").read_bytes())

    def test_fresh_clone_restore_requires_original_key(self):
        self.seal()
        clone = self.base / "clone"
        shutil.copytree(self.root / "vault", clone / "vault")
        with patch("health_bank.vault.Path.home", return_value=self.base / "different-home"):
            with self.assertRaisesRegex(ValueError, "key unavailable"):
                vault.unseal(clone)
        self.assertTrue(vault.unseal(clone, self.key_path)["restored"])
        self.assertTrue(Store(clone).verify()["verified"])

    def test_authenticated_traversal_payload_is_rejected(self):
        self.seal()
        manifest = json.loads((self.root / "vault/manifest.json").read_bytes())
        plaintext = canonical_bytes({"version": 1, "files": [{"path": "../../outside.txt", "sha256": content_hash(b"escaped"),
                                                            "data": base64.b64encode(b"escaped").decode()}]})
        item = {"version": 1, "type": "snapshot", "sha256": content_hash(plaintext)}
        nonce = os.urandom(12)
        content = vault.MAGIC + nonce + AESGCM(self.key_path.read_bytes()).encrypt(nonce, plaintext, vault._aad(manifest, item))
        (self.root / "vault/objects" / f"{item['sha256']}.aesgcm").write_bytes(content)
        manifest["objects"] = [item]
        (self.root / "vault/manifest.json").write_bytes(canonical_bytes(manifest))
        with self.assertRaisesRegex(ValueError, "Unsafe relative"):
            vault.unseal(self.root, self.key_path, self.base / "unsafe-restore")
        self.assertFalse((self.base / "outside.txt").exists())

    def test_large_repetitive_source_compresses_and_restores_exact_bytes(self):
        original = self.base / "large-synthetic-source.txt"
        unit = b"synthetic repeated source bytes; no clinical information\n" * 1024
        with original.open("wb") as stream:
            for _ in range(2000):
                stream.write(unit)
        self.assertGreater(original.stat().st_size, 100 * 1024 * 1024)
        receipt = self.store.add_artifact(original, "synthetic")
        self.seal()
        encrypted = list((self.root / "vault/objects").glob("*.aesgcm"))
        self.assertTrue(all(path.stat().st_size < vault.MAX_OBJECT_BYTES for path in encrypted))
        self.assertLess(sum(path.stat().st_size for path in encrypted), 2 * 1024 * 1024)
        destination = self.base / "large-restore"
        self.assertTrue(vault.unseal(self.root, self.key_path, destination)["restored"])
        restored = destination / receipt["local_path"]
        self.assertEqual(restored.stat().st_size, original.stat().st_size)
        self.assertEqual(content_hash(restored.read_bytes()), receipt["artifact_sha256"])

    def test_compressed_and_legacy_packs_restore_same_bytes(self):
        content = b'{"original": "001.20", "unknown": null}\n' * 100
        files = [{"path": "canonical/observations.jsonl", "sha256": content_hash(content),
                  "data": base64.b64encode(content).decode("ascii")}]
        packed = vault._pack(files)
        self.assertTrue(packed.startswith(vault.PACK_MAGIC))
        self.assertEqual(packed, vault._pack(files))
        legacy = canonical_bytes({"version": 1, "files": files})
        self.assertLess(len(packed), len(legacy))
        self.assertEqual(vault._unpack(packed, "snapshot"), [("canonical/observations.jsonl", content)])
        self.assertEqual(vault._unpack(legacy, "snapshot"), vault._unpack(packed, "snapshot"))

    def test_compression_bombs_truncated_and_trailing_streams_rejected(self):
        bomb = vault.PACK_MAGIC + zlib.compress(b"x" * 4096)
        with patch("health_bank.vault.MAX_UNPACKED_BYTES", 64):
            with self.assertRaisesRegex(ValueError, "bounded decompression"):
                vault._unpack(bomb, "snapshot")
        packed = vault._pack([])
        with self.assertRaisesRegex(ValueError, "Truncated"):
            vault._unpack(packed[:-1], "snapshot")
        with self.assertRaisesRegex(ValueError, "Trailing"):
            vault._unpack(packed + b"unexpected", "snapshot")
        with self.assertRaisesRegex(ValueError, "Invalid compressed"):
            vault._unpack(vault.PACK_MAGIC + b"not a zlib stream", "snapshot")

    def test_ciphertext_size_guard_fails_before_writing_oversized_blob(self):
        original = self.base / "incompressible.bin"
        original.write_bytes(os.urandom(8192))
        self.store.add_artifact(original, "synthetic")
        with patch("health_bank.vault.MAX_OBJECT_BYTES", 2048):
            with self.assertRaisesRegex(ValueError, "95 MiB safety limit"):
                self.seal()
        self.assertFalse(list((self.root / "vault/objects").glob("*.aesgcm")))


if __name__ == "__main__":
    unittest.main()
