import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from joyagent.cli_backend import ExternalProvider, backend_catalog, installation, transcript
from joyagent.cli_profiles import build_profile, build_resident_profile
from joyagent.config import Config
from joyagent.policy import Limits, Policy
from joyagent.providers import create_provider
from joyagent.tools import Workspace
from joyagent.types import Message


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = Workspace(self.root / 'workspace', protected=[self.root / 'private'])
        self.install = self.root / 'runtimes'
        (self.install / 'node_modules/.bin').mkdir(parents=True)
        for name in ('node', 'codex', 'pi', 'dsh'):
            path = self.install / 'node_modules/.bin' / name
            path.write_text('#!/bin/sh\nexit 0\n')
            path.chmod(0o700)
        self.config = Config(self.root / 'agent.toml', {
            'backends': {'install_dir': str(self.install), 'codex': {'enabled': True},
                         'pi': {'enabled': True}, 'deepseek': {'enabled': True}},
            'execution': {'enabled': True}})
        self.provider = create_provider({'provider': 'openai', 'model': 'fake',
            'base_url': 'http://127.0.0.1:1/v1', 'api_key_env': 'JOYAGENT_TEST_EMPTY'})

    def test_default_has_only_joy_and_external_names_are_allowlisted(self):
        self.assertEqual(Config(self.config.path, {}).backend_choices, ('joyagent',))
        for value in ('other', '../codex', '', 1):
            with self.assertRaises(ValueError):
                self.config.select_backend(value)
        self.config.data['backends']['codex']['enabled'] = 'true'
        with self.assertRaises(ValueError):
            self.config.backend_choices

    def test_no_fallback_without_linux_isolation_or_real_provider(self):
        with patch('joyagent.execution.sys.platform', 'darwin'):
            catalog = backend_catalog(self.config, self.workspace, self.provider)
        self.assertTrue(catalog[0]['available'])
        self.assertTrue(all(not row['available'] for row in catalog[1:]))
        with patch('joyagent.cli_backend.SandboxExecutor'):
            catalog = backend_catalog(self.config, self.workspace, create_provider({'provider': 'demo'}))
        self.assertTrue(all(not row['available'] for row in catalog[1:]))

    def test_install_cannot_overlap_private_state_or_escape_via_binary_symlink(self):
        self.assertEqual(installation(self.config, self.workspace), self.install.resolve())
        binary = self.install / 'node_modules/.bin/pi'
        binary.unlink()
        binary.symlink_to(sys.executable)
        with self.assertRaises(ValueError):
            installation(self.config, self.workspace)
        self.config.data['backends']['install_dir'] = str(self.workspace.root)
        with self.assertRaises(ValueError):
            installation(self.config, self.workspace)

    def test_transcript_carries_history_but_not_vendor_private_data(self):
        result = transcript([Message('user', 'remember this'), Message('assistant', 'done',
            provider_data={'token': 'opaque-secret'}), Message('user', 'continue')])
        self.assertIn('remember this', result)
        self.assertIn('continue', result)
        self.assertNotIn('opaque-secret', result)

    def test_profiles_use_fixed_local_gateway_and_no_inherited_secrets(self):
        for backend in ('codex', 'pi', 'dsh'):
            with self.subTest(backend=backend):
                home = self.root / backend
                with patch.dict(os.environ, {'IDEALAB_API_TOKEN': 'owner-secret'}):
                    argv, env = build_profile(backend, 'fake-model', 'http://127.0.0.1:1234/v1', home, self.install)
                text = json.dumps({'argv': argv, 'env': env})
                text += ''.join(p.read_text() for p in home.rglob('*') if p.is_file())
                self.assertNotIn('owner-secret', text)
                self.assertIn('127.0.0.1:1234/v1', text)
                self.assertNotIn('IDEALAB_API_TOKEN', env)

    def test_retired_backend_cannot_generate_profiles_or_create_private_state(self):
        home = self.root / 'removed-backend'
        args = ('opencode', 'fake-model', 'http://127.0.0.1:1234/v1', home, self.install)
        for build, extra in ((build_profile, ()), (build_resident_profile, (('fake-model',),))):
            with self.subTest(builder=build.__name__), self.assertRaises(ValueError):
                build(*args, *extra)
            self.assertFalse(home.exists())


@unittest.skipUnless(sys.platform == 'linux' and os.environ.get('JOYAGENT_TEST_CLI_PREFIX'),
                     'requires Linux isolation and explicitly installed optional CLIs')
