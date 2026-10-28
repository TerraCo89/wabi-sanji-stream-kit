import shutil

import pytest

# Tests that render or probe real (tiny) videos.
NEEDS_FFMPEG = {
    "test_inventory_classifies_and_flags",
    "test_inventory_marks_session_recordings",
    "test_cut_segment_joins_parts_on_real_video",
    "test_cut_segment_with_banner_on_real_video",
    "test_cut_segment_with_blur_on_real_video",
}


def pytest_collection_modifyitems(config, items):
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    skip = pytest.mark.skip(reason="needs ffmpeg and ffprobe on PATH")
    for item in items:
        if item.originalname in NEEDS_FFMPEG:
            item.add_marker(skip)
