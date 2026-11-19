"""Unit tests pemilik konfigurasi tunggal: env_values/env_value dan database_url.

Tanpa PostgreSQL: database_url diuji lewat stub env_values pada modul connection
(nama terikat saat import, maka stub disasar di connection, bukan di config).
Diverifikasi live psycopg 3.3.8: conninfo_to_dict('') -> {} dan
'postgresql://localhost/' -> {'host': 'localhost'} — keduanya jatuh ke validasi field.
"""
import os
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Sources import config
from Sources.infrastructure.database import connection


class FfmpegTransportTests(unittest.TestCase):
    def test_rtsp_forced_tcp_before_any_cv2_use(self):
        """Sources.config adalah pintu pagi bersama semua entrypoint (main/Tools/
        tests); env transport RTSP WAJIB sudah terisi begitu modul ini dimuat —
        sebelum `import cv2` pertama di mana pun — OpenCV membacanya sekali saat
        backend FFmpeg pertama dibentuk (kejadian lapangan 14-15 Nov: set di
        RTSPReader terlambat → RTSP tetap UDP → paket rusak → SEGV dekoder)."""
        self.assertEqual(os.environ.get('OPENCV_FFMPEG_CAPTURE_OPTIONS'),
                         'rtsp_transport;tcp')


class EnvReadingTests(unittest.TestCase):
    def test_environment_variable_wins(self):
        with unittest.mock.patch.dict(os.environ, {'K2_TEST_KEY': 'from-env'}):
            self.assertEqual(config.env_value('K2_TEST_KEY'), 'from-env')
            self.assertEqual(config.env_values('K2_TEST_KEY'), ['from-env'])

    def test_missing_key_returns_default(self):
        self.assertIsNone(config.env_value('K2_TEST_KEY_ABSENT'))
        self.assertEqual(config.env_value('K2_TEST_KEY_ABSENT'), None)
        self.assertEqual(config.env_value('K2_TEST_KEY_ABSENT', 'dflt'), 'dflt')

    def test_model_env_value_target_still_patchable(self):
        # test_detection menambal Sources.detection.model.env_value; nama itu
        # harus tetap megang fungsi config (kontrak refactor K2).
        from Sources.detection import model
        self.assertIs(model.env_value, config.env_value)


class DatabaseUrlStrictnessTests(unittest.TestCase):
    def test_no_value_points_to_setup_script(self):
        with unittest.mock.patch.object(connection, 'env_values', lambda key: []):
            with self.assertRaises(ValueError) as raised:
                connection.database_url()
            self.assertIn('setup-database.sh', str(raised.exception))

    def test_multiple_values_rejected(self):
        with unittest.mock.patch.object(connection, 'env_values', lambda key: ['a', 'b']):
            with self.assertRaises(ValueError) as raised:
                connection.database_url()
            self.assertIn('tepat satu DATABASE_URL', str(raised.exception))

    def test_empty_value_reaches_field_validation(self):
        with unittest.mock.patch.object(connection, 'env_values', lambda key: ['']):
            with self.assertRaises(ValueError) as raised:
                connection.database_url()
            self.assertIn('harus berisi database', str(raised.exception))

    def test_partial_value_reaches_field_validation(self):
        with unittest.mock.patch.object(connection, 'env_values',
                                        lambda key: ['postgresql://localhost/']):
            with self.assertRaises(ValueError) as raised:
                connection.database_url()
            self.assertIn('harus berisi database', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
