
from reply_handlers.auth import is_authorized_telegram, is_authorized_whatsapp


class TestTelegramAuth:
    def test_unset_env_denies(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_ALLOWED_USERS", raising=False)
        assert is_authorized_telegram(123) is False

    def test_empty_env_denies(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "")
        assert is_authorized_telegram(123) is False

    def test_match_in_csv_allows(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "123,456,789")
        assert is_authorized_telegram(456) is True

    def test_no_match_denies(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "123,456")
        assert is_authorized_telegram(999) is False

    def test_wildcard_allows_anyone(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "*")
        assert is_authorized_telegram(999999) is True

    def test_string_user_id_works(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_ALLOWED_USERS", "123")
        assert is_authorized_telegram("123") is True


class TestWhatsAppAuth:
    def test_unset_env_denies(self, monkeypatch):
        monkeypatch.delenv("WHATSAPP_ALLOWED_USERS", raising=False)
        assert is_authorized_whatsapp("34652029134@s.whatsapp.net") is False

    def test_match_by_number_allows(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "34652029134")
        assert is_authorized_whatsapp("34652029134@s.whatsapp.net") is True

    def test_match_by_full_jid_allows(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "34652029134@s.whatsapp.net")
        assert is_authorized_whatsapp("34652029134@s.whatsapp.net") is True

    def test_no_match_denies(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "34652029134")
        assert is_authorized_whatsapp("99999999999@s.whatsapp.net") is False

    def test_wildcard_allows_anyone(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_ALLOWED_USERS", "*")
        assert is_authorized_whatsapp("anyone@s.whatsapp.net") is True
