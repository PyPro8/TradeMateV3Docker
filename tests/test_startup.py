"""Startup test: confirms the foundation works. Run with:
    python -m unittest discover -s tests -v
"""

import contextlib
import io
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trademate.app import EXIT_CONFIG_ERROR, EXIT_OK, main
from trademate.config import Mode, load_config
from trademate.errors import ConfigError
from trademate.logging_setup import LOG_FILE_NAME, setup_logging



class StartupTest(unittest.TestCase):
    def setUp(self):
        for key in ("TRADEMATE_MODE", "TRADEMATE_LOG_LEVEL", "TRADEMATE_CONFIG", "TRADEMATE_LOG_DIR",
                    "TRADEMATE_DATA_DIR", "TRADEMATE_MT5_DATA_DIR", "TRADEMATE_HOST", "PORT"):
            os.environ.pop(key, None)

    def test_config_loads_with_development_default(self):
        settings = load_config()
        self.assertEqual(settings.mode, Mode.DEVELOPMENT)
        self.assertEqual(settings.app.name, "TradeMate")


    def test_render_environment_overrides_host_port_and_persistent_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {
                "TRADEMATE_HOST": "0.0.0.0",
                "PORT": "10000",
                "TRADEMATE_DATA_DIR": str(root / "trademate"),
                "TRADEMATE_LOG_DIR": str(root / "logs"),
                "TRADEMATE_MT5_DATA_DIR": str(root / "Common" / "Files"),
            }
            with mock.patch.dict(os.environ, env):
                settings = load_config()
            self.assertEqual(settings.ui.host, "0.0.0.0")
            self.assertEqual(settings.ui.port, 10000)
            self.assertEqual(settings.app.data_dir, root / "trademate")
            self.assertEqual(settings.app.log_dir, root / "logs")
            self.assertEqual(settings.mt5.data_dir, root / "Common" / "Files")

    def test_env_overrides_mode(self):
        with mock.patch.dict(os.environ, {"TRADEMATE_MODE": "demo"}):
            self.assertEqual(load_config().mode, Mode.DEMO)

    def test_invalid_mode_raises_config_error(self):
        with mock.patch.dict(os.environ, {"TRADEMATE_MODE": "bogus"}):
            with self.assertRaises(ConfigError):
                load_config()

    def test_logging_writes_to_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = load_config()
            settings = type(settings)(
                mode=settings.mode,
                app=type(settings.app)(name="TradeMate", log_level="INFO", log_dir=Path(tmp)),
                mt5=settings.mt5,
            )
            logger = setup_logging(settings)
            logger.info("hello from test")
            for handler in logger.handlers:
                handler.flush()
            log_text = (Path(tmp) / LOG_FILE_NAME).read_text(encoding="utf-8")
            self.assertIn("hello from test", log_text)
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()

    def test_main_prints_confirmation(self):
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(out):
            with mock.patch("trademate.config.PROJECT_ROOT", Path(tmp)):
                # log_dir resolves against the real root at load time, so point config at tmp
                cfg = Path(tmp) / "settings.toml"
                cfg.write_text(f'mode = "development"\n[app]\nlog_dir = "{Path(tmp).as_posix()}/logs"\n')
                with mock.patch.dict(os.environ, {"TRADEMATE_CONFIG": str(cfg)}):
                    code = main()
        trademate_logger = logging.getLogger("trademate")
        for handler in list(trademate_logger.handlers):
            trademate_logger.removeHandler(handler)
            handler.close()
        self.assertEqual(code, EXIT_OK)
        text = out.getvalue()
        self.assertIn("TradeMate", text)
        self.assertIn("Foundation initialized successfully.", text)
        self.assertIn("Mode: development", text)

    def test_main_reports_bad_config_cleanly(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"TRADEMATE_MODE": "bogus"}):
            with contextlib.redirect_stderr(err):
                code = main()
        self.assertEqual(code, EXIT_CONFIG_ERROR)
        self.assertIn("Configuration error", err.getvalue())


if __name__ == "__main__":
    unittest.main()
