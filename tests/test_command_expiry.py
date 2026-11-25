"""A command's expiry must never be SHORTER than the time TradeMate waits for it (regression: int() truncation made a
2 s window as short as ~1 s, so commands expired under load and tests failed intermittently)."""
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trademate.broker.mt5_commands import CommandChannel
from trademate.errors import CommandWithdrawnError


class ExpiryNeverShorterThanRequested(unittest.TestCase):
    def test_expiry_rounds_up_never_down(self):
        captured = []
        real_replace = __import__("os").replace

        def spy(src, dst):
            captured.append(Path(src).read_text(encoding="utf-8"))
            return real_replace(src, dst)

        for now, window, expected in ((1000.0, 2.0, 1002), (1000.4, 2.0, 1003), (1000.99, 2.0, 1003), (1000.01, 15.0, 1016)):
            captured.clear()
            with tempfile.TemporaryDirectory() as d, mock.patch("trademate.broker.mt5_commands.os.replace", spy), \
                    mock.patch("trademate.broker.mt5_commands.time.time", return_value=now):
                try:
                    CommandChannel(Path(d)).send({"cmd": "open"}, timeout=0.02, expires_in=window)
                except CommandWithdrawnError:
                    pass
            expires = int(re.search(r"^expires=(\d+)$", captured[0], re.M).group(1))
            self.assertEqual(expires, expected, (now, window))
            self.assertGreaterEqual(expires, now + window)          # never earlier than the window asked for


if __name__ == "__main__":
    unittest.main()
