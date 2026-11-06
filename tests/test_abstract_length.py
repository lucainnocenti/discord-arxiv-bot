import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic

from abstract_shortener import AbstractShortener
from discord_formatter import MAX_DISCORD_MSG_LEN, TRUNCATION_SUFFIX, discord_length, format_new_paper
from tests.test_publication_tracking import DEFAULT_AUTHORS, make_paper


class FakeShortener:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    async def shorten(self, abstract, max_length):
        self.calls.append((abstract, max_length))
        return self.result(max_length) if callable(self.result) else self.result


def room_for_abstract(paper, et_al=False):
    """The exact number of characters the message leaves for the abstract."""
    if et_al:
        paper = paper._replace(authors=[f"{paper.authors[0]} et al."])
    return MAX_DISCORD_MSG_LEN - discord_length(asyncio.run(format_new_paper(paper._replace(summary=""), DEFAULT_AUTHORS)))


class AbstractLengthTests(unittest.TestCase):
    def test_discord_length_counts_astral_characters_twice(self):
        self.assertEqual(discord_length("abc"), 3)
        self.assertEqual(discord_length("é"), 1)
        self.assertEqual(discord_length("𝒪(n)"), 5)

    def test_abstract_that_exactly_fits_is_posted_unchanged(self):
        paper = make_paper()
        paper = paper._replace(summary="a" * room_for_abstract(paper))
        shortener = FakeShortener("should not be used")

        message = asyncio.run(format_new_paper(paper, DEFAULT_AUTHORS, shortener))

        self.assertEqual(discord_length(message), MAX_DISCORD_MSG_LEN)
        self.assertIn(paper.summary, message)
        self.assertEqual(shortener.calls, [])

    def test_abstract_that_fits_only_with_et_al_is_posted_unchanged(self):
        paper = make_paper()
        paper = paper._replace(summary="a" * (room_for_abstract(paper) + 1))
        shortener = FakeShortener("should not be used")

        message = asyncio.run(format_new_paper(paper, DEFAULT_AUTHORS, shortener))

        self.assertIn(paper.summary, message)
        self.assertIn("**Authors:** Alice Example et al.\n", message)
        self.assertEqual(shortener.calls, [])

    def test_one_character_over_calls_the_shortener_with_the_exact_room(self):
        paper = make_paper()
        room = room_for_abstract(paper)
        paper = paper._replace(summary="a" * (room_for_abstract(paper, et_al=True) + 1))
        shortener = FakeShortener("A condensed abstract.")

        message = asyncio.run(format_new_paper(paper, DEFAULT_AUTHORS, shortener))

        self.assertEqual(shortener.calls, [(paper.summary, room)])
        self.assertIn("**Abstract:** A condensed abstract.\n", message)
        self.assertIn("Alice Example, Bob Example", message)

    def test_astral_characters_count_against_the_room(self):
        paper = make_paper()
        # Fits by code points, not by UTF-16 units.
        paper = paper._replace(summary="𝒪" * (room_for_abstract(paper, et_al=True) // 2 + 1))
        shortener = FakeShortener("short")
        asyncio.run(format_new_paper(paper, DEFAULT_AUTHORS, shortener))
        self.assertEqual(len(shortener.calls), 1)

    def test_falls_back_to_word_boundary_truncation_without_a_shortener(self):
        paper = make_paper(summary="word " * 500)
        message = asyncio.run(format_new_paper(paper, DEFAULT_AUTHORS))

        self.assertLessEqual(discord_length(message), MAX_DISCORD_MSG_LEN)
        self.assertIn("word" + TRUNCATION_SUFFIX + "\n", message)

    def test_falls_back_to_truncation_when_the_shortener_fails(self):
        paper = make_paper(summary="word " * 500)
        message = asyncio.run(format_new_paper(paper, DEFAULT_AUTHORS, FakeShortener(None)))
        self.assertIn(TRUNCATION_SUFFIX, message)
        self.assertLessEqual(discord_length(message), MAX_DISCORD_MSG_LEN)

    def test_long_author_list_becomes_et_al_when_it_leaves_too_little_room(self):
        authors = ["Alice Example"] + [f"Coauthor Number{i}" for i in range(80)]
        paper = make_paper(authors=authors, summary="x" * 1950)
        shortener = FakeShortener(lambda room: "y" * room)

        message = asyncio.run(format_new_paper(paper, DEFAULT_AUTHORS, shortener))

        self.assertIn("**Authors:** Alice Example et al.\n", message)
        self.assertEqual(discord_length(message), MAX_DISCORD_MSG_LEN)

    def test_long_author_list_is_kept_when_the_abstract_fits(self):
        authors = ["Alice Example"] + [f"Coauthor Number{i}" for i in range(80)]
        paper = make_paper(authors=authors, summary="Short abstract.")
        message = asyncio.run(format_new_paper(paper, DEFAULT_AUTHORS))
        self.assertIn("Coauthor Number79", message)


def api_response(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)])


class AbstractShortenerTests(unittest.TestCase):
    def make(self, *responses):
        shortener = AbstractShortener("test-key", measure=discord_length)
        shortener.client.beta.messages.create = AsyncMock(side_effect=list(responses))
        return shortener

    def test_returns_a_result_within_the_limit(self):
        shortener = self.make(api_response("Short enough."))
        self.assertEqual(asyncio.run(shortener.shorten("x" * 500, 100)), "Short enough.")
        kwargs = shortener.client.beta.messages.create.await_args.kwargs
        self.assertEqual(kwargs["model"], "claude-opus-5-5")
        self.assertIn("x" * 500, kwargs["messages"][0]["content"])

    def test_retries_once_when_the_first_attempt_is_too_long(self):
        shortener = self.make(api_response("z" * 150), api_response("Now short."))
        self.assertEqual(asyncio.run(shortener.shorten("x" * 500, 100)), "Now short.")
        second = shortener.client.beta.messages.create.await_args_list[1].kwargs["messages"]
        self.assertIn("150 characters", second[-1]["content"])

    def test_gives_up_after_two_long_attempts(self):
        shortener = self.make(api_response("z" * 150), api_response("z" * 120))
        self.assertIsNone(asyncio.run(shortener.shorten("x" * 500, 100)))

    def test_refusal_or_api_error_returns_none(self):
        self.assertIsNone(asyncio.run(self.make(api_response("", "refusal")).shorten("x" * 500, 100)))
        error = anthropic.APIConnectionError(request=SimpleNamespace(method="POST", url="x"))
        self.assertIsNone(asyncio.run(self.make(error).shorten("x" * 500, 100)))


if __name__ == "__main__":
    unittest.main()
