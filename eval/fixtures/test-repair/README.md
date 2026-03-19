# Test repair fixture

Python module with a broken test. Used by the `test-repair` task family to verify that the agent
can locate a deliberately wrong assertion in `tests/test_calculator.py` and produce a patch that
corrects it so the full test suite passes.
