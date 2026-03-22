"""Abstract credential backend."""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Optional


class CredentialBackend(ABC):
    @abstractmethod
    def store(self, profile: str, key: str, value: str) -> None:
        """Store a credential value."""

    @abstractmethod
    def retrieve(self, profile: str, key: str) -> Optional[str]:
        """Retrieve a credential value, or None if not found."""

    @abstractmethod
    def delete(self, profile: str, key: str) -> None:
        """Delete a credential."""

    @abstractmethod
    def list_keys(self, profile: str) -> list[str]:
        """List all credential keys for a profile."""

    def test(self) -> None:
        """Verify backend is functional. Raises on failure."""
        self.store("__test__", "__test__", "ok")
        val = self.retrieve("__test__", "__test__")
        self.delete("__test__", "__test__")
        if val != "ok":
            raise RuntimeError("Credential backend round-trip failed")
