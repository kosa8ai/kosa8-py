"""The version is the same in pyproject.toml, kosa8.__version__ and the User-Agent."""

import os
import re
import unittest

import kosa8
from kosa8.cloud import USER_AGENT

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Version(unittest.TestCase):
    def test_one_version(self):
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as f:
            text = f.read()
        want = re.search(r'^version = "([^"]+)"', text, re.M).group(1)
        self.assertEqual(kosa8.__version__, want)
        self.assertEqual(USER_AGENT, f"kosa8-python/{want}")

    def test_description_leads_with_the_tagline(self):
        with open(os.path.join(ROOT, "pyproject.toml"), encoding="utf-8") as f:
            desc = re.search(r'^description = "([^"]+)"', f.read(), re.M).group(1)
        self.assertTrue(desc.startswith("Zero Trust cybersecurity infrastructure for AI agents"), desc)


if __name__ == "__main__":
    unittest.main()
