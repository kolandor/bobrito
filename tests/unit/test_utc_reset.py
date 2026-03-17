"""Unit tests for UTC daily reset behavior (get_trading_day_utc)."""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from bobrito.risk.manager import get_trading_day_utc


class TestGetTradingDayUtc:
    def test_returns_date_type(self):
        assert isinstance(get_trading_day_utc(), date)

    @patch("bobrito.risk.manager.datetime")
    def test_uses_utcnow(self, mock_dt):
        mock_dt.utcnow.return_value = datetime(2026, 3, 15, 14, 30, 0)
        result = get_trading_day_utc()
        assert result == date(2026, 3, 15)
        mock_dt.utcnow.assert_called()
