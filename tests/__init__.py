"""Test package.

`__init__.py` is here so that `tests.conftest` has exactly one module name.
Without it, pytest imports the file as `conftest` while an explicit
`from tests.conftest import ...` makes mypy see `tests.conftest`, and mypy
refuses the ambiguity.
"""
