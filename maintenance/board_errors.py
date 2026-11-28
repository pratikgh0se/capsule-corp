"""The Board exception hierarchy; `board` re-exports
every name, so `board.BoardError` etc. keep resolving."""


class BoardError(Exception):
    """Anything that went wrong between Capsule and a kanban board."""


class CardNotFound(BoardError):
    """show/list found no such task id."""


class LegacyCliError(BoardError):
    """The CLI failed: non-zero exit, timeout, or unparseable output."""

    def __init__(self, message, *, argv=(), returncode=None, stderr=""):
        super().__init__(message)
        self.argv = [str(a).split("\n", 1)[0] for a in argv]
        self.returncode = returncode
        self.stderr = (stderr or "")[:200]


class LegacyOutputInvalid(LegacyCliError):
    """The CLI exited 0 and printed something that is not the JSON we asked
    for. Its own class because the retired merge gate's card loader had to keep turning exactly
    this case into `{}` while a real CLI failure stays an InfraError."""


class TransitionRefused(BoardError):
    """A write the old board's transition rules would refuse."""


class Locked(BoardError):
    """Another process holds this card's lock."""


class BoardFrozen(BoardError):
    """The board carries `.capsule-owned` (C29): every write verb refuses; reads keep working."""
