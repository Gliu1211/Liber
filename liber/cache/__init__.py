"""Liber's content-addressed cache and on-disk blob store (ARCHITECTURE.md §7, §8).

Arrives in Session 4: content.py maps content_hash -> {tokens, summary, topic, blob path};
blobs.py stores offloaded content under ~/.liber/blobs/<hash[:2]>/<hash>.
"""
