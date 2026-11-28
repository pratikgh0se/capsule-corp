"""`unavailable._names` matches a path only at a path boundary, not as a bare prefix."""
import unittest

from capsule.blind import unavailable


class NamesCase(unittest.TestCase):
    BLIND = "/var/spool/capsule/run1/blind"

    def test_the_path_itself_is_named_whatever_punctuation_follows_it(self):
        b = self.BLIND
        for text in (b, f"mount {b}/x failed", f"bad {b} here", f"source '{b}' missing",
                     f'source "{b}"', f"mkdir {b}: permission denied", f"'{b}:/tests:ro'",
                     f"does not exist: {b}.", f"mount ({b})", f"{b}, x", f'said \\"{b}\\"'):
            self.assertTrue(unavailable._names(text, [b]), text)

    def test_a_longer_sibling_with_the_same_prefix_is_not_named(self):
        for text in (f"mount {self.BLIND}-out/x failed", f"bad {self.BLIND}out here",
                     f"see {self.BLIND}.toml"):
            self.assertFalse(unavailable._names(text, [self.BLIND]), text)


if __name__ == "__main__":
    unittest.main()
