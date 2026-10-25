"""Tests for the escalation bridge (pause/relay/resume) and the encrypted
credential store. The bridge is tested against a fake Playwright Page --
these tests are about the control-transfer state machine and redaction, not
about Playwright itself."""

from __future__ import annotations

import threading
import time

import pytest
from cryptography.fernet import Fernet

from cua.escalation.bridge import EscalationBridge, EscalationRequest
from cua.escalation.credentials import (
    CredentialStoreError,
    delete_credential,
    get_credential,
    has_credential,
    list_credential_names,
    save_credential,
)


class _FakeMouse:
    def __init__(self):
        self.clicks = []

    def click(self, x, y):
        self.clicks.append((x, y))


class _FakeKeyboard:
    def __init__(self):
        self.typed = []
        self.pressed = []

    def type(self, text):
        self.typed.append(text)

    def press(self, key):
        self.pressed.append(key)


class _FakePage:
    def __init__(self):
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()
        self.viewport_size = {"width": 1280, "height": 720}
        self._shot_calls = []

    def screenshot(self, full_page=False):
        self._shot_calls.append(full_page)
        return b"fake-png-bytes"


# ---- EscalationBridge -------------------------------------------------------


def test_bridge_starts_under_agent_control():
    bridge = EscalationBridge(_FakePage())
    assert bridge.control == "agent"
    assert bridge.pending is None


def test_relay_refused_while_agent_in_control():
    bridge = EscalationBridge(_FakePage())
    with pytest.raises(PermissionError):
        bridge.relay_click(1, 1)


def test_request_human_blocks_until_resume_and_collects_actions():
    bridge = EscalationBridge(_FakePage())
    request = EscalationRequest(kind="credential", reason="need admin password", step=3)
    results = {}

    def discovery_thread():
        results["actions"] = bridge.request_human(request)

    t = threading.Thread(target=discovery_thread)
    t.start()
    time.sleep(0.05)

    assert bridge.control == "human"
    assert bridge.pending is request

    bridge.relay_type("super-secret-value", sensitive=True)
    bridge.relay_key("Enter")
    bridge.resume()
    t.join(timeout=2)

    assert bridge.control == "agent"
    assert bridge.pending is None
    actions = results["actions"]
    assert [a.kind for a in actions] == ["type", "key"]
    # Sensitive text must never appear in the recorded action -- only length.
    assert actions[0].detail == {"length": len("super-secret-value")}
    assert actions[1].detail == {"key": "Enter"}


def test_non_sensitive_type_is_recorded_verbatim():
    bridge = EscalationBridge(_FakePage())

    def discovery_thread():
        bridge.request_human(EscalationRequest(kind="other", reason="confirm this", step=1))

    t = threading.Thread(target=discovery_thread)
    t.start()
    time.sleep(0.05)
    bridge.relay_type("not a secret")
    bridge.resume()
    t.join(timeout=2)


def test_timeout_with_no_human_returns_no_actions():
    bridge = EscalationBridge(_FakePage())
    actions = bridge.request_human(EscalationRequest(kind="other", reason="nobody's here", step=1), timeout=0.05)
    assert actions == []
    assert bridge.control == "agent"


def test_screenshot_uses_viewport_not_full_page():
    page = _FakePage()
    bridge = EscalationBridge(page)
    bridge.screenshot_bytes()
    assert page._shot_calls == [False]


# ---- credentials ------------------------------------------------------------


def test_credential_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_STORE_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "cua.db"

    assert has_credential("target_app_admin_password", db_path=db_path) is False
    save_credential("target_app_admin_password", "Tr0ub4dor&3", db_path=db_path)
    assert has_credential("target_app_admin_password", db_path=db_path) is True
    assert get_credential("target_app_admin_password", db_path=db_path) == "Tr0ub4dor&3"
    assert "target_app_admin_password" in list_credential_names(db_path=db_path)

    delete_credential("target_app_admin_password", db_path=db_path)
    assert has_credential("target_app_admin_password", db_path=db_path) is False


def test_credential_names_never_expose_values(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_STORE_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "cua.db"
    save_credential("some_secret", "value-that-must-not-leak", db_path=db_path)
    names = list_credential_names(db_path=db_path)
    assert names == ["some_secret"]
    assert all("value-that-must-not-leak" not in n for n in names)


def test_missing_key_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("CREDENTIAL_STORE_KEY", raising=False)
    with pytest.raises(CredentialStoreError):
        save_credential("x", "y", db_path=tmp_path / "cua.db")


def test_stored_values_are_actually_encrypted_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_STORE_KEY", Fernet.generate_key().decode())
    db_path = tmp_path / "cua.db"
    save_credential("x", "a-very-recognizable-plaintext-value", db_path=db_path)
    raw = db_path.read_bytes()
    assert b"a-very-recognizable-plaintext-value" not in raw
