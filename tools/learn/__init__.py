"""The self-learning loop: background reviews of conversations and the weekly memory curator.

The door hook lives in core/afterturn.py, the shared state in core/learnlog.py; review.py and curator.py
run detached (or from `archi learn`). digest.py turns the turn log into what the reviewer reads, prompts.py
holds the rules it gets, stores.py stages, validates and applies its edits, reviewer.py runs the CLI.
"""
