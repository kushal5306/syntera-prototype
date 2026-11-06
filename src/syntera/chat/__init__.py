"""Chat-first Syntera: a plain-language conversation that drives the deterministic engine.

A language model (or, without a key, a small rule-based stand-in) turns what the person
writes into calls of the tools in :mod:`syntera.chat.tools`. Every number and verdict in a
reply comes from those tools, which run the same deterministic layout, routing and
verification code as the expert pages.
"""
