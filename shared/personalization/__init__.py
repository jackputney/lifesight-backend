"""Adaptive personalization foundation — summaries (evidence) + pending proposals.

Proposals are always written pending and reviewed by a human in Oliver admin;
this package never writes the user_prompt_overrides table.
"""

from shared.personalization import store as store
from shared.personalization import summarize as summarize
from shared.personalization import proposals as proposals

__all__ = ["proposals", "store", "summarize"]
