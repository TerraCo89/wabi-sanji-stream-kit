from wabisanji_post import facecam


def test_cam_crop_centres_on_median_face_and_keeps_aspect():
    faces = [(1600, 850, 120), (1610, 860, 118), (1595, 845, 122)]
    x, y, w, h = facecam.cam_crop(faces, aspect=260 / 400)
    assert abs((x + w / 2) - 1600) <= 2
    assert abs(h / w - 0.65) < 0.01
    assert x + w <= 1920 and y + h <= 1080


def test_cam_crop_drops_outliers_and_needs_two_detections():
    assert facecam.cam_crop([(1600, 850, 120)], aspect=0.65) is None
    crop = facecam.cam_crop([(1600, 850, 120), (1605, 852, 118), (1000, 500, 90)], aspect=0.65)
    assert crop is not None and crop[0] > 1300


def test_cam_crop_clamps_to_frame():
    x, y, w, h = facecam.cam_crop([(1900, 1070, 200), (1905, 1072, 200)], aspect=0.65)
    assert x + w <= 1920 and y + h <= 1080
    assert w == facecam.MAX_W - facecam.MAX_W % 2 or w <= facecam.MAX_W
