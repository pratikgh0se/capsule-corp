"""inc_8b4a0a4b: an `answer` that narrows a card changes the contract, and only `amend` carries a
change to the card body. The writer refuses such an answer and points to `amend`."""
import re

NARROWING = re.compile(
    r"\b(?:do\s+only|only\b.{0,60}?\b(?:is|are)\s+left|nothing\s+else|change\s+only"
    r"|only\s+these|no\s+other\s+change)\b", re.IGNORECASE | re.DOTALL)
SKIPPED = "[scope lint skipped] "


def require(kind, text, lint):
    """The text to write. Raises ValueError for an `answer` that narrows scope while `lint` is
    on; with `lint` off, an `answer` is marked as unchecked."""
    if kind != "answer":
        return text
    found = NARROWING.search(text or "") if lint else None
    if found:
        raise ValueError(
            f"this answer narrows the card ({found.group(0)!r}), and only the card body carries "
            "scope: amend the body first with `human amend --body-file <path> '<reason>'`, or "
            "pass --no-lint to send the answer as it is")
    return text if lint else SKIPPED + (text or "")
