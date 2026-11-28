"""C2 fix: the operator identity used by irreversible overrides must not be spoofable by a bare
env var - it needs a real, correctly-permissioned identity file
(`~/.config/capsule/identities/<name>.env`, the same mechanism the ops secrets layer uses)."""
import os
import stat
import tempfile
import unittest
from pathlib import Path

from capsule import identity


class Resolve(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())

    def _touch(self, name, mode=0o600):
        f = self.d / f"{name}.env"
        f.write_text("CAPSULE_IDENTITY_CLIENT_ID=x\n")
        os.chmod(f, mode)
        return f

    def test_no_env_var_resolves_nothing(self):
        self.assertIsNone(identity.resolve(env={}, identity_dir=self.d))

    def test_env_var_alone_without_a_file_resolves_nothing(self):
        """The exact C2 hole: an env var a worker process could set, with nothing backing it."""
        self.assertIsNone(identity.resolve(env={"CAPSULE_IDENTITY": "owner"}, identity_dir=self.d))

    def test_env_var_with_a_properly_permissioned_file_resolves(self):
        self._touch("owner", 0o600)
        env = {"CAPSULE_IDENTITY": "owner"}
        self.assertEqual(identity.resolve(env=env, identity_dir=self.d), "owner")

    def test_group_or_world_readable_file_is_refused(self):
        self._touch("owner", 0o644)
        self.assertIsNone(identity.resolve(env={"CAPSULE_IDENTITY": "owner"}, identity_dir=self.d))

    def test_a_directory_at_the_identity_path_is_refused(self):
        (self.d / "owner.env").mkdir()
        self.assertIsNone(identity.resolve(env={"CAPSULE_IDENTITY": "owner"}, identity_dir=self.d))

    def test_a_symlink_is_refused_even_to_a_correctly_permissioned_target(self):
        other = Path(tempfile.mkdtemp())
        target = other / "secret.env"
        target.write_text("x\n")
        os.chmod(target, 0o600)
        (self.d / "owner.env").symlink_to(target)
        self.assertIsNone(identity.resolve(env={"CAPSULE_IDENTITY": "owner"}, identity_dir=self.d))

    def test_a_traversal_name_is_refused(self):
        """The regex alone refuses any name that could spell a path escape - no '/' is ever
        allowed in a name, so no lookup outside the identities dir is even expressible."""
        self.assertIsNone(identity.resolve(env={"CAPSULE_IDENTITY": "../elsewhere/secret"}, identity_dir=self.d))
        self.assertIsNone(identity.resolve(env={"CAPSULE_IDENTITY": "sub/name"}, identity_dir=self.d))

    def test_verify_is_the_same_check_resolve_uses(self):
        """The tick calls `verify()` directly on a human file's `issuer` field - same function,
        so nothing lets a spool write skip what the CLI enforces."""
        self._touch("owner", 0o600)
        self.assertEqual(identity.verify("owner", identity_dir=self.d), "owner")
        self.assertIsNone(identity.verify("ghost", identity_dir=self.d))


class Import(unittest.TestCase):
    def test_importing_without_a_home_does_not_raise(self):
        """The CLI must start with no HOME and no passwd entry (the worker container)."""
        import importlib
        from unittest import mock
        with mock.patch.dict(os.environ, clear=False), \
                mock.patch.object(Path, "home", side_effect=RuntimeError("no home")):
            os.environ.pop("HOME", None)
            self.assertIs(importlib.reload(identity), identity)
        importlib.reload(identity)


if __name__ == "__main__":
    unittest.main()
