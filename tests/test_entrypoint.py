"""包入口点的冒烟测试。"""

from __future__ import annotations

import logging

import pytest

from mining_daily_agent import main


def test_main_logs_startup_message(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        main()

    assert "mining-daily-agent" in caplog.text
