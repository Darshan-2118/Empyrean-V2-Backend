"""Tsukamoto fuzzy inference engine — public API.

Canonical import (L37): ``from fuzzy import infer, fuzzy_score`` — this
package re-export is the public surface. ``fuzzy.tsukamoto`` is the
implementation module; importing from it directly also works (some internal
consumers do) but is not the documented path.
"""

from __future__ import annotations

from fuzzy.tsukamoto import fuzzy_score, infer

__all__ = ["fuzzy_score", "infer"]
