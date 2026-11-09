"""Shared paginated-list envelope for admin endpoints that need real offset pagination
(unlike most of today's admin surface — see admin_dashboard.py's module docstring on why its
own listing endpoints only support "most recent N", not this). Matches the frontend's
existing `Page<T>` TypeScript type (src/api/types.ts) field-for-field, since that shape was
already committed to against the mocked versions of these endpoints.
"""

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
