import time
import unittest


class Slow(unittest.TestCase):
    def test_slow(self):
        time.sleep(1.5)
