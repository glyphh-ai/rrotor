"""The Gateway mod's client, as programs will see it (docs/architecture.md, 9.2).

Gateway is designed, not built: until its part is installed, `ctx.gateway` raises `PartMissing`. This module is the
interface programs can be written against; it has no behaviour of its own."""

from __future__ import annotations

from typing import Any, Protocol


class Gateway(Protocol):
    """Third-party inference in the person's own rrotor, with their own keys. Never a Claude subscription."""

    def models(self) -> list[str]:
        """The models this program may use, as set in the Gateway mod's panel."""
        ...

    def chat(self, model: str, messages: list[dict[str, Any]], **options: Any) -> dict[str, Any]:
        """An OpenAI-compatible chat completion (`/v1/chat/completions`)."""
        ...

    def messages(self, model: str, messages: list[dict[str, Any]], max_tokens: int, **options: Any) -> dict[str, Any]:
        """An Anthropic-compatible message (`/v1/messages`), on the person's own API key."""
        ...

    def embeddings(self, model: str, input: list[str]) -> list[list[float]]:
        """OpenAI-compatible embeddings (`/v1/embeddings`)."""
        ...
