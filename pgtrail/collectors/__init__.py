"""One module per layer of the engine.

Each collector turns raw evidence (protocol trace, server notices, system
views, strace output) into plain dicts and lists, so the whole trace can be
dumped as JSON and rendered by any front end.
"""
