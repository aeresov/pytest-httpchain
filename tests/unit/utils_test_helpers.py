"""Importable user functions for the ``utils`` tests.

A plain module rather than helpers defined in the test module itself: user
functions are referenced by dotted import string, and pointing that string at
the test module makes the importer load a *second* copy of it under
``--import-mode=importlib``. Keeping them here means the string names a module
whose only job is to be imported.
"""


def func_with_args(a, b, c=None):
    return {"a": a, "b": b, "c": c}


def add_numbers(x, y):
    return x + y
