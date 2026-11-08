"""Version numbers of Maestro releases, compared without a packaging library.

Only the forms this project publishes are understood: ``X.Y.Z`` for a formal release,
``X.Y.Z.devN`` between releases and ``X.Y.ZaN`` / ``bN`` / ``rcN`` should a
pre-release ever be made; a ``+local`` suffix is ignored. For the same ``X.Y.Z`` the order
is dev < a < b < rc < formal, so an instance running a development build of 0.5.0 is
offered 0.5.0 but not 0.4.x.
"""

import re

FORMAL = re.compile(r"\d+\.\d+\.\d+")
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:(a|b|rc)(\d+))?(?:\.dev(\d+))?(?:\+[A-Za-z0-9.]+)?")
_STAGES = {"a": 1, "b": 2, "rc": 3}


def key(text: str) -> tuple | None:
    """A sortable key for ``text``, or None when it is not a version this project uses."""
    found = _VERSION.fullmatch(text.strip())
    if found is None:
        return None
    major, minor, patch, stage, number, dev = found.groups()
    release = (int(major), int(minor), int(patch))
    if dev is not None:
        return (*release, 0, int(dev))
    if stage is not None:
        return (*release, _STAGES[stage], int(number))
    return (*release, 4, 0)


def newer(candidate: str, current: str) -> bool:
    """Whether ``candidate`` comes after ``current``; False when either cannot be read."""
    ours, theirs = key(current), key(candidate)
    return ours is not None and theirs is not None and theirs > ours
