"""BidScope LangGraph workflow plane.

A single bounded graph (:func:`build_graph`) owns each query run. The first
six nodes — intent parsing, validation, human confirmation, retrieval-plan
construction, candidate retrieval, and duplicate resolution — are frozen at
``candidates_resolved`` by this layer; evidence verification, report synthesis
and delivery extend it in later tasks.

The graph depends only on the
:mod:`bidscope.llm.ports` protocols and the frozen
:mod:`bidscope.retrieval` / :mod:`bidscope.domain` contracts, so the public
demo and the test suite run it fully offline on fake ports.
"""
