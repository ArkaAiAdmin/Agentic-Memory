#!/usr/bin/env python3
"""Unit tests for safe_call.py.

Run with:
    ~/.config/agentic-memory/venv/bin/python eval/test_safe_call.py
"""

import logging
import sys
import unittest
from pathlib import Path

INSTALL_DIR = Path.home() / ".config" / "agentic-memory"
sys.path.insert(0, str(INSTALL_DIR))

from safe_call import safe_call


class TestSafeCall(unittest.TestCase):
    def test_returns_result_on_success(self):
        def add(a, b):
            return a + b

        result = safe_call(add, 1, 2)
        self.assertEqual(result, 3)

    def test_returns_fallback_on_exception(self):
        def fail():
            raise ValueError("boom")

        result = safe_call(fail, fallback="default")
        self.assertEqual(result, "default")

    def test_none_fallback_on_exception(self):
        def fail():
            raise RuntimeError("fail")

        result = safe_call(fail, fallback=None)
        self.assertIsNone(result)

    def test_raise_on_propagates_specified_exception(self):
        def fail():
            raise TypeError("type error")

        with self.assertRaises(TypeError):
            safe_call(fail, raise_on=(TypeError,), fallback="x")

    def test_raise_on_does_not_catch_other_exceptions(self):
        def fail():
            raise ValueError("value error")

        result = safe_call(fail, raise_on=(TypeError,), fallback="ok")
        self.assertEqual(result, "ok")

    def test_kwargs_forwarded(self):
        def greet(greeting, name):
            return f"{greeting}, {name}"

        result = safe_call(greet, greeting="Hello", name="World")
        self.assertEqual(result, "Hello, World")

    def test_log_level_respected(self):
        messages = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                messages.append(record.levelno)

        logger = logging.getLogger("infra.safe_call")
        logger.addHandler(CaptureHandler())
        logger.setLevel(logging.DEBUG)

        def fail():
            raise OSError("disk full")

        safe_call(fail, fallback=0, log_level=logging.ERROR)
        self.assertIn(logging.ERROR, messages)

    def test_err_label_in_log(self):
        messages = []

        class CaptureHandler(logging.Handler):
            def emit(self, record):
                messages.append(record.getMessage())

        logger = logging.getLogger("infra.safe_call")
        logger.addHandler(CaptureHandler())
        logger.setLevel(logging.WARNING)

        def fail():
            raise RuntimeError("crash")

        safe_call(fail, fallback=None, err_label="read db")
        self.assertTrue(any("read db" in m for m in messages))


if __name__ == "__main__":
    unittest.main(verbosity=2)
