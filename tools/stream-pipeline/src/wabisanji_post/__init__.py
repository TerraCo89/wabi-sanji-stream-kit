"""Post-stream pipeline: stream recordings → per-game episodes, Shorts, clips.

Spec: ``docs/post-stream-spec.md``. Commands are registered on the main Typer
app as ``stream-*`` (see :mod:`wabisanji_post.cli`; run ``wabisanji-post`` or ``python pipeline.py``).
"""
