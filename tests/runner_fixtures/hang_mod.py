import time
import unittest


class Hang(unittest.TestCase):
    def test_hang(self):
        time.sleep(60)
