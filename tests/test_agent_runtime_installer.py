from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from ops import install_agent_runtimes as installer


class RuntimeInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.dependencies = {package: "1.2.3" for package in installer.PACKAGES.values()}
        self.package = {"private": True, "dependencies": self.dependencies}
        self.lock = {"lockfileVersion": 3, "packages": {"": {"dependencies": self.dependencies}}}
        self.write_manifest()

    def write_manifest(self):
        (self.source / "package.json").write_text(json.dumps(self.package))
        (self.source / "package-lock.json").write_text(json.dumps(self.lock))

    def test_rejects_relative_or_parent_traversal_prefix(self):
        for value in ("relative", "/tmp/../global"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                installer.absolute(value)

    def test_manifest_requires_exact_matching_versions_and_credential_free_urls(self):
        self.package["dependencies"]["node"] = "latest"
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "exact"):
            installer.manifest(self.source)
        self.package["dependencies"]["node"] = "1.2.3"
        self.lock["packages"]["node_modules/node"] = {"resolved": "https://token@example.invalid/pkg.tgz"}
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "download URL"):
            installer.manifest(self.source)
        self.lock["packages"]["node_modules/node"]["resolved"] = "https://example.invalid/pkg.tgz?token=secret"
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "download URL"):
            installer.manifest(self.source)

    def test_removed_or_unknown_runtime_packages_are_rejected(self):
        self.package["dependencies"]["opencode-ai"] = "1.18.4"
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, "exactly the four"):
            installer.manifest(self.source)

    def test_environment_does_not_inherit_credentials_or_user_config(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "NPM_TOKEN": "secret",
                                    "NODE_OPTIONS": "--require=/private/injection.js",
                                    "CODEX_HOME": "/private/codex", "HOME": "/private/home"}):
            env = installer.environment(self.root / "isolated")
        for variable in ("OPENAI_API_KEY", "NPM_TOKEN", "NODE_OPTIONS"):
            self.assertNotIn(variable, env)
        self.assertNotEqual(env["HOME"], "/private/home")
        self.assertNotEqual(env["CODEX_HOME"], "/private/codex")
        self.assertNotEqual(env["npm_config_userconfig"], env["npm_config_globalconfig"])
        for key in ("npm_config_userconfig", "npm_config_globalconfig"):
            path = Path(env[key])
            self.assertEqual(path.read_text(), "")
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_existing_unmanaged_or_symlink_prefix_is_never_modified(self):
        existing = self.root / "existing"
        existing.mkdir()
        marker = existing / "keep.txt"
        marker.write_text("original")
        with self.assertRaisesRegex(ValueError, "already exists"):
            installer.install(existing, self.source)
        alias = self.root / "alias"
        alias.symlink_to(existing, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "real directory"):
            installer.install(alias, self.source)
        self.assertEqual(marker.read_text(), "original")

    def test_explicit_install_publishes_only_after_versions_pass(self):
        prefix = self.root / "managed" / "v1"
        process = Mock(returncode=0)
        process.poll.return_value = 0
        versions = {name: "1.2.3" for name in installer.PACKAGES}
        with patch.object(installer.shutil, "which", return_value="/usr/bin/npm"), \
                patch.object(installer.subprocess, "Popen", return_value=process) as spawn, \
                patch.object(installer, "verify", return_value=versions) as verify:
            record = installer.install(prefix, self.source)
        self.assertEqual(spawn.call_args.args[0], ["/usr/bin/npm", "ci", "--no-audit", "--no-fund"])
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        self.assertNotEqual(verify.call_args.args[0], prefix)
        self.assertEqual(record["versions"], versions)
        self.assertEqual(stat.S_IMODE(prefix.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((prefix / "installation.json").stat().st_mode), 0o600)
        self.assertFalse((prefix / ".install-home").exists())
        for name in installer.PACKAGES:
            wrapper = prefix / "bin" / name
            self.assertEqual(stat.S_IMODE(wrapper.stat().st_mode), 0o700)
            self.assertIn('node_modules/.bin:$PATH', wrapper.read_text())
            self.assertEqual(record["executables"][name], str(wrapper))
        with patch.object(installer, "verify", return_value=versions), \
                patch.object(installer.subprocess, "Popen") as spawn:
            self.assertEqual(installer.install(prefix, self.source), record)
            spawn.assert_not_called()

    def test_failed_install_preserves_private_diagnostics_without_publishing_prefix(self):
        prefix = self.root / "managed" / "v1"
        process = Mock(returncode=1)
        process.poll.return_value = 1
        with patch.object(installer.shutil, "which", return_value="/usr/bin/npm"), \
                patch.object(installer.subprocess, "Popen", return_value=process), \
                self.assertRaisesRegex(RuntimeError, "npm ci failed"):
            installer.install(prefix, self.source)
        self.assertFalse(prefix.exists())
        staging, = prefix.parent.glob(".v1-install-*")
        self.assertEqual(stat.S_IMODE(staging.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((staging / "install.log").stat().st_mode), 0o600)

    def test_verification_only_executes_version_commands_with_private_homes(self):
        prefix = self.root / "runtime"
        prefix.mkdir()
        result = subprocess.CompletedProcess([], 0, b"CLI 1.2.3\n", b"")
        with patch.object(installer.subprocess, "run", return_value=result) as run:
            self.assertEqual(installer.verify(prefix, self.dependencies), {name: "1.2.3" for name in installer.PACKAGES})
        self.assertEqual(run.call_count, 4)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][1:], ["--version"])
            self.assertTrue(Path(call.kwargs["env"]["HOME"]).is_relative_to(prefix))
            self.assertIs(call.kwargs["stdin"], subprocess.DEVNULL)


if __name__ == "__main__":
    unittest.main()
