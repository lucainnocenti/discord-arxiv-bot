"""Shortens abstracts that don't fit in a Discord message, through the Claude API.

Only used when the original abstract would have to be cut: the result keeps
the authors' own wording and style as far as possible, just condensed to the
space the message has left.
"""

import logging
from typing import Callable, Optional

import anthropic

MODEL = "claude-opus-5-5"
# A second attempt, told how long the first one came out, before giving up.
MAX_ATTEMPTS = 2

SYSTEM_PROMPT = (
    "You condense arXiv abstracts so they fit a length limit. Stay as close as "
    "possible to the original: keep the authors' wording, sentence structure, "
    "tone and first-person voice, and remove or merge the least essential "
    "phrases and sentences rather than paraphrasing. Keep technical terms, "
    "numbers and LaTeX notation (such as $...$) exactly as written. Do not add "
    "anything that is not in the original, and do not comment on the text. "
    "Reply with the condensed abstract only, as plain text."
)

logger = logging.getLogger(__name__)


class AbstractShortener:
    """Async `shorten(abstract, max_length) -> Optional[str]`.

    `measure` is the length function the caller checks the result with, so
    the model is told lengths in the same units the message limit uses.
    Returns None when the API call fails or no attempt fits.
    """

    def __init__(self, api_key: Optional[str], measure: Callable[[str], int]):
        # max_retries covers 429/5xx/connection errors with backoff.
        self.client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3)
        self.measure = measure

    async def shorten(self, abstract: str, max_length: int) -> Optional[str]:
        original_length = self.measure(abstract)
        # Models judge length loosely; aim a little under the hard limit.
        target = int(max_length * 0.9)
        request = (
            f"This abstract is {original_length} characters long. Condense it to "
            f"at most {target} characters (a hard limit of {max_length}), staying "
            f"as close to the original as you can.\n\n<abstract>\n{abstract}\n</abstract>"
        )
        messages = [{"role": "user", "content": request}]

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self.client.beta.messages.create(
                    model=MODEL,
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    messages=messages,
                    output_config={"effort": "medium"},
                    # Re-runs the request on another model if this one declines.
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                )
            except anthropic.APIError as e:
                logger.error(f"Claude API call to shorten an abstract failed: {type(e).__name__}: {e}")
                return None

            if response.stop_reason != "end_turn":
                logger.warning(f"Abstract shortening stopped early ({response.stop_reason}).")
                return None
            text = "".join(block.text for block in response.content if block.type == "text").strip()
            length = self.measure(text)
            logger.info(
                f"Shortened abstract from {original_length} to {length} characters "
                f"(limit {max_length}, attempt {attempt})."
            )
            if text and length <= max_length:
                return text

            # Append-only follow-up: the model sees its own attempt and how far over it was.
            messages = messages + [
                {"role": "assistant", "content": response.content},
                {"role": "user", "content": (
                    f"That version is {length} characters, over the limit of {max_length}. "
                    f"Condense it further, to at most {int(max_length * 0.8)} characters."
                )},
            ]

        logger.warning(f"Could not shorten the abstract below {max_length} characters.")
        return None
