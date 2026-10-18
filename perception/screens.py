"""界面分类器（Python 3.11）：OCR 关键词 + dHash 缓存门控。

不依赖 cv2/模板库：宿主自带 RapidOCR（PP-OCRv4 中文），关键词分类思路已被
旧版 v5 实测有效；坐标全部按窗口尺寸归一化，适配任意分辨率。
"""

import re
from typing import Any

from PIL import Image

from .capture import dhash, hamming
from ..state.model import (
    SCENE_DEFEAT,
    SCENE_DRAFT,
    SCENE_MATCH,
    SCENE_MENU,
    SCENE_UNKNOWN,
    SCENE_VICTORY,
)

_WIN_PAT = re.compile(r"victory|you\s*win|胜利", re.I)
_LOSE_PAT = re.compile(r"defeat|you\s*lose|战败|失败", re.I)
_END_TURN_PAT = re.compile(r"结束回合|endturn|end\s*turn", re.I)
_MULLIGAN_PAT = re.compile(r"mulligan|换牌|重新抽", re.I)

_MENU_WORDS = (
    "collection", "store", "deckbuilder", "battlelogs", "singleplayer", "arena",
    "dailymissions", "mainmenu", "settings", "shop", "news", "profile",
    "收藏", "商店", "卡组", "构筑", "战斗日志", "单人", "竞技场", "轮抽", "每日任务",
    "主菜单", "设置", "对战", "新闻", "开始",
)
_FP_TOLERANCE = 6


def _join_text(ocr_result: Any) -> str:
    """把 RapidOCR 结果拼成去空白小写串（艺术字体常在词内插空格）。"""
    if not ocr_result:
        return ""
    lines = ocr_result[0] if isinstance(ocr_result, tuple) else ocr_result
    if not lines:
        return ""
    parts: list[str] = []
    for item in lines:
        try:
            parts.append(str(item[1]))
        except (IndexError, TypeError):
            continue
    return "".join(parts).replace(" ", "").lower()


class ScreenClassifier:
    """按画面指纹缓存分类结果，静态画面零成本。

    classify 返回 (scene, my_turn)：scene==SCENE_MATCH 时 my_turn 才可信
    （OCR 读到「结束回合」按钮 = 我方回合；读不到 = 敌回合或未知）。
    """

    def __init__(self, ocr: Any, *, fp_tolerance: int = _FP_TOLERANCE) -> None:
        self._ocr = ocr
        self._tolerance = int(fp_tolerance)
        self._last_fp: int | None = None
        self._scene = SCENE_UNKNOWN
        self._my_turn = False

    @property
    def scene(self) -> str:
        return self._scene

    def classify(self, image: Image.Image) -> tuple[str, bool]:
        fp = dhash(image)
        if self._last_fp is not None and hamming(fp, self._last_fp) <= self._tolerance:
            return self._scene, self._my_turn
        self._last_fp = fp
        scene, my_turn = self._classify_uncached(image)
        self._scene, self._my_turn = scene, my_turn
        return scene, my_turn

    # ------------------------------------------------------------- internals

    def _run_ocr(self, image: Image.Image) -> str:
        import numpy as np
        if self._ocr is None:
            return ""
        try:
            result, _ = self._ocr(np.asarray(image.convert("RGB")))
            return _join_text(result)
        except Exception:
            return ""

    def _classify_uncached(self, image: Image.Image) -> tuple[str, bool]:
        w, h = image.size
        center = self._run_ocr(image.crop((int(w * 0.2), int(h * 0.18), int(w * 0.8), int(h * 0.8))))
        if _WIN_PAT.search(center):
            return SCENE_VICTORY, False
        if _LOSE_PAT.search(center):
            return SCENE_DEFEAT, False
        if _MULLIGAN_PAT.search(center):
            return SCENE_DRAFT, False

        right_band = self._run_ocr(image.crop((int(w * 0.84), int(h * 0.5), w, int(h * 0.75))))
        if _END_TURN_PAT.search(right_band):
            return SCENE_MATCH, True

        top_band = self._run_ocr(image.crop((int(w * 0.25), 0, w, int(h * 0.12))))
        joined = center + right_band + top_band
        menu_hits = sum(1 for word in _MENU_WORDS if word in joined)
        if menu_hits >= 2:
            return SCENE_MENU, False
        if self._run_ocr(image.crop((int(w * 0.3), 0, int(w * 0.72), int(h * 0.1)))):
            # 顶部文字带（敌方手牌区指示等）存在 → 大概率敌方回合对局中
            return SCENE_MATCH, False
        return SCENE_UNKNOWN, False

    def invalidate(self) -> None:
        self._last_fp = None
        self._scene, self._my_turn = SCENE_UNKNOWN, False
