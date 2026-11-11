class ScriptError(Exception):
    """Raised when a script action fails or the script itself errors."""


class ScriptCancelled(Exception):
    """Raised when a running script is stopped before it finishes."""


class ScriptFinished(Exception):
    """Raised by the script itself to stop cleanly (e.g. ``script.finish()``)."""
