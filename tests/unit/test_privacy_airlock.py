"""
tests/unit/test_privacy_airlock.py - Unit tests for PrivacyAirlock PII and secret scrubbing.
"""

import pytest

from core.storage import PrivacyAirlock


@pytest.mark.unit
@pytest.mark.security
def test_privacy_airlock_scrubs_all_sensitive_categories() -> None:
    """PrivacyAirlock must scrub email, IP, API secrets, URLs, and local paths."""
    raw_text = (
        "Developer alice@example.com connected from 192.168.0.105 with key sk-abcdef1234567890abcdef "
        "and requested https://internal.company.corp:8443/secrets while storing logs at C:\\Users\\Alice\\token.txt"
    )
    sanitized = PrivacyAirlock.sanitize(raw_text)

    # Assert raw sensitive information is wiped
    assert "alice@example.com" not in sanitized
    assert "192.168.0.105" not in sanitized
    assert "sk-abcdef1234567890abcdef" not in sanitized
    assert "https://internal.company.corp" not in sanitized
    assert "C:\\Users\\Alice\\token.txt" not in sanitized

    # Assert redaction tags are inserted
    assert "<EMAIL>" in sanitized
    assert "<IP>" in sanitized
    assert "<SECRET>" in sanitized
    assert "<URL>" in sanitized
    assert "<PATH>" in sanitized


@pytest.mark.unit
def test_privacy_airlock_preserves_clean_text() -> None:
    """Benign coding advice without PII should remain intact."""
    clean = "Use Python list comprehensions to filter items efficiently."
    assert PrivacyAirlock.sanitize(clean) == clean
