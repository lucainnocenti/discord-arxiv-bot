# utils.py
import re
from typing import List

AUTHOR_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)


def author_names_match(left: str, right: str) -> bool:
    """Matches author names across common initial and middle-name variations."""
    left_clean = left.strip().casefold()
    right_clean = right.strip().casefold()
    if not left_clean or not right_clean:
        return False
    if left_clean == right_clean:
        return True

    left_tokens = _author_name_tokens(left_clean)
    right_tokens = _author_name_tokens(right_clean)
    if len(left_tokens) < 2 or len(right_tokens) < 2:
        return False

    if left_tokens[-1] != right_tokens[-1]:
        return False

    if not _author_tokens_compatible(left_tokens[0], right_tokens[0]):
        return False

    left_middle = left_tokens[1:-1]
    right_middle = right_tokens[1:-1]
    for left_token, right_token in zip(left_middle, right_middle):
        if not _author_tokens_compatible(left_token, right_token):
            return False

    return True


def find_matching_target_authors(paper_authors: List[str], target_authors: List[str]) -> List[str]:
    """Returns canonical configured target authors found in the paper author list."""
    matched_targets: List[str] = []
    seen_targets = set()

    for paper_author in paper_authors:
        for target_author in target_authors:
            if target_author in seen_targets:
                continue
            if author_names_match(paper_author, target_author):
                matched_targets.append(target_author)
                seen_targets.add(target_author)
                break

    return matched_targets


def _author_name_tokens(name: str) -> List[str]:
    return AUTHOR_TOKEN_PATTERN.findall(name)


def _author_tokens_compatible(left: str, right: str) -> bool:
    if left == right:
        return True

    # `left` is the paper author and `right` is the configured target author.
    # Initials in the paper can match full configured names, but a configured
    # initial-only alias should not match a different full first name.
    if len(left) == 1 and right.startswith(left):
        return True

    return False
