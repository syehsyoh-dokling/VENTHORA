"""Specialist engines behind the orchestrator: Verity, Horizon and Theorem.

Each engine is a pure async function over a TreasuryState. An externally hosted
engine can replace the local one through `adapters.py` without being trusted to
widen what the local engine allows.
"""
