# Approval flow fixture

A patch that requires operator approval before applying. Used by the `approval-suspend-resume`
task to verify that the policy gate suspends execution when a mutation targets production
infrastructure, exposes checkpoint metadata, and waits for explicit operator action before
continuing.
