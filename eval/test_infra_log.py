"""Unit tests for the centralized logging setup module."""

from __future__ import annotations

import logging
import os
from unittest import mock

import pytest
from infra.log import setup_logging


def test_setup_logging_returns_logger():
    """Verify that setup_logging returns a Logger instance with the correct name."""
    name = "test_custom_logger_name"
    logger = setup_logging(name)
    assert isinstance(logger, logging.Logger)
    assert logger.name == name


def test_setup_logging_sets_level():
    """Verify that setup_logging properly overrides the level on the logger."""
    name = "test_level_logger"
    logger = setup_logging(name, level="WARNING")
    assert logger.level == logging.WARNING

    logger_int = setup_logging(name, level=logging.ERROR)
    assert logger_int.level == logging.ERROR


def test_setup_logging_idempotent_if_handlers_exist():
    """Verify that if root handlers already exist, basicConfig is not called."""
    with mock.patch("logging.getLogger") as mock_get_logger, \
         mock.patch("logging.basicConfig") as mock_basic_config:

        # Mock the root logger to return a non-empty list of handlers
        mock_root = mock.Mock()
        mock_root.handlers = [mock.Mock()]
        
        # mock_get_logger("") or mock_get_logger() returns root logger
        mock_get_logger.side_effect = lambda name=None: mock_root if not name else mock.Mock()

        setup_logging("test_idempotency")
        
        # basicConfig should not be called because handlers exist
        mock_basic_config.assert_not_called()


def test_setup_logging_calls_basic_config_if_no_handlers():
    """Verify that basicConfig is called when root handlers are empty."""
    with mock.patch("logging.getLogger") as mock_get_logger, \
         mock.patch("logging.basicConfig") as mock_basic_config, \
         mock.patch("infra.log.configure_logging") as mock_configure_logging:

        # Mock root logger to have empty handlers list
        mock_root = mock.Mock()
        mock_root.handlers = []
        
        mock_get_logger.side_effect = lambda name=None: mock_root if not name else mock.Mock()

        setup_logging("test_new_setup", level="DEBUG", fmt="%(message)s")
        
        # basicConfig should be called with correct level and format
        mock_basic_config.assert_called_once_with(level="DEBUG", format="%(message)s")
        mock_configure_logging.assert_not_called()


def test_setup_logging_uses_json_formatter_if_configured():
    """Verify that configure_logging is called instead of basicConfig if LOG_FORMAT=json."""
    with mock.patch("logging.getLogger") as mock_get_logger, \
         mock.patch("logging.basicConfig") as mock_basic_config, \
         mock.patch("infra.log.configure_logging") as mock_configure_logging, \
         mock.patch.dict(os.environ, {"LOG_FORMAT": "json"}):

        # Mock root logger to have empty handlers list
        mock_root = mock.Mock()
        mock_root.handlers = []
        
        mock_get_logger.side_effect = lambda name=None: mock_root if not name else mock.Mock()

        setup_logging("test_json_setup")
        
        # configure_logging should be called instead of basicConfig
        mock_configure_logging.assert_called_once()
        mock_basic_config.assert_not_called()
