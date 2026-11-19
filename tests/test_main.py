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
        self.client = TestClient(app_nano.app, headers={'host': '127.0.0.1'})

    def test_enqueuing_single_url_works(self):
        response = self.client.post(
            '/enqueue',
            json={'url': 'https://www.youtube.com/watch?v=dyKgT7EOpKU'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn(payload['status'], {'queued', 'already_exists'})
        self.assertEqual(
            payload['url'],
            'https://www.youtube.com/watch?v=dyKgT7EOpKU',
        )

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
    def test_format_selector_prefers_original_audio_track(self):
        selector = download_worker._build_format_selector({
            'max_resolution': 1080,
            'p_vcodec': 'avc1',
            'p_acodec': 'mp4a',
        })

        self.assertIn('[language_preference>=10]', selector)
        self.assertIn('ba[language_preference>=10]', selector)
        self.assertIn('[dynamic_range=?SDR]', selector)

    def test_format_selector_falls_back_to_available_audio(self):
        selector = download_worker._build_format_selector({
            'max_resolution': 1080,
            'p_vcodec': 'avc1',
            'p_acodec': 'mp4a',
        })

        self.assertIn('+ba', selector)
        self.assertIn('b[height<=1080][dynamic_range=?SDR]', selector)
        self.assertNotIn('b[height<=1080]+', selector)

    def test_final_encode_defaults_to_available_h264_encoder(self):
        video_args, pixel_format = download_worker._video_codec_args({})

        self.assertIn('libx264', video_args)
        self.assertEqual(pixel_format, 'yuv420p')

    def test_final_encode_allows_explicit_av1(self):
        video_args, pixel_format = download_worker._video_codec_args({'video_codec': 'av1'})

        self.assertIn('libsvtav1', video_args)
        self.assertEqual(pixel_format, 'yuv420p10le')

    def test_final_encode_allows_configured_eight_bit_pixel_format(self):
        for codec in ('av1', 'hevc', 'h264'):
            with self.subTest(codec=codec):
                _, pixel_format = download_worker._video_codec_args({
                    'video_codec': codec,
                    'video_pixel_format': 'yuv420p',
                })

                self.assertEqual(pixel_format, 'yuv420p')

    def test_final_encode_rejects_unknown_pixel_format(self):
        with self.assertRaisesRegex(ValueError, 'video_pixel_format'):
            download_worker._video_codec_args({'video_pixel_format': 'rgb24'})

    def test_source_frame_rate_rejects_invalid_encoder_rate(self):
        self.assertEqual(
            download_worker._source_frame_rate({'r_frame_rate': '30/1'}),
            '30',
        )
        self.assertIsNone(
            download_worker._source_frame_rate({'r_frame_rate': '1000/1'}),
        )

    def test_max_video_fps_caps_source_rate_without_slowing_video(self):
        self.assertEqual(
            download_worker._resolve_frame_rate(
                {'r_frame_rate': '60/1'},
                {'max_video_fps': 29.97},
            ),
            '29.97',
        )
        self.assertEqual(
            download_worker._resolve_frame_rate(
                {'r_frame_rate': '24/1'},
                {'max_video_fps': 29.97},
            ),
            '24',
        )
        self.assertEqual(
            download_worker._resolve_frame_rate(
                {'r_frame_rate': '60/1'},
                {'max_video_fps': 0},
            ),
            '60',
        )

    def test_video_frame_count_uses_ffprobe_frame_fields(self):
        self.assertEqual(
            download_worker._video_frame_count({'nb_frames': '120'}),
            120,
        )
        self.assertEqual(
            download_worker._video_frame_count({'nb_frames': 'N/A', 'nb_read_frames': '90'}),
            90,
        )
        self.assertIsNone(download_worker._video_frame_count({'nb_frames': 'N/A'}))

    def test_filter_graph_normalizes_video_frame_rate(self):
        graph = download_worker._build_filter_graph(
            [(0.0, 3.0)],
            0,
            'yuv420p10le',
            frame_rate='30',
        )

        self.assertIn('[vc]fps=30,format=yuv420p10le[v]', graph)

    def test_ffmpeg_resource_args_allow_automatic_threading(self):
        self.assertEqual(
            download_worker._ffmpeg_resource_args({'ffmpeg_threads': 0}),
            ['-threads', '0', '-filter_threads', '0', '-filter_complex_threads', '0'],
        )

    def test_ffmpeg_progress_accepts_numeric_timestamps(self):
        self.assertAlmostEqual(
            download_worker._progress_elapsed(
                {'out_time_us': '1500000'},
                10.0,
            ),
            1.5,
        )
        self.assertAlmostEqual(
            download_worker._progress_elapsed(
                {'out_time_ms': '2500000'},
                10.0,
            ),
            2.5,
        )
        self.assertAlmostEqual(
            download_worker._progress_elapsed(
                {'out_time': '00:00:03.250000'},
                10.0,
            ),
            3.25,
        )

    async def test_download_delegates_after_database_claim(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            class FakeDatabase:
                def get_status(self, url):
                    return None

                def is_done(self, url):
                    return False

                def claim(self, url):
                    return True

                def update_status(self, url, status):
                    pass

            with patch('server.download_worker.db.Database', return_value=FakeDatabase()), \
                    patch('server.download_worker._fetch_video_title_async', return_value='Example'), \
                    patch('server.download_worker._download_async', new=AsyncMock()) as download:
                with patch.dict(
                    download_worker.config_module.CONFIG,
                    {
                        'output_template': str(pathlib.Path(tmp_dir) / '%(title)s.%(ext)s'),
                        'recode_video': True,
                    },
                    clear=False,
                ):
                    download.return_value = {
                        'status': 'downloaded',
                        'saved_bytes': 0,
                    }
                    result = await download_worker.download_video(
                        'https://www.youtube.com/watch?v=dyKgT7EOpKU'
                    )

            self.assertEqual(result['status'], 'downloaded')
            download.assert_awaited_once()

    async def test_download_retries_when_database_says_downloaded_but_file_is_missing(self):
        class FakeDatabase:
            def get_status(self, url):
                return 'downloaded'

            def is_done(self, url):
                return False

            def claim(self, url):
                return True

            def update_status(self, url, status):
                pass

        with patch('server.download_worker.db.Database', return_value=FakeDatabase()), \
                patch('server.download_worker._fetch_video_title_async', return_value='Missing'), \
                patch('server.download_worker._download_async', new=AsyncMock()) as download:
            with patch.dict(
                download_worker.config_module.CONFIG,
                {
                    'output_template': 'missing-downloads/%(title)s.%(ext)s',
                    'recode_video': True,
                },
                clear=False,
            ):
                await download_worker.download_video(
                    'https://www.youtube.com/watch?v=dyKgT7EOpKU'
                )

        download.assert_awaited_once()

    async def test_download_skips_unset_ffmpeg_env_path(self):
        config = {
            'output_template': 'downloads/%(title)s.%(ext)s',
            'ffmpeg_loc': '.env/FFMPEG',
            'preferred_video_container': ['mp4'],
            'p_vcodec': 'avc1',
            'p_acodec': 'mp4a',
            'write_subtitles': True,
            'subtitle_languages': ['en', 'de'],
        }
        with patch.dict(os.environ, {}):
            if 'FFMPEG' in os.environ:
                del os.environ['FFMPEG']

            args = download_worker._build_download_args(
                'https://www.youtube.com/watch?v=dyKgT7EOpKU',
                'Example',
                config,
                [],
                None,
                pathlib.Path('downloads/%(title)s.%(ext)s'),
                'paths.txt',
            )

        self.assertNotIn('--ffmpeg-location', args)
        self.assertIn('--no-playlist', args)
        self.assertIn('--windows-filenames', args)
        self.assertIn('--force-overwrites', args)
        self.assertIn('--file-access-retries', args)
        self.assertIn('--write-subs', args)
        self.assertIn('--sub-langs', args)
        self.assertIn('en,de', args)
        self.assertNotIn('--write-auto-subs', args)

class TestShutdownOnIdle(unittest.TestCase):
    def test_shutdown_option_requires_positive_minutes(self):
        import app_nano

        with self.assertRaises(SystemExit):
            app_nano._parse_args(['--shutdown_on_idle', '0'])

    def test_shutdown_option_is_parsed(self):
        import app_nano

        args = app_nano._parse_args(['--shutdown_on_idle', '15'])
        self.assertEqual(args.shutdown_on_idle, 15)


if __name__ == '__main__':
    unittest.main()