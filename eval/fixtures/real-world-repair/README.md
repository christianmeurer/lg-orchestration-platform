# Real-world repair fixture

Python HTTP handler with a latent bug. Used by the `real-world-repair` task family to verify that
the agent can identify and patch a `ZeroDivisionError` in `compute_rate` when `duration_seconds`
is zero, and that the updated code passes the accompanying regression test.
