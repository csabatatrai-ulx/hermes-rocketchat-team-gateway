import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rc_markdown import escape_lone_tildes  # noqa: E402


class TestEscapeLoneTildes(unittest.TestCase):
    """Rocket.Chat strikes through ``~text~``; ``\\~`` is a plain tilde."""

    def test_the_context_gauge_lines_are_not_struck_through(self):
        line = "■ System prompt ~    2,276 token ~  1.7%"
        self.assertEqual(escape_lone_tildes(line), "■ System prompt \\~    2,276 token \\~  1.7%")
        self.assertEqual(escape_lone_tildes("Kontextusablak: ~8,587 / 131,072 token (~7%)"),
                         "Kontextusablak: \\~8,587 / 131,072 token (\\~7%)")

    def test_markdown_strikethrough_stays(self):
        self.assertEqual(escape_lone_tildes("this is ~~obsolete~~, this is not"), "this is ~~obsolete~~, this is not")

    def test_code_and_urls_are_untouched(self):
        text = "look: `~/.hermes` and https://example.org/~user/x, then ~5 minutes\n```\ncd ~/proj\n```"
        self.assertEqual(escape_lone_tildes(text),
                         "look: `~/.hermes` and https://example.org/~user/x, then \\~5 minutes\n```\ncd ~/proj\n```")

    def test_an_unclosed_fence_while_streaming_is_protected(self):
        self.assertEqual(escape_lone_tildes("~1 minute\n```bash\ncd ~/p"), "\\~1 minute\n```bash\ncd ~/p")

    def test_already_escaped_and_tilde_free_text_is_unchanged(self):
        self.assertEqual(escape_lone_tildes("a \\~ b"), "a \\~ b")
        self.assertEqual(escape_lone_tildes("none in here"), "none in here")
        self.assertEqual(escape_lone_tildes(""), "")

    def test_escaping_twice_is_the_same(self):
        once = escape_lone_tildes("~ 2 token ~")
        self.assertEqual(escape_lone_tildes(once), once)


if __name__ == "__main__":
    unittest.main()
