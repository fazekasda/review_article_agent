"""Deliberately failing probe: proves CI turns red. Never merge."""
import unittest


class CiRedProbe(unittest.TestCase):
    def test_deliberate_failure(self):
        self.fail("deliberate CI red-check probe")
