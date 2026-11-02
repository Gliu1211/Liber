"""Liber: a local API proxy that lets you decide what stays in an LLM's context.

See ARCHITECTURE.md for the design. The client (e.g. Claude Code) points its base URL at
Liber; Liber forwards each request to the real API and streams the answer straight back.
"""
