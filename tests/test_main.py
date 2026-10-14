import asyncio
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app_nano
from server import download_worker


class TestAppNano(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_nano.app)

    def test_enqueuing_single_url_works(self):
        response = self.client.post('/enqueue', json={'url': 'https://youtube.com'})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn(payload['status'], {'queued', 'already_exists'})
        self.assertEqual(payload['url'], 'https://youtube.com')

    def test_command_unknown_command_is_rejected(self):
        response = self.client.post('/command', json={'cmd': 'not-a-command'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'unknown_command')

    def test_ytdlp_runtime_uses_env_config(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            runtime = pathlib.Path(tmp_dir) / 'node.exe'
            runtime.touch()

            with patch.dict(os.environ, {'TEST_JS_RUNTIME': str(runtime)}):
                args = download_worker._build_ytdlp_auth_args({
                    'js_runtime_loc': '.env/TEST_JS_RUNTIME',
                })

                self.assertEqual(args, ['--js-runtimes', f'node:{runtime}'])

class TestDownloadWorkerAsync(unittest.IsolatedAsyncioTestCase):
    async def test_download_skips_unset_ffmpeg_env_path(self):
        class FakeStream:
            async def readline(self):
                return b''

        class FakeProcess:
            stdout = FakeStream()

            async def wait(self):
                return 0

        class FakeDatabase:
            def get_queued(self):
                return []

            def update_status(self, url, status):
                pass

        captured = {}

        async def fake_create_subprocess(*args, **kwargs):
            captured['args'] = args
            return FakeProcess()

        # FFMPEG-Variable entfernen und subprocess_exec patchen
        with patch.dict(os.environ, {}):
            if 'FFMPEG' in os.environ:
                del os.environ['FFMPEG']

            with patch('server.download_worker.asyncio.create_subprocess_exec', new=fake_create_subprocess):
                await download_worker._download_async(
                    'https://example.invalid',
                    FakeDatabase(),
                    'Example',
                    config={
                        'output_template': 'downloads/%(title)s.%(ext)s',
                        'ffmpeg_loc': '.env/FFMPEG',
                        'preferred_video_container': ['mp4'],
                        'p_vcodec': 'avc1',
                        'p_acodec': 'mp4a',
                    },
                )

        self.assertNotIn('--ffmpeg-location', captured.get('args', []))


if __name__ == '__main__':
    unittest.main()