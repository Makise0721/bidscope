"""BidScope LLM port layer.

Three async protocols (:mod:`bidscope.llm.ports`) bound to two implementations:

* :mod:`bidscope.llm.fake` — fully offline, deterministic provider used by the
  public demo and by tests.
* :mod:`bidscope.llm.deepseek` — OpenAI-compatible provider used only when
  real-model mode is explicitly configured and authorized on the server.
"""
