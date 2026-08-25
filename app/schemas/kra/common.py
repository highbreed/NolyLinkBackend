"""Shared request/response envelope pieces reused across every KRA endpoint (spec §5.1, §5.2).

Field names on every model in app/schemas/kra/ are the exact KRA camelCase names, not
Python-conventional snake_case — this is deliberate: the task's own field-name/length
contracts are the KRA spec's, and matching them 1:1 keeps JSON round-tripping exact without
alias juggling. Only client-facing DTOs (app/schemas/client/) use snake_case.
"""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

DataT = TypeVar("DataT")


class KraAuthenticatedRequest(BaseModel):
    """Fields required on every KRA request except /selectInitOsdcInfo (spec §5.1)."""

    tin: str = Field(max_length=11)
    bhfId: str = Field(max_length=2)
    cmcKey: str = Field(max_length=255)


class KraResponseEnvelope(BaseModel, Generic[DataT]):
    """Common response envelope (spec §5.2). resultCd == "000" is the only success code."""

    resultCd: str
    resultMsg: str
    resultDt: str
    data: DataT | None = None
