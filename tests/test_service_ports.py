"""Regression checks for service port routing and shutdown isolation."""
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('blocky_ui', ROOT / 'scripts/blocky_ui_server.py')
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)


class ServicePortsTest(unittest.TestCase):
    def test_missing_manifest_does_not_launch_job(self):
        with patch.object(ui, '_launch_job_process') as launch:
            with self.assertRaisesRegex(ValueError, 'Save the manifest'):
                ui.start_job('missing_unsaved_manifest.json', 'dry-run')
            launch.assert_not_called()

    def test_mesh_command_uses_configured_url(self):
        with patch.object(ui, 'HUNYUAN_URL', 'http://127.0.0.1:19082'):
            command = ui.run_command_for('meshes', 'test.json', ['test_001'])
        self.assertEqual(command[command.index('--hunyuan-url') + 1], 'http://127.0.0.1:19082')

    def test_conflicting_port_is_preserved(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(ValueError, 'leaving it running'):
                ui.require_free_port('127.0.0.1', port)
            with patch.object(ui.os, 'kill') as kill:
                self.assertFalse(ui._kill_port(port))
                kill.assert_not_called()
            self.assertEqual(listener.getsockname()[1], port)

    def test_shutdown_matches_installation_and_port(self):
        with tempfile.TemporaryDirectory() as directory:
            install = Path(directory)
            script = install / 'main.py'
            script.write_text('import time; time.sleep(30)\n')
            processes = [subprocess.Popen([sys.executable, str(script), '--port', str(port)], cwd=install)
                         for port in (19088, 19089)]
            try:
                with patch.object(ui, 'find_comfyui_dir', return_value=install), patch.object(ui, 'COMFYUI_PORT', 19088):
                    self.assertEqual(ui._service_processes(19088), [processes[0].pid])
                    with patch.object(ui.os, 'kill') as kill:
                        self.assertTrue(ui._kill_port(19088))
                        kill.assert_called_once_with(processes[0].pid, ui.signal.SIGTERM)
            finally:
                for process in processes:
                    process.terminate()
                    process.wait(timeout=5)

    def test_legacy_broad_patterns_cannot_kill_processes(self):
        with patch.object(ui.os, 'kill') as kill:
            for pattern in ['api_server.py', 'hy3d', 'ComfyUI/main.py']:
                self.assertFalse(ui._pkill_pattern(pattern))
            kill.assert_not_called()

    def test_batch_and_ui_honor_environment_port(self):
        env = dict(os.environ, HUNYUAN_PORT='19082', COMFYUI_PORT='19088')
        result = subprocess.run([sys.executable, '-c',
            "import sys; sys.path.insert(0, 'scripts'); import run_batch, blocky_ui_server as ui; "
            "assert run_batch.HUNYUAN_URL == ui.HUNYUAN_URL == 'http://127.0.0.1:19082'; "
            "assert run_batch.COMFYUI_URL == ui.COMFYUI_URL == 'http://127.0.0.1:19088'"],
            cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
