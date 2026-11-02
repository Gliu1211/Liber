"""Conversation identity and storage (ARCHITECTURE.md D7, §8).

- store.py: SQLite access (Session 1: the `requests` table).
- chain.py (Session 2): canonicalization and the message hash chain that identifies sessions.
"""
