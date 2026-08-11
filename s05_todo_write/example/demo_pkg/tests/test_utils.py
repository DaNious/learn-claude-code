"""Tests for the demo_pkg utilities."""

import unittest

from demo_pkg.utils import add, to_uppercase


class TestAdd(unittest.TestCase):
    """Test cases for the add function."""

    def test_positive(self) -> None:
        """Test that positive integers are summed correctly."""
        self.assertEqual(add(2, 3), 5)

    def test_negative(self) -> None:
        """Test that negative integers are summed correctly."""
        self.assertEqual(add(-1, -2), -3)

    def test_zero(self) -> None:
        """Test that adding zero returns the other addend."""
        self.assertEqual(add(0, 0), 0)


class TestToUppercase(unittest.TestCase):
    """Test cases for the to_uppercase function."""

    def test_lowercase(self) -> None:
        """Test that lowercase text is converted to uppercase."""
        self.assertEqual(to_uppercase("hello"), "HELLO")

    def test_mixed_case(self) -> None:
        """Test that mixed-case text is converted to uppercase."""
        self.assertEqual(to_uppercase("HeLLo WoRlD"), "HELLO WORLD")

    def test_empty_string(self) -> None:
        """Test that an empty string stays empty."""
        self.assertEqual(to_uppercase(""), "")


if __name__ == "__main__":
    unittest.main()
