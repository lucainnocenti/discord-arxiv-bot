import contextlib
import io
import unittest
from unittest.mock import patch

from settings import _parse_args


def parse(*argv):
    with patch("sys.argv", ["bot.py", *argv]):
        return _parse_args()


class FlagTests(unittest.TestCase):
    def test_defaults(self):
        args = parse()
        self.assertEqual(args.days, 7)
        self.assertFalse(args.rss or args.api or args.nosave or args.nosend)

    def test_invalid_combinations_are_rejected(self):
        for argv in (["--rss", "--api"], ["--nomembersync", "--forcemembersync"], ["--days", "0"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit), \
                    contextlib.redirect_stderr(io.StringIO()):
                parse(*argv)


if __name__ == "__main__":
    unittest.main()
