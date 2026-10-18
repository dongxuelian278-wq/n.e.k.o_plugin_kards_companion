import numpy as np

from kards_companion.perception.detector import (
    CLASS_TO_ID,
    Detection,
    UnitTracker,
    YoloDetector,
    assign_rows,
    nms,
)


def det(name: str, x1: float, y1: float, x2: float, y2: float, conf: float = 0.9) -> Detection:
    cid = CLASS_TO_ID[name]
    return Detection(cid, name, conf, x1, y1, x2, y2)


def make_yolo_output(boxes: list[tuple[str, float, float, float, float, float]]) -> np.ndarray:
    """ultralytics 布局 (1, 4+n_cls, n_anchors)，xywh 为 640 letterbox 坐标。"""
    n_cls = len(CLASS_TO_ID)
    out = np.zeros((1, 4 + n_cls, len(boxes)), dtype=np.float32)
    for i, (name, cx, cy, w, h, score) in enumerate(boxes):
        out[0, 0, i], out[0, 1, i], out[0, 2, i], out[0, 3, i] = cx, cy, w, h
        out[0, 4 + CLASS_TO_ID[name], i] = score
    return out


def test_postprocess_maps_back_and_filters_conf() -> None:
    out = make_yolo_output([
        ("unit_board", 320, 200, 80, 100, 0.8),   # 通过
        ("hq", 600, 100, 60, 90, 0.10),           # 低于阈值
        ("unit_board", 322, 202, 80, 100, 0.7),   # 与第一个重叠 -> NMS
    ])
    result = YoloDetector._postprocess(None, out, 1.0, 0, 0)
    assert len(result) == 1 and result[0].name == "unit_board"
    assert result[0].conf > 0.5 and abs(result[0].cx - 320) < 2


def test_nms_keeps_max_per_cluster() -> None:
    boxes = [det("unit_board", 0, 0, 100, 100, 0.9),
             det("unit_board", 5, 5, 105, 105, 0.5),
             det("unit_board", 300, 300, 400, 400, 0.8),
             det("card_hand", 0, 0, 100, 100, 0.7)]  # 跨类不抑制
    kept = nms(boxes)
    assert len(kept) == 3
    assert {(k.name, round(k.conf, 1)) for k in kept} == {("unit_board", 0.9), ("unit_board", 0.8),
                                                          ("card_hand", 0.7)}


def test_assign_rows_side_and_lane() -> None:
    units = [det("unit_board", 500, 200, 560, 280),   # 敌支援（线上方远处）
             det("unit_board", 600, 440, 660, 520),   # 敌前线（贴着线上缘）
             det("unit_board", 500, 520, 560, 600)]   # 我前线（贴着线下缘）
    fronts = [det("frontline", 200, 480, 1400, 540)]
    assignment = assign_rows(units + fronts, frame_h=1080)
    by_pos = {(round(d.cx), round(d.cy)): (side, lane) for d, side, lane in assignment.units}
    assert by_pos[(530, 240)] == ("enemy", "support")
    assert by_pos[(630, 480)] == ("enemy", "front")
    assert by_pos[(530, 560)] == ("mine", "front")
    assert assignment.source == "model"


def test_assign_rows_fallback_ratio() -> None:
    units = [det("unit_board", 500, 900, 560, 980)]
    assignment = assign_rows(units, frame_h=1080)
    assert assignment.units[0][1] == "mine"
    assert assignment.source == "fallback_ratio"


def test_unit_tracker_identity_and_expiry() -> None:
    tracker = UnitTracker()
    front = [det("frontline", 200, 480, 1400, 540)]

    def ids_at(y: float) -> list[tuple[int, float]]:
        rows = assign_rows(front + [det("unit_board", 500, y, 560, y + 80)], 1080)
        return [(t.instance_id, t.det.cy) for t in tracker.update(rows)]

    first = ids_at(300)
    assert len(first) == 1 and first[0][0] == 1
    moved = ids_at(310)  # 小幅移动 -> 同一 id
    assert moved[0][0] == 1
    gone = tracker.update(assign_rows(front, 1080))  # 漏检：宽限期保留
    assert len(gone) == 1
    for _ in range(3):
        gone = tracker.update(assign_rows(front, 1080))
    assert gone == []
    new = ids_at(300)  # 单位彻底消失后再出现 -> 新 id
    assert new[0][0] == 2
