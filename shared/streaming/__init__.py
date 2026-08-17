"""Streaming chat transport primitives (WS /chat/stream).

Frame shapes, per-turn cancellation tokens, and the single send choke-point are
kept here so the router stays a thin orchestration layer.
"""
