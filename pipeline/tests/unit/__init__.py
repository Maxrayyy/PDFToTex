"""Compatibility package for pipeline unit tests."""
"""Keep legacy relative test imports bound to the production module objects.

Some older tests use ``from . import llm`` while other tests import the same
code through ``texopt.optimization``.  Loading both paths independently makes
monkeypatches affect only one copy and produces false failures.
"""

import importlib
import sys


for _name in ("cli", "llm", "reconcile"):
    sys.modules.setdefault(
        f"{__name__}.{_name}", importlib.import_module(f"texopt.optimization.{_name}")
    )
