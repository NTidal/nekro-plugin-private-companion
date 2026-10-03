# -*- coding: utf-8 -*-
"""Source-scan tests for /陪伴 日程生成 wiring. Do not import handlers/plugin/on_command.

本地精简（NTidal）：已移除日程图卡渲染，故删除 render_schedule_card / qq_avatar_url /
SCHEDULE_CARD_ENABLED / RENDERER_URL 相关断言，保留日程生成命令接线与帮助文本断言。
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class TestScheduleGenerateCommandSource(unittest.TestCase):
    def test_handlers_wires_schedule_generate(self):
        src = ROOT.joinpath("handlers.py").read_text(encoding="utf-8")
        self.assertIn("日程生成", src)
        self.assertTrue(
            "format_user_error('日程生成失败'" in src
            or 'format_user_error("日程生成失败"' in src,
            "handlers.py must call format_user_error for 日程生成失败",
        )
        self.assertRegex(
            src,
            r'action == ["\']日程生成["\']',
            "must parse action == 日程生成",
        )
        self.assertIn('parts[1] == "生成"', src)
        help_m = re.search(r"HELP_TEXT\s*=\s*\"\"\"(.*?)\"\"\"", src, re.S)
        self.assertIsNotNone(help_m, "HELP_TEXT not found")
        assert help_m is not None
        self.assertIn("日程生成", help_m.group(1))

    def test_card_render_fully_removed(self):
        """精简后不应再有任何日程图卡/自拍/日记的残留引用。"""
        handlers = ROOT.joinpath("handlers.py").read_text(encoding="utf-8")
        plugin_src = ROOT.joinpath("plugin.py").read_text(encoding="utf-8")
        for gone in ("render_schedule_card", "qq_avatar_url", "selfie_draw", "visuals"):
            self.assertNotIn(gone, handlers, f"handlers.py 不应再引用 {gone}")
        for gone in ("SCHEDULE_CARD_ENABLED", "RENDERER_URL", "RENDER_TIMEOUT",
                     "SELFIE_ENABLED", "VISUALS_ENABLED", "DIARY_TIME"):
            self.assertNotIn(gone, plugin_src, f"plugin.py 不应再含配置 {gone}")


if __name__ == "__main__":
    unittest.main()
