import unittest


class Mixed(unittest.TestCase):
    def test_passes(self):
        pass

    def test_fails(self):
        self.assertEqual(1, 2, "<img src=x onerror=alert(1)> expected 1 got 2")

    @unittest.skip("SKIPPED: needs hardware <b>x</b>")
    def test_skipped(self):
        pass