class InstalledAgentTests(unittest.IsolatedAsyncioTestCase):
    BACKENDS = ('codex', 'pi', 'deepseek')

    async def test_each_real_cli_runs_through_isolated_fake_gateway(self):
        # No network model calls: transport is replaced before any CLI starts.
        for backend in self.BACKENDS:
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = Workspace(root / 'workspace')
                config = Config(root / 'agent.toml', {'execution': {'enabled': True},
                    'backends': {'install_dir': os.environ['JOYAGENT_TEST_CLI_PREFIX'], backend: {'enabled': True}}})
                provider = create_provider({'provider': 'openai', 'model': 'fake-model',
                    'base_url': 'http://127.0.0.1:1/v1', 'api_key_env': 'JOYAGENT_TEST_FAKE_KEY'})
                requests = []
                def fake(url, body, headers, timeout):
                    requests.append(body)
                    return {'id': 'fake', 'object': 'chat.completion', 'created': 1, 'model': 'fake-model',
                        'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant',
                                                                                   'content': 'FAKE_AGENT_OK'}}],
                        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}
                with patch.dict(os.environ, {'JOYAGENT_TEST_FAKE_KEY': 'fake-only'}), patch('joyagent.model_broker._post_json', fake):
                    result = await ExternalProvider(backend, config, provider, workspace,
                        Policy(('*',), limits=Limits(turn_timeout_seconds=30))).complete(
                        [Message('system', 'Use tools only when necessary.'), Message('user', 'Say hello')], [])
                self.assertFalse(result.stopped, result.content)
                self.assertEqual(result.content, 'FAKE_AGENT_OK')
                self.assertGreaterEqual(len(requests), 1)
                self.assertEqual(result.backend, backend)

    async def test_each_real_cli_executes_native_tool_and_returns_actual_output(self):
        # Exercise the CLI's own tool loop through the real namespace, relay,
        # and event parser. Only its upstream model transport is fake.
        for backend in self.BACKENDS:
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = Workspace(root / 'workspace')
                config = Config(root / 'agent.toml', {'execution': {'enabled': True},
                    'backends': {'install_dir': os.environ['JOYAGENT_TEST_CLI_PREFIX'], backend: {'enabled': True}}})
                provider = create_provider({'provider': 'openai', 'model': 'fake-model',
                    'base_url': 'http://127.0.0.1:1/v1', 'api_key_env': 'JOYAGENT_TEST_FAKE_KEY'})
                requests = []
                tool_sent = False
                proof = 'NATIVE_' + backend.upper() + '_PROOF'

                def fake(url, body, headers, timeout):
                    nonlocal tool_sent
                    requests.append(body)
                    names = {item.get('function', {}).get('name') for item in body.get('tools', [])}
                    name = 'exec_command' if backend == 'codex' else 'bash'
                    message = {'role': 'assistant', 'content': 'FAKE_AGENT_OK'}
                    finish = 'stop'
                    if not tool_sent and name in names:
                        command = ('test -z "${LD_PRELOAD-}" && test -z "${BUN_OPTIONS-}" '
                            '&& test -z "${BUN_INSPECT_PRELOAD-}" && test ! -e /proc/self/environ '
                            "&& printf '%s' '" + proof + "' > native-proof.txt && cat native-proof.txt")
                        arguments = ({'cmd': command, 'yield_time_ms': 1000} if backend == 'codex'
                            else {'command': command, 'description': 'Create a local verification file'}
                            if backend == 'deepseek' else {'command': command})
                        message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                            'id': 'native-proof-call', 'type': 'function', 'function': {
                                'name': name, 'arguments': json.dumps(arguments)}}]}
                        finish = 'tool_calls'
                        tool_sent = True
                    return {'id': 'fake', 'object': 'chat.completion', 'created': 1, 'model': 'fake-model',
                        'choices': [{'index': 0, 'finish_reason': finish, 'message': message}],
                        'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}

                with patch.dict(os.environ, {'JOYAGENT_TEST_FAKE_KEY': 'fake-only'}), patch('joyagent.model_broker._post_json', fake):
                    result = await ExternalProvider(backend, config, provider, workspace,
                        Policy(('*',), limits=Limits(turn_timeout_seconds=30))).complete(
                        [Message('system', 'Use your native command tool for the requested verification.'),
                         Message('user', 'Create native-proof.txt and read it back using a command.')], [])
                self.assertFalse(result.stopped, result.content)
                self.assertTrue(tool_sent, 'CLI must advertise its native command tool')
                self.assertEqual((workspace.root / 'native-proof.txt').read_text(), proof)
                self.assertTrue(any(message.get('role') == 'tool' and proof in json.dumps(message.get('content'))
                    for request in requests for message in request.get('messages', [])),
                    'The model must receive the actual command result before answering')
                self.assertEqual(result.content, 'FAKE_AGENT_OK')
                self.assertEqual(result.backend, backend)
                self.assertTrue(result.tool_events)
                self.assertTrue(all(event['ok'] for event in result.tool_events), result.tool_events)
