"""Authentication tests for LocalEGA Inbox uploads."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from impact_tools.ega.upload_inbox import (
    InboxUploadConfig,
    _keyboard_interactive_client,
    _open_sftp,
)


class _FakeSSHClient:
    def __init__(self) -> None:
        self.connect_kwargs: dict[str, object] = {}

    def set_missing_host_key_policy(self, _policy: object) -> None:
        return None

    def connect(self, **kwargs: object) -> None:
        self.connect_kwargs = kwargs


class InboxAuthenticationTests(unittest.TestCase):
    def test_password_auth_does_not_try_agent_or_default_keys(self) -> None:
        client = _FakeSSHClient()
        paramiko = ModuleType("paramiko")
        paramiko.SSHClient = lambda: client
        paramiko.AutoAddPolicy = lambda: object()
        paramiko.RejectPolicy = lambda: object()
        paramiko.ssh_exception = SimpleNamespace(
            AuthenticationException=RuntimeError,
        )
        config = InboxUploadConfig(
            input_dir=Path("/input"),
            output_dir=Path("/output"),
            host="inbox.example.test",
            username="user@example.test",
            password="secret",
        )

        with patch.dict(sys.modules, {"paramiko": paramiko}):
            managed = _open_sftp(config)

        self.assertIs(managed.client, client)
        self.assertFalse(client.connect_kwargs["look_for_keys"])
        self.assertFalse(client.connect_kwargs["allow_agent"])
        self.assertEqual(client.connect_kwargs["password"], "secret")

    def test_keyboard_interactive_uses_bracketed_host_key_for_custom_port(
        self,
    ) -> None:
        class FakeServerKey:
            def get_name(self) -> str:
                return "ssh-ed25519"

        server_key = FakeServerKey()
        expected_key = server_key

        class FakeHostKeys:
            def __init__(self) -> None:
                self.lookups: list[str] = []

            def lookup(self, hostname: str):
                self.lookups.append(hostname)
                if hostname == "[inbox.example.test]:8086":
                    return {"ssh-ed25519": expected_key}
                return None

        host_keys = FakeHostKeys()

        class FakeSSHClient:
            _system_host_keys = host_keys

            def load_system_host_keys(self) -> None:
                return None

            def set_missing_host_key_policy(self, _policy: object) -> None:
                return None

            def get_host_keys(self):
                return SimpleNamespace(lookup=lambda _hostname: None)

        class FakeTransport:
            def __init__(self, _sock: object) -> None:
                self.authenticated = False

            def start_client(self, timeout: int) -> None:
                return None

            def get_remote_server_key(self):
                return server_key

            def auth_interactive(self, username: str, handler) -> None:
                handler("", "", [("Password: ", False)])
                self.authenticated = True

            def is_authenticated(self) -> bool:
                return self.authenticated

        paramiko = SimpleNamespace(
            SSHClient=FakeSSHClient,
            RejectPolicy=lambda: object(),
            Transport=FakeTransport,
            ssh_exception=SimpleNamespace(
                SSHException=RuntimeError,
                AuthenticationException=RuntimeError,
            ),
        )
        config = InboxUploadConfig(
            input_dir=Path("/input"),
            output_dir=Path("/output"),
            host="inbox.example.test",
            port=8086,
            username="user@example.test",
            host_key_policy="reject",
        )

        with patch(
            "impact_tools.ega.upload_inbox.socket.create_connection",
            return_value=object(),
        ):
            _keyboard_interactive_client(paramiko, config, "secret")

        self.assertEqual(host_keys.lookups, ["[inbox.example.test]:8086"])

    def test_keyboard_interactive_closes_transport_on_host_key_failure(self) -> None:
        class FakeServerKey:
            def get_name(self) -> str:
                return "ssh-ed25519"

        class FakeSSHClient:
            _system_host_keys = SimpleNamespace(lookup=lambda _hostname: None)

            def load_system_host_keys(self) -> None:
                return None

            def set_missing_host_key_policy(self, _policy: object) -> None:
                return None

            def get_host_keys(self):
                return SimpleNamespace(lookup=lambda _hostname: None)

        class FakeTransport:
            instance = None

            def __init__(self, _sock: object) -> None:
                self.closed = False
                FakeTransport.instance = self

            def start_client(self, timeout: int) -> None:
                return None

            def get_remote_server_key(self):
                return FakeServerKey()

            def close(self) -> None:
                self.closed = True

        paramiko = SimpleNamespace(
            SSHClient=FakeSSHClient,
            RejectPolicy=lambda: object(),
            Transport=FakeTransport,
            ssh_exception=SimpleNamespace(
                SSHException=RuntimeError,
                AuthenticationException=RuntimeError,
            ),
        )
        config = InboxUploadConfig(
            input_dir=Path("/input"),
            output_dir=Path("/output"),
            host="inbox.example.test",
            port=8086,
            username="user@example.test",
            host_key_policy="reject",
        )

        with patch(
            "impact_tools.ega.upload_inbox.socket.create_connection",
            return_value=object(),
        ), self.assertRaisesRegex(RuntimeError, "is not known"):
            _keyboard_interactive_client(paramiko, config, "secret")

        self.assertTrue(FakeTransport.instance.closed)


if __name__ == "__main__":
    unittest.main()
