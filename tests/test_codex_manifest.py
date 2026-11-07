"""Codex routing, authentication, structured output, and timeout regression checks."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('blocky_ui_codex', ROOT / 'scripts/blocky_ui_server.py')
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


class CodexManifestTest(unittest.TestCase):
    def test_default_provider_uses_codex_without_api_key(self):
        with patch.object(ui, 'load_env', return_value={}), \
             patch.object(ui, 'generate_manifest_entries_with_codex', return_value={
                 'entries': [{'id': 'old_doll', 'prompt': 'single old doll'}]
             }) as codex, patch.object(ui, 'generate_manifest_json_with_openai') as api:
            entries = ui.generate_manifest_entries({'model_types': 'old doll'})
        self.assertEqual(entries[0]['id'], 'old_doll')
        codex.assert_called_once()
        api.assert_not_called()

    def test_api_is_only_used_when_explicitly_selected(self):
        with patch.object(ui, 'generate_manifest_json_with_openai', return_value={
            'entries': [{'id': 'doll'}]
        }) as api, patch.object(ui, 'generate_manifest_entries_with_codex') as codex:
            ui.generate_manifest_entries({'provider': 'openai', 'model_types': 'doll'})
        api.assert_called_once()
        codex.assert_not_called()

    def test_api_credentials_are_removed_from_codex_environment(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'secret', 'CODEX_API_KEY': 'secret',
                                     'OPENAI_API': 'secret', 'CODEX_BIN': '/test/codex'}):
            env = ui.codex_environment()
        self.assertTrue({'OPENAI_API_KEY', 'CODEX_API_KEY', 'OPENAI_API'}.isdisjoint(env))
        self.assertEqual(env['CODEX_BIN'], '/test/codex')

    def test_api_key_login_does_not_count_as_chatgpt_login(self):
        with patch.object(ui, 'codex_binary', return_value='codex'), \
             patch.object(ui.subprocess, 'run', return_value=subprocess.CompletedProcess(
                 [], 0, '', 'Logged in using an API key')):
            self.assertFalse(ui.codex_status()['signed_in'])

    def test_logged_out_does_not_launch_generation(self):
        with patch.object(ui, 'codex_status', return_value={'signed_in': False, 'message': 'Run codex login'}), \
             patch.object(ui.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(ValueError, 'codex login'):
                ui.generate_manifest_entries_with_codex('instructions', {})
            launch.assert_not_called()

    def test_structured_result_is_read_from_output_file(self):
        proc = Mock(returncode=0)
        def launch(command, **kwargs):
            output = Path(command[command.index('--output-last-message') + 1])
            output.write_text(json.dumps({'entries': [{'id': 'old_doll'}]}))
            self.assertIn('--ignore-user-config', command)
            self.assertIn('forced_login_method="chatgpt"', command)
            self.assertIn('read-only', command)
            self.assertIn('shell_tool', command)
            self.assertIn('web_search="disabled"', command)
            self.assertNotEqual(Path(kwargs['cwd']), ROOT)
            return proc
        with patch.object(ui, 'codex_status', return_value={'signed_in': True}), \
             patch.object(ui, 'codex_binary', return_value='codex'), \
             patch.object(ui.subprocess, 'Popen', side_effect=launch):
            result = ui.generate_manifest_entries_with_codex('instructions', {'model_types': 'old doll'})
        self.assertEqual(result['entries'][0]['id'], 'old_doll')
        self.assertIn('old doll', proc.communicate.call_args.args[0])

    def test_timeout_kills_generation_process_group(self):
        proc = Mock(pid=123)
        proc.communicate.side_effect = [subprocess.TimeoutExpired('codex', 180), ('', '')]
        with patch.object(ui, 'codex_status', return_value={'signed_in': True}), \
             patch.object(ui, 'codex_binary', return_value='codex'), \
             patch.object(ui.subprocess, 'Popen', return_value=proc), \
             patch.object(ui.os, 'killpg') as kill:
            with self.assertRaisesRegex(ValueError, 'timed out'):
                ui.generate_manifest_entries_with_codex('instructions', {})
            kill.assert_called_once_with(123, ui.signal.SIGKILL)


if __name__ == '__main__':
    unittest.main()
