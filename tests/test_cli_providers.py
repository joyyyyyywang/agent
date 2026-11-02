import contextlib
import io
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from joyagent.cli import doctor
from joyagent.config import load_config


class CloudDoctorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        shutil.copytree(Path(__file__).parents[1] / "joyagent/defaults", self.root, dirs_exist_ok=True)
        self.config = load_config(self.root / "agent.toml")
        # These cases validate only the selected chat provider, using fake keys.
        self.config.data["decision"] = {"enabled": False}
        self.config.data["routing"] = {"enabled": False}

    def check(self, environment):
        output = io.StringIO()
        with patch.dict(os.environ, environment, clear=True), contextlib.redirect_stdout(output), \
                patch("joyagent.providers._post_json") as request:
            result = doctor(self.config)
        request.assert_not_called()
        return result, output.getvalue()

    def test_cloud_presets_require_their_own_key(self):
        for provider, variable in [("deepseek", "DEEPSEEK_API_KEY"),
                                   ("kimi", "MOONSHOT_API_KEY"), ("zhipu", "ZHIPU_API_KEY")]:
            with self.subTest(provider=provider):
                self.config.data["model"] = {"provider": provider, "model": "configured-model"}
                result, text = self.check({"OPENAI_API_KEY": "unrelated-key"})
                self.assertEqual(result, 1)
                self.assertIn(variable, text)
                self.assertNotIn("本地模型可选", text)
                result, text = self.check({variable: "test-secret-never-print"})
                self.assertEqual(result, 0)
                self.assertIn("已设置", text)
                self.assertNotIn("test-secret-never-print", text)

    def test_custom_key_variable_is_used(self):
        self.config.data["model"] = {"provider": "deepseek", "model": "configured-model", "api_key_env": "MY_MODEL_KEY"}
        self.assertEqual(self.check({"DEEPSEEK_API_KEY": "wrong-variable"})[0], 1)
        self.assertEqual(self.check({"MY_MODEL_KEY": "test-key"})[0], 0)

    def test_unsupported_thinking_setting_fails_without_network(self):
        self.config.data["model"] = {"provider": "kimi", "model": "kimi-k3", "thinking": "disabled"}
        result, text = self.check({"MOONSHOT_API_KEY": "test-key"})
        self.assertEqual(result, 1)
        self.assertIn("thinking", text)

    def test_invalid_model_choices_fail_before_provider_creation_without_network(self):
        for choices in ([], ["different-model"], ["configured-model", "configured-model"],
                        ["configured-model", "https://attacker.invalid"], "configured-model"):
            with self.subTest(choices=choices):
                self.config.data["model"] = {"provider": "demo", "model": "configured-model",
                                             "choices": choices}
                with patch("joyagent.providers.create_provider") as provider:
                    result, text = self.check({})
                provider.assert_not_called()
                self.assertEqual(result, 1)
                self.assertIn("FAIL  模型配置", text)
                self.assertIn("未访问网络", text)


if __name__ == "__main__":
    unittest.main()
