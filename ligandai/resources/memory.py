# Copyright © 2026 Ligandal, Inc. All rights reserved.
"""Episodic memory and recent activity."""

from __future__ import annotations

from typing import List, Literal

from ligandai.resources._base import AsyncResource, Resource
from ligandai.types import MemoryItem, RecentActivity

_MemoryType = Literal["result", "decision", "conversation", "discovery", "all"]


class Memory(Resource):
    """``/api/episodic-memory/*`` and ``/api/recent-activity``."""

    def search(
        self,
        query: str,
        memory_type: _MemoryType = "all",
        limit: int = 10,
    ) -> List[MemoryItem]:
        """Semantic search across the user's memory."""
        body: dict[str, object] = {"query": query, "limit": limit}
        if memory_type != "all":
            body["memoryType"] = memory_type
        payload = self._transport.request("POST", "/api/episodic-memory/search", json=body) or []
        items = payload if isinstance(payload, list) else payload.get("results", [])
        return [MemoryItem.model_validate(it) for it in items]

    def list(self, limit: int = 20, offset: int = 0) -> List[MemoryItem]:
        """List memories paged."""
        payload = self._transport.request(
            "GET", "/api/episodic-memory/list", params={"limit": limit, "offset": offset}
        ) or []
        items = payload if isinstance(payload, list) else payload.get("items", [])
        return [MemoryItem.model_validate(it) for it in items]

    def save(
        self,
        content: str,
        memory_type: str,
        title: str | None = None,
        tags: List[str] | None = None,
    ) -> MemoryItem:
        body: dict[str, object] = {
            "content": content,
            "memoryType": memory_type,
        }
        if title is not None:
            body["title"] = title
        if tags is not None:
            body["tags"] = tags
        return MemoryItem.model_validate(
            self._transport.request("POST", "/api/episodic-memory/save", json=body) or {}
        )

    def delete(self, memory_id: str | int) -> bool:
        try:
            self._transport.request("DELETE", f"/api/episodic-memory/{memory_id}")
            return True
        except Exception:
            return False

    def recent_activity(self, limit: int = 10) -> RecentActivity:
        return RecentActivity.model_validate(
            self._transport.request("GET", "/api/recent-activity", params={"limit": limit}) or {}
        )


class AsyncMemory(AsyncResource):
    async def search(
        self,
        query: str,
        memory_type: _MemoryType = "all",
        limit: int = 10,
    ) -> List[MemoryItem]:
        body: dict[str, object] = {"query": query, "limit": limit}
        if memory_type != "all":
            body["memoryType"] = memory_type
        payload = await self._transport.request("POST", "/api/episodic-memory/search", json=body) or []
        items = payload if isinstance(payload, list) else payload.get("results", [])
        return [MemoryItem.model_validate(it) for it in items]

    async def list(self, limit: int = 20, offset: int = 0) -> List[MemoryItem]:
        payload = await self._transport.request(
            "GET", "/api/episodic-memory/list", params={"limit": limit, "offset": offset}
        ) or []
        items = payload if isinstance(payload, list) else payload.get("items", [])
        return [MemoryItem.model_validate(it) for it in items]

    async def save(
        self,
        content: str,
        memory_type: str,
        title: str | None = None,
        tags: List[str] | None = None,
    ) -> MemoryItem:
        body: dict[str, object] = {"content": content, "memoryType": memory_type}
        if title is not None:
            body["title"] = title
        if tags is not None:
            body["tags"] = tags
        return MemoryItem.model_validate(
            await self._transport.request("POST", "/api/episodic-memory/save", json=body) or {}
        )

    async def delete(self, memory_id: str | int) -> bool:
        try:
            await self._transport.request("DELETE", f"/api/episodic-memory/{memory_id}")
            return True
        except Exception:
            return False

    async def recent_activity(self, limit: int = 10) -> RecentActivity:
        return RecentActivity.model_validate(
            await self._transport.request("GET", "/api/recent-activity", params={"limit": limit}) or {}
        )
