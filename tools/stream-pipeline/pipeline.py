"""Run the post-stream pipeline without installing it: ``python pipeline.py stream-<cmd> ...``.

Same as the ``wabisanji-post`` console script.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__":
    src = Path(__file__).resolve().parent / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from wabisanji_post.cli import main

    main()
