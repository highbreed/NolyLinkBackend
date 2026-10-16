"""Generic VSCU response envelope — verified against every decompiled *Res class in
com.kra.vsdc.execute.model (InitInfoRes, EchoTestRes, TrnsSalesSaveWrRes, ...): all of them
declare exactly {resultCd, resultMsg, resultDt, data}, matching the OSCU envelope shape
(app/schemas/kra/common.py) field-for-field. Field names are the literal KRA/VSCU camelCase,
per this project's existing convention (see common.py's own docstring) — not Python-
conventional snake_case, so JSON round-trips exactly without alias juggling.
"""

from typing import Generic, TypeVar

from pydantic import BaseModel

DataT = TypeVar("DataT")


class VSCURequestBase(BaseModel):
    """Every VSCU request carries at least tin + bhfId (confirmed on InitInfoReq, EchoTestReq,
    TrnsSalesSaveWrReq, ReportZCheckReq, ...); subclasses add their own fields.
    """

    tin: str
    bhfId: str


class VSCUResponseEnvelope(BaseModel, Generic[DataT]):
    """resultCd == "000" is the only unambiguous success. Unlike the OSCU envelope
    (app/schemas/kra/common.py), "001" is NOT a documented no-result-but-ok case here — no
    decompiled VSCU code path emits or expects it locally; treat anything other than "000" as
    requiring a look at app/integrations/vscu/constants.py's tables.
    """

    resultCd: str
    resultMsg: str | None = None
    resultDt: str | None = None
    data: DataT | None = None
