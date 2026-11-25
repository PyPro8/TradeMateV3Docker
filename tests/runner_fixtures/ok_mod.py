import unittest


class Ok(unittest.TestCase):
    def test_one(self):
        self.assertTrue(True)

    def test_two(self):
        self.assertEqual(2, 1 + 1)
