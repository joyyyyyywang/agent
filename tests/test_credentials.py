import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from joyagent.credentials import resolve_api_key
from joyagent.cli import doctor
from joyagent.config import load_config
from joyagent.providers import ProviderError, create_provider
from joyagent.runtime import build_runtime
from joyagent.types import Message


class CredentialFileTests(unittest.TestCase):
    """Only synthetic keys and private temporary files are used here."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.path = self.root / "credentials.toml"
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write(self, text=None, *, key="fake-fallback-key", endpoint="https://api.deepseek.com", provider="deepseek"):
        if text is None:
            text = f"[{provider}]\napi_key = {json.dumps(key)}\nbase_url = {json.dumps(endpoint)}\n"
        self.path.write_text(text, encoding="utf-8")
        self.path.chmod(0o600)
        return self.path

    def resolve(self, *, kind="deepseek", endpoint="https://api.deepseek.com", key_env="DEEPSEEK_API_KEY", path=None):
        return resolve_api_key(kind, endpoint, key_env, str(path or self.path))

    def assert_safe_error(self, secret="fake-fallback-key"):
        with self.assertRaises(ValueError) as raised:
            self.resolve()
        self.assertNotIn(secret, str(raised.exception))
        self.assertNotIn(self.path.read_text(errors="replace") if self.path.is_file() else "sentinel-unavailable", str(raised.exception))

    def test_private_file_is_used_when_environment_is_absent(self):
        self.write()
        self.assertEqual(self.resolve(), ("fake-fallback-key", "file"))

    def test_explicit_environment_wins_without_reading_invalid_file(self):
        self.write("this is invalid TOML and contains fake-fallback-key")
        self.path.chmod(0o666)
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "fake-user-key"}):
            self.assertEqual(self.resolve(), ("fake-user-key", "environment"))

    def test_whitespace_only_environment_uses_fallback(self):
        self.write()
        for value in ("", " ", "\t\n"):
            with self.subTest(value=repr(value)), patch.dict(os.environ, {"DEEPSEEK_API_KEY": value}):
                self.assertEqual(self.resolve(), ("fake-fallback-key", "file"))

    def test_environment_edge_whitespace_is_trimmed(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": " \tfake-user-key\n"}):
            self.assertEqual(self.resolve(), ("fake-user-key", "environment"))

    def test_invalid_nonempty_environment_is_rejected_instead_of_falling_back(self):
        self.write()
        for key in ("fake user key", "fake\tuser-key", "fake\nuser-key", "fake\u007fuser-key",
                    "fake-\u00e9-user-key", "k" * 4097):
            with self.subTest(length=len(key), prefix=repr(key[:20])), patch.dict(os.environ, {"DEEPSEEK_API_KEY": key}):
                with self.assertRaises(ValueError) as raised:
                    self.resolve()
                self.assertNotIn(key, str(raised.exception))
                self.assertNotIn("fake-fallback-key", str(raised.exception))

    def test_custom_environment_variable_overrides_only_when_configured(self):
        self.write()
        with patch.dict(os.environ, {"MY_MODEL_KEY": "fake-custom-key", "OPENAI_API_KEY": "fake-unrelated-key"}):
            self.assertEqual(self.resolve(key_env="MY_MODEL_KEY"), ("fake-custom-key", "environment"))
            self.assertEqual(self.resolve(), ("fake-fallback-key", "file"))

    def test_missing_file_returns_missing(self):
        self.assertEqual(self.resolve(), ("", "missing"))

    def test_no_configured_path_does_not_discover_files(self):
        home = self.root / "home"
        default = home / ".config" / "joyagent"
        default.mkdir(parents=True)
        self.write()
        self.path.rename(default / "credentials.toml")
        with patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(resolve_api_key("deepseek", "https://api.deepseek.com", "DEEPSEEK_API_KEY", None), ("", "missing"))

    def test_tilde_expansion_uses_explicit_home_path(self):
        self.write()
        with patch.dict(os.environ, {"HOME": str(self.root)}):
            self.assertEqual(self.resolve(path="~/credentials.toml"), ("fake-fallback-key", "file"))

    def test_key_is_bound_to_provider_and_endpoint(self):
        self.write()
        for kind, endpoint in (("kimi", "https://api.deepseek.com"),
                               ("openai", "https://api.deepseek.com"),
                               ("deepseek", "https://proxy.example/v1"),
                               ("deepseek", "https://api.deepseek.com.evil.example"),
                               ("deepseek", "https://api.deepseek.com/v1"),
                               ("deepseek", "http://api.deepseek.com")):
            with self.subTest(kind=kind, endpoint=endpoint):
                self.assertEqual(self.resolve(kind=kind, endpoint=endpoint), ("", "missing"))

    def test_endpoint_trailing_slash_is_normalized(self):
        self.write(endpoint="https://api.deepseek.com/")
        self.assertEqual(self.resolve(), ("fake-fallback-key", "file"))
        self.write()
        self.assertEqual(self.resolve(endpoint="https://api.deepseek.com/"), ("fake-fallback-key", "file"))

    def test_generic_provider_can_use_explicit_file_entry(self):
        self.write(provider="openai", endpoint="https://gateway.example/v1")
        self.assertEqual(self.resolve(kind="openai", endpoint="https://gateway.example/v1", key_env="OPENAI_API_KEY"),
                         ("fake-fallback-key", "file"))

    def test_malformed_toml_does_not_expose_file_content(self):
        self.write('[deepseek]\napi_key = "fake-fallback-key"\nnot valid TOML\n')
        self.assert_safe_error()

    def test_invalid_utf8_does_not_expose_file_content(self):
        self.write()
        self.path.write_bytes(b'[deepseek]\napi_key="fake-fallback-key"\n\xff')
        self.assert_safe_error()

    def test_wrong_entry_types_fail_safely(self):
        for text in ('deepseek = "fake-fallback-key"\n',
                     '[deepseek]\napi_key = 123\nbase_url = "https://api.deepseek.com"\n',
                     '[deepseek]\napi_key = ["fake-fallback-key"]\nbase_url = "https://api.deepseek.com"\n',
                     '[deepseek]\napi_key = "fake-fallback-key"\nbase_url = 123\n'):
            with self.subTest(text=text):
                self.write(text)
                self.assert_safe_error()

    def test_key_whitespace_control_or_excessive_length_is_rejected(self):
        for key in (" fake-fallback-key", "fake-fallback-key ", "fake fallback key", "fake\tfallback-key",
                    "fake\nfallback-key", "fake\u007ffallback-key", "fake-\u00e9-fallback-key", "f" * 4097):
            with self.subTest(length=len(key), prefix=repr(key[:20])):
                self.write(key=key)
                self.assert_safe_error(secret=key)

    def test_oversized_file_is_rejected(self):
        self.write()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("#" + "x" * (16 * 1024))
        self.assert_safe_error()

    @unittest.skipUnless(os.name == "posix", "POSIX ownership and mode checks")
    def test_group_or_world_permissions_are_rejected(self):
        for mode in (0o640, 0o604, 0o660, 0o606, 0o611, 0o666):
            with self.subTest(mode=oct(mode)):
                self.write()
                self.path.chmod(mode)
                self.assert_safe_error()

    @unittest.skipUnless(os.name == "posix", "POSIX symlink check")
    def test_final_symlink_is_rejected(self):
        self.write()
        target = self.root / "real-credentials.toml"
        self.path.rename(target)
        self.path.symlink_to(target)
        self.assert_safe_error()

    @unittest.skipUnless(os.name == "posix", "POSIX ownership check")
    def test_file_owned_by_different_user_is_rejected(self):
        self.write()
        with patch("joyagent.credentials.os.geteuid", return_value=os.geteuid() + 1):
            self.assert_safe_error()

    def test_directory_cannot_be_used_as_credential_file(self):
        self.path.mkdir()
        with self.assertRaises(ValueError):
            self.resolve()


class CredentialIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        shutil.copytree(Path(__file__).parents[1] / "joyagent/defaults", self.root, dirs_exist_ok=True)
        self.path = self.root / "test-credentials.toml"
        self.path.write_text('[deepseek]\napi_key = "fake-fallback-key"\nbase_url = "https://api.deepseek.com"\n', encoding="utf-8")
        self.path.chmod(0o600)
        self.settings = {"provider": "deepseek", "model": "fake-model", "credentials_file": str(self.path)}
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def configuration(self):
        config = load_config(self.root / "agent.toml")
        # Model credential tests must not inspect unrelated owner credentials.
        config.data["decision"] = {"enabled": False}
        config.data["routing"] = {"enabled": False}
        return config

    def test_mock_request_uses_file_key_then_environment_override(self):
        provider = create_provider(self.settings)
        for environment, expected in (({}, "fake-fallback-key"), ({"DEEPSEEK_API_KEY": "fake-user-key"}, "fake-user-key")):
            with self.subTest(source="environment" if environment else "file"), patch.dict(os.environ, environment, clear=True), \
                    patch("joyagent.providers._post_json", return_value={"choices": [{"message": {"content": "safe answer"}}]}) as post:
                reply = asyncio.run(provider.complete([Message("user", "hello")], []))
            self.assertEqual(reply.content, "safe answer")
            self.assertEqual(post.call_args.args[0], "https://api.deepseek.com/chat/completions")
            self.assertEqual(post.call_args.args[2]["Authorization"], f"Bearer {expected}")
            self.assertNotIn(expected, json.dumps(post.call_args.args[1]))
            self.assertNotIn(expected, repr(reply))

    def test_changed_endpoint_cannot_send_fallback_key(self):
        provider = create_provider({**self.settings, "base_url": "https://proxy.example/v1"})
        with patch("joyagent.providers._post_json") as post, self.assertRaises(ProviderError) as raised:
            asyncio.run(provider.complete([Message("user", "hello")], []))
        post.assert_not_called()
        self.assertNotIn("fake-fallback-key", str(raised.exception))

    def test_doctor_accepts_file_credential_without_network_or_printing_key(self):
        config = self.configuration()
        config.data["model"] = dict(self.settings)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch("joyagent.providers._post_json") as post:
            result = doctor(config)
        post.assert_not_called()
        self.assertEqual(result, 0)
        self.assertNotIn("fake-fallback-key", output.getvalue())

    def test_relative_credential_path_is_anchored_to_config_directory(self):
        config = self.configuration()
        config.data["model"] = {**self.settings, "credentials_file": self.path.name}
        settings = config.model_settings
        self.assertEqual(settings["credentials_file"], str(self.path))
        self.assertEqual(config.data["model"]["credentials_file"], self.path.name)
        self.assertEqual(create_provider(settings).credential(), ("fake-fallback-key", "file"))

    @unittest.skipUnless(os.name == "posix", "POSIX final symlink protection")
    def test_config_path_resolution_does_not_bypass_final_symlink_rejection(self):
        link = self.root / "linked.toml"
        link.symlink_to(self.path)
        config = self.configuration()
        config.data["model"] = {**self.settings, "credentials_file": link.name}
        settings = config.model_settings
        self.assertEqual(settings["credentials_file"], str(link))
        with self.assertRaises(ValueError) as raised:
            create_provider(settings).credential()
        self.assertNotIn("fake-fallback-key", str(raised.exception))

    def test_configured_credentials_inside_workspace_are_protected(self):
        workspace = self.root / "workspace"
        workspace.mkdir(exist_ok=True)
        protected = workspace / "owner-settings.dat"
        original = self.path.read_text()
        protected.write_text(original, encoding="utf-8")
        protected.chmod(0o600)
        config = self.configuration()
        config.data["model"] = {"provider": "demo", "model": "demo", "credentials_file": "workspace/owner-settings.dat"}

        async def check():
            runtime = await build_runtime(config)
            try:
                registry = runtime.agent.tools
                listing = await registry.execute("workspace_list", {"path": "."})
                self.assertTrue(listing["ok"])
                self.assertNotIn(protected.name, json.dumps(listing))
                for name, arguments in (("workspace_read", {"path": protected.name}),
                                        ("workspace_write", {"path": protected.name, "content": "changed", "overwrite": True})):
                    with self.subTest(tool=name):
                        result = await registry.execute(name, arguments)
                        self.assertFalse(result["ok"])
                        self.assertEqual(result["code"], "tool_error")
                        self.assertNotIn("fake-fallback-key", json.dumps(result))
                self.assertEqual(protected.read_text(), original)
            finally:
                await runtime.close()

        with patch("joyagent.providers._post_json") as post:
            asyncio.run(check())
        post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
