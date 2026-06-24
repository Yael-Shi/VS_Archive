import io
import logging
import logging.config
import os
from collections.abc import Mapping
from typing import cast
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from vs_archive.settings import _log_level_from_env

GEMINI_LOGGER_NAME = "documents.services.gemini_engine"


class LoggingConfigurationTests(SimpleTestCase):
    def _logging_config(self, *, log_level: str | None = None) -> dict:
        root = dict(cast(Mapping[str, object], settings.LOGGING["root"]))
        if log_level is not None:
            root["level"] = log_level
        return {
            "version": settings.LOGGING["version"],
            "disable_existing_loggers": settings.LOGGING["disable_existing_loggers"],
            "formatters": settings.LOGGING["formatters"],
            "handlers": settings.LOGGING["handlers"],
            "root": root,
            "loggers": settings.LOGGING.get("loggers", {}),
        }

    def _apply_logging_config(self, *, log_level: str | None = None) -> None:
        logging.config.dictConfig(self._logging_config(log_level=log_level))

    def _root_console_handler(self) -> logging.StreamHandler:
        root = logging.getLogger()
        stream_handlers = [
            handler
            for handler in root.handlers
            if isinstance(handler, logging.StreamHandler)
        ]
        self.assertEqual(len(stream_handlers), 1)
        return stream_handlers[0]

    def setUp(self):
        super().setUp()
        self._apply_logging_config(log_level="INFO")

    def tearDown(self):
        logging.config.dictConfig(settings.LOGGING)
        super().tearDown()

    def test_settings_logging_declares_console_root_handler(self):
        self.assertEqual(
            settings.LOGGING["handlers"]["console"]["class"],
            "logging.StreamHandler",
        )
        self.assertEqual(settings.LOGGING["root"]["handlers"], ["console"])
        self.assertEqual(settings.LOGGING["root"]["level"], settings.LOG_LEVEL)

    def test_root_logger_has_console_handler_at_info(self):
        root = logging.getLogger()
        handler = self._root_console_handler()

        self.assertIsInstance(handler, logging.StreamHandler)
        self.assertEqual(root.getEffectiveLevel(), logging.INFO)

    def test_gemini_engine_inherits_info_from_root_without_local_level(self):
        logger = logging.getLogger(GEMINI_LOGGER_NAME)

        self.assertEqual(logger.level, logging.NOTSET)
        self.assertEqual(logger.getEffectiveLevel(), logging.INFO)
        self.assertTrue(logger.isEnabledFor(logging.INFO))

    def test_gemini_engine_info_reaches_configured_console_handler(self):
        logger = logging.getLogger(GEMINI_LOGGER_NAME)
        handler = self._root_console_handler()
        buffer = io.StringIO()
        original_stream = handler.stream

        try:
            handler.stream = buffer
            logger.info("Gemini transcription page completed: page=%s", 1)
        finally:
            handler.stream = original_stream

        output = buffer.getvalue()
        self.assertIn("Gemini transcription page completed: page=1", output)
        self.assertIn(GEMINI_LOGGER_NAME, output)


class LogLevelFromEnvTests(SimpleTestCase):
    def test_normalizes_lowercase_value(self):
        with patch.dict(os.environ, {"LOG_LEVEL": "info"}, clear=False):
            self.assertEqual(_log_level_from_env("LOG_LEVEL"), "INFO")

    def test_uses_default_when_unset(self):
        env = os.environ.copy()
        env.pop("LOG_LEVEL", None)
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(_log_level_from_env("LOG_LEVEL"), "INFO")

    def test_rejects_invalid_value(self):
        with patch.dict(os.environ, {"LOG_LEVEL": "VERBOSE"}, clear=False):
            with self.assertRaises(ImproperlyConfigured):
                _log_level_from_env("LOG_LEVEL")
