"""Unit tests for UI authentication helpers."""

from __future__ import annotations

from bobrito.ui.auth import check_credentials


def _settings(username: str = "admin", password: str = "secret"):
    """Build a minimal mock settings object."""

    class FakeSettings:
        web_ui_username = username
        web_ui_password = password

    return FakeSettings()


class TestCheckCredentials:
    def test_correct_credentials_returns_true(self):
        assert check_credentials("admin", "secret", _settings()) is True

    def test_wrong_password_returns_false(self):
        assert check_credentials("admin", "wrong", _settings()) is False

    def test_wrong_username_returns_false(self):
        assert check_credentials("hacker", "secret", _settings()) is False

    def test_both_wrong_returns_false(self):
        assert check_credentials("x", "y", _settings()) is False

    def test_empty_credentials_returns_false(self):
        assert check_credentials("", "", _settings()) is False

    def test_case_sensitive_username(self):
        assert check_credentials("Admin", "secret", _settings()) is False

    def test_case_sensitive_password(self):
        assert check_credentials("admin", "Secret", _settings()) is False

    def test_custom_credentials(self):
        s = _settings(username="operator", password="complex_pass_123!")
        assert check_credentials("operator", "complex_pass_123!", s) is True
        assert check_credentials("operator", "wrong", s) is False

    def test_unicode_credentials(self):
        s = _settings(username="użytkownik", password="hasło123")
        assert check_credentials("użytkownik", "hasło123", s) is True
        assert check_credentials("uzytkownik", "haslo123", s) is False
