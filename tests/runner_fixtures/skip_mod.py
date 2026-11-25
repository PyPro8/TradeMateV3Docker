import unittest


@unittest.skip("SKIPPED: dependency missing")
class AllSkipped(unittest.TestCase):
    def test_a(self):
        pass
