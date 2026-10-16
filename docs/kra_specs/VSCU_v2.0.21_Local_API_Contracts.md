# VSCU v2.0.21 — Local HTTP API Contract Reference

> **Source:** Decompiled class signatures and Spring route annotations from the user-supplied `vscu-v2.0.21.jar` (`BOOT-INF/classes/com/kra/vsdc/excute`). Generated 2026-10-14.
> **Scope:** Local VSCU interface, **not** the upstream KRA API and **not** the Nolytech Fiscal Gateway API.
> **Evidence levels:** Routes, verbs, Java DTO types, and declared DTO properties are **JAR-verified**. Field requiredness, accepted code values, business outcomes, and runtime behavior require deeper bytecode analysis and/or sandbox testing unless separately noted.

## Quick start

- Base URL: `http://127.0.0.1:8088` (from this deployment; configurable).
- Most endpoints: `POST`, `Content-Type: application/json`.
- VSCU reads local device material (`EbmData/<TIN>_<BRANCH>/Ebm/device/cmcKey`) when constructing the upstream client. **Do not send or expose this key in ERP requests.**
- TIN and branch must match initialized device state.
- A local HTTP 200 or `resultCd: "000"` is not by itself proof of completed KRA persistence; some controllers include resend logic. Reconcile where appropriate.

## Endpoint index

| Method | Local endpoint | Purpose | Request DTO | Response DTO |
|---|---|---|---|---|
| `GET` | `/main/selectServerTime` | Read VSCU server time | `—` | `String` |
| `POST` | `/initializer/selectInitInfo` | Initialize device / fetch initialization info | `InitInfoReq` | `InitInfoRes` |
| `POST` | `/initializer/selectSequences` | Read device sequence state | `InitInfoReq` | `InitInfoRes` |
| `POST` | `/initializer/updateSequences` | Update device sequence state | `InitInfoReq` | `InitInfoRes` |
| `REQUEST` | `/initializer/initializer` | Inspect controller behavior | `InitInfoReq` | `InitInfoRes` |
| `POST` | `/test/echoTest` | Test connectivity/echo | `TestEchoReq` | `TestEchoRes` |
| `POST` | `/code/selectCodes` | Fetch KRA general reference codes | `CodeReq` | `CodeRes` |
| `POST` | `/itemClass/selectItemsClass` | Fetch KRA item classification hierarchy | `ItemClsReq` | `ItemClsRes` |
| `POST` | `/customers/selectCustomer` | Fetch customer records | `CustReq` | `CustRes` |
| `POST` | `/items/selectItems` | Fetch item records | `ItemReq` | `ItemRes` |
| `POST` | `/items/saveItems` | Submit item registration/update | `ItemSaveReq` | `ItemSaveRes` |
| `POST` | `/items/saveItemComposition` | Submit item composition | `ItemCompositionSaveReq` | `ItemCompositionSaveRes` |
| `POST` | `/branches/selectBranches` | Fetch branch data | `BhfReq` | `BhfRes` |
| `POST` | `/branches/saveBrancheUsers` | Submit branch user data | `BhfUserSaveReq` | `BhfUserSaveRes` |
| `POST` | `/branches/saveBrancheInsurances` | Submit branch insurance data | `BhfInsuranceSaveReq` | `BhfInsuranceSaveRes` |
| `POST` | `/branches/saveBrancheCustomers` | Submit branch customer data | `BhfCustSaveReq` | `BhfCustSaveRes` |
| `POST` | `/trnsSales/saveSales` | Submit sales/credit-note transactions | `TrnsSalesSaveWrReq` | `TrnsSalesSaveWrRes` |
| `POST` | `/trnsSales/selectInvoiceDetails` | Retrieve invoice details | `InvoiceDetailsReq` | `InvoiceDetailsRes` |
| `POST` | `/trnsSales/selectTrnsSalesList` | Retrieve sales transaction list | `TrnsPurchaseSalesReq` | `InvoicesRes` |
| `POST` | `/trnsPurchase/savePurchases` | Submit purchase transactions | `TrnsPurchaseSaveReq` | `TrnsPurchaseSaveRes` |
| `POST` | `/trnsPurchase/selectTrnsPurchaseSales` | Retrieve supplier-declared purchase/sales records | `TrnsPurchaseSalesReq` | `TrnsPurchaseSalesRes` |
| `POST` | `/imports/selectImportItems` | Fetch imported item declarations | `ImportItemReq` | `ImportItemRes` |
| `POST` | `/imports/updateImportItems` | Update imported item declarations | `ImportItemUpdateReq` | `ImportItemUpdateRes` |
| `POST` | `/stock/selectStockItems` | Retrieve stock movement records | `StockMoveReq` | `StockMoveRes` |
| `POST` | `/stock/saveStockItems` | Submit stock movement | `StockIoSaveReq` | `StockIoSaveRes` |
| `POST` | `/stockMaster/saveStockMaster` | Submit stock master/balance | `StockMasterSaveReq` | `StockMasterSaveRes` |
| `POST` | `/notices/selectNotices` | Retrieve notices | `NoticeReq` | `NoticeRes` |
| `POST` | `/reports/saveZReports` | Submit Z report | `ReportZSaveUpdateReq` | `ReportZSaveRes` |
| `POST` | `/reports/checkZReport` | Check Z report status | `ReportZCheckReq` | `ReportZCheckRes` |

> **Caution:** `/initializer/initializer` appears as a `RequestMapping` on an additional controller method. Its HTTP verb and purpose are not established by the simple route scan; do not use without inspecting its annotations and behavior.

## Request DTO field inventory

The fields below are extracted from compiled Java request classes, **not inferred from the public KRA PDF**. Nested types and list element schemas may need further inspection. A declared field is not automatically required.

### `POST /initializer/selectInitInfo`
- **Purpose:** Initialize device / fetch initialization info
- **Controller:** `InitializeVsdcExcute`
- **Request:** `com.kra.vsdc.execute.model.InitInfoReq`
- **Response:** `com.kra.vsdc.execute.model.InitInfoRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `dvcSrlNo` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "dvcSrlNo": null
}
```

### `POST /initializer/selectSequences`
- **Purpose:** Read device sequence state
- **Controller:** `InitializeVsdcExcute`
- **Request:** `com.kra.vsdc.execute.model.InitInfoReq`
- **Response:** `com.kra.vsdc.execute.model.InitInfoRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `dvcSrlNo` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "dvcSrlNo": null
}
```

### `POST /initializer/updateSequences`
- **Purpose:** Update device sequence state
- **Controller:** `InitializeVsdcExcute`
- **Request:** `com.kra.vsdc.execute.model.InitInfoReq`
- **Response:** `com.kra.vsdc.execute.model.InitInfoRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `dvcSrlNo` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "dvcSrlNo": null
}
```

### `REQUEST /initializer/initializer`
- **Purpose:** Requires behavioral review
- **Controller:** `InitializeVsdcExcute`
- **Request:** `com.kra.vsdc.execute.model.InitInfoReq`
- **Response:** `com.kra.vsdc.execute.model.InitInfoRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `dvcSrlNo` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "dvcSrlNo": null
}
```

### `POST /test/echoTest`
- **Purpose:** Test connectivity/echo
- **Controller:** `TestExcute`
- **Request:** `com.kra.vsdc.execute.model.TestEchoReq`
- **Response:** `com.kra.vsdc.execute.model.TestEchoRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `testStr` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "testStr": null
}
```

### `POST /code/selectCodes`
- **Purpose:** Fetch KRA general reference codes
- **Controller:** `CodeExcute`
- **Request:** `com.kra.vsdc.execute.model.CodeReq`
- **Response:** `com.kra.vsdc.execute.model.CodeRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `lastReqDt` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "lastReqDt": null
}
```

### `POST /itemClass/selectItemsClass`
- **Purpose:** Fetch KRA item classification hierarchy
- **Controller:** `ItemClsExcute`
- **Request:** `com.kra.vsdc.execute.model.ItemClsReq`
- **Response:** `com.kra.vsdc.execute.model.ItemClsRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `lastReqDt` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "lastReqDt": null
}
```

### `POST /customers/selectCustomer`
- **Purpose:** Fetch customer records
- **Controller:** `CustomerExcute`
- **Request:** `com.kra.vsdc.execute.model.CustReq`
- **Response:** `com.kra.vsdc.execute.model.CustRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `custmTin` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "custmTin": null
}
```

### `POST /items/selectItems`
- **Purpose:** Fetch item records
- **Controller:** `ItemExcute`
- **Request:** `com.kra.vsdc.execute.model.ItemReq`
- **Response:** `com.kra.vsdc.execute.model.ItemRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `lastReqDt` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "lastReqDt": null
}
```

### `POST /items/saveItems`
- **Purpose:** Submit item registration/update
- **Controller:** `ItemExcute`
- **Request:** `com.kra.vsdc.execute.model.ItemSaveReq`
- **Response:** `com.kra.vsdc.execute.model.ItemSaveRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `itemCd` | `String` |
| `itemClsCd` | `String` |
| `itemTyCd` | `String` |
| `itemNm` | `String` |
| `itemStdNm` | `String` |
| `orgnNatCd` | `String` |
| `pkgUnitCd` | `String` |
| `qtyUnitCd` | `String` |
| `taxTyCd` | `String` |
| `btchNo` | `String` |
| `bcd` | `String` |
| `dftPrc` | `java.math.BigDecimal` |
| `grpPrcL1` | `java.math.BigDecimal` |
| `grpPrcL2` | `java.math.BigDecimal` |
| `grpPrcL3` | `java.math.BigDecimal` |
| `grpPrcL4` | `java.math.BigDecimal` |
| `grpPrcL5` | `java.math.BigDecimal` |
| `addInfo` | `String` |
| `sftyQty` | `java.math.BigDecimal` |
| `isrcAplcbYn` | `String` |
| `useYn` | `String` |
| `regrId` | `String` |
| `regrNm` | `String` |
| `modrId` | `String` |
| `modrNm` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "itemCd": null,
  "itemClsCd": null,
  "itemTyCd": null,
  "itemNm": null,
  "itemStdNm": null,
  "orgnNatCd": null,
  "pkgUnitCd": null,
  "qtyUnitCd": null,
  "taxTyCd": null,
  "btchNo": null,
  "bcd": null,
  "dftPrc": null,
  "grpPrcL1": null,
  "grpPrcL2": null,
  "grpPrcL3": null,
  "grpPrcL4": null,
  "grpPrcL5": null,
  "addInfo": null,
  "sftyQty": null,
  "isrcAplcbYn": null,
  "useYn": null,
  "regrId": null,
  "regrNm": null,
  "modrId": null,
  "modrNm": null
}
```

### `POST /items/saveItemComposition`
- **Purpose:** Submit item composition
- **Controller:** `ItemExcute`
- **Request:** `com.kra.vsdc.execute.model.ItemCompositionSaveReq`
- **Response:** `com.kra.vsdc.execute.model.ItemCompositionSaveRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `itemCd` | `String` |
| `cpstItemCd` | `String` |
| `cpstQty` | `java.math.BigDecimal` |
| `regrId` | `String` |
| `regrNm` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "itemCd": null,
  "cpstItemCd": null,
  "cpstQty": null,
  "regrId": null,
  "regrNm": null
}
```

### `POST /branches/selectBranches`
- **Purpose:** Fetch branch data
- **Controller:** `BhfExcute`
- **Request:** `com.kra.vsdc.execute.model.BhfReq`
- **Response:** `com.kra.vsdc.execute.model.BhfRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `lastReqDt` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "lastReqDt": null
}
```

### `POST /branches/saveBrancheUsers`
- **Purpose:** Submit branch user data
- **Controller:** `BhfExcute`
- **Request:** `com.kra.vsdc.execute.model.BhfUserSaveReq`
- **Response:** `com.kra.vsdc.execute.model.BhfUserSaveRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `userId` | `String` |
| `userNm` | `String` |
| `pwd` | `String` |
| `adrs` | `String` |
| `cntc` | `String` |
| `authCd` | `String` |
| `remark` | `String` |
| `useYn` | `String` |
| `regrId` | `String` |
| `regrNm` | `String` |
| `modrId` | `String` |
| `modrNm` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "userId": null,
  "userNm": null,
  "pwd": null,
  "adrs": null,
  "cntc": null,
  "authCd": null,
  "remark": null,
  "useYn": null,
  "regrId": null,
  "regrNm": null,
  "modrId": null,
  "modrNm": null
}
```

### `POST /branches/saveBrancheInsurances`
- **Purpose:** Submit branch insurance data
- **Controller:** `BhfExcute`
- **Request:** `com.kra.vsdc.execute.model.BhfInsuranceSaveReq`
- **Response:** `com.kra.vsdc.execute.model.BhfInsuranceSaveRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `isrccCd` | `String` |
| `isrccNm` | `String` |
| `isrcRt` | `java.math.BigDecimal` |
| `useYn` | `String` |
| `regrId` | `String` |
| `regrNm` | `String` |
| `modrId` | `String` |
| `modrNm` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "isrccCd": null,
  "isrccNm": null,
  "isrcRt": null,
  "useYn": null,
  "regrId": null,
  "regrNm": null,
  "modrId": null,
  "modrNm": null
}
```

### `POST /branches/saveBrancheCustomers`
- **Purpose:** Submit branch customer data
- **Controller:** `BhfExcute`
- **Request:** `com.kra.vsdc.execute.model.BhfCustSaveReq`
- **Response:** `com.kra.vsdc.execute.model.BhfCustSaveRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `custNo` | `String` |
| `custTin` | `String` |
| `custNm` | `String` |
| `adrs` | `String` |
| `telNo` | `String` |
| `email` | `String` |
| `faxNo` | `String` |
| `useYn` | `String` |
| `remark` | `String` |
| `regrId` | `String` |
| `regrNm` | `String` |
| `modrId` | `String` |
| `modrNm` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "custNo": null,
  "custTin": null,
  "custNm": null,
  "adrs": null,
  "telNo": null,
  "email": null,
  "faxNo": null,
  "useYn": null,
  "remark": null,
  "regrId": null,
  "regrNm": null,
  "modrId": null,
  "modrNm": null
}
```

### `POST /trnsSales/saveSales`
- **Purpose:** Submit sales/credit-note transactions
- **Controller:** `TrnsSalesExcute`
- **Request:** `com.kra.vsdc.execute.model.TrnsSalesSaveWrReq`
- **Response:** `com.kra.vsdc.execute.model.TrnsSalesSaveWrRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `invcNo` | `Long` |
| `orgInvcNo` | `Long` |
| `custTin` | `String` |
| `custNm` | `String` |
| `salesTyCd` | `String` |
| `rcptTyCd` | `String` |
| `pmtTyCd` | `String` |
| `salesSttsCd` | `String` |
| `cfmDt` | `String` |
| `salesDt` | `String` |
| `stockRlsDt` | `String` |
| `cnclReqDt` | `String` |
| `cnclDt` | `String` |
| `rfdDt` | `String` |
| `rfdRsnCd` | `String` |
| `totItemCnt` | `Integer` |
| `taxblAmtA` | `java.math.BigDecimal` |
| `taxblAmtB` | `java.math.BigDecimal` |
| `taxblAmtC` | `java.math.BigDecimal` |
| `taxblAmtD` | `java.math.BigDecimal` |
| `taxblAmtE` | `java.math.BigDecimal` |
| `taxRtA` | `java.math.BigDecimal` |
| `taxRtB` | `java.math.BigDecimal` |
| `taxRtC` | `java.math.BigDecimal` |
| `taxRtD` | `java.math.BigDecimal` |
| `taxRtE` | `java.math.BigDecimal` |
| `taxAmtA` | `java.math.BigDecimal` |
| `taxAmtB` | `java.math.BigDecimal` |
| `taxAmtC` | `java.math.BigDecimal` |
| `taxAmtD` | `java.math.BigDecimal` |
| `taxAmtE` | `java.math.BigDecimal` |
| `totTaxblAmt` | `java.math.BigDecimal` |
| `totTaxAmt` | `java.math.BigDecimal` |
| `totAmt` | `java.math.BigDecimal` |
| `prchrAcptcYn` | `String` |
| `remark` | `String` |
| `regrId` | `String` |
| `regrNm` | `String` |
| `modrId` | `String` |
| `modrNm` | `String` |
| `receipt` | `com.kra.vsdc.execute.model.TrnsSalesSaveWrReceipt` |
| `itemList` | `List<com.kra.vsdc.execute.model.TrnsSalesSaveWrItem>` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "invcNo": null,
  "orgInvcNo": null,
  "custTin": null,
  "custNm": null,
  "salesTyCd": null,
  "rcptTyCd": null,
  "pmtTyCd": null,
  "salesSttsCd": null,
  "cfmDt": null,
  "salesDt": null,
  "stockRlsDt": null,
  "cnclReqDt": null,
  "cnclDt": null,
  "rfdDt": null,
  "rfdRsnCd": null,
  "totItemCnt": null,
  "taxblAmtA": null,
  "taxblAmtB": null,
  "taxblAmtC": null,
  "taxblAmtD": null,
  "taxblAmtE": null,
  "taxRtA": null,
  "taxRtB": null,
  "taxRtC": null,
  "taxRtD": null,
  "taxRtE": null,
  "taxAmtA": null,
  "taxAmtB": null,
  "taxAmtC": null,
  "taxAmtD": null,
  "taxAmtE": null,
  "totTaxblAmt": null,
  "totTaxAmt": null,
  "totAmt": null,
  "prchrAcptcYn": null,
  "remark": null,
  "regrId": null,
  "regrNm": null,
  "modrId": null,
  "modrNm": null,
  "receipt": null,
  "itemList": null
}
```

### `POST /trnsSales/selectInvoiceDetails`
- **Purpose:** Retrieve invoice details
- **Controller:** `TrnsSalesExcute`
- **Request:** `com.kra.vsdc.execute.model.InvoiceDetailsReq`
- **Response:** `com.kra.vsdc.execute.model.InvoiceDetailsRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `invcNo` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "invcNo": null
}
```

### `POST /trnsSales/selectTrnsSalesList`
- **Purpose:** Retrieve sales transaction list
- **Controller:** `TrnsSalesExcute`
- **Request:** `com.kra.vsdc.execute.model.TrnsPurchaseSalesReq`
- **Response:** `com.kra.vsdc.execute.model.InvoicesRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `lastReqDt` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "lastReqDt": null
}
```

### `POST /trnsPurchase/savePurchases`
- **Purpose:** Submit purchase transactions
- **Controller:** `TrnsPurchaseExcute`
- **Request:** `com.kra.vsdc.execute.model.TrnsPurchaseSaveReq`
- **Response:** `com.kra.vsdc.execute.model.TrnsPurchaseSaveRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `invcNo` | `Long` |
| `orgInvcNo` | `Long` |
| `spplrTin` | `String` |
| `spplrBhfId` | `String` |
| `spplrNm` | `String` |
| `spplrInvcNo` | `Long` |
| `regTyCd` | `String` |
| `pchsTyCd` | `String` |
| `rcptTyCd` | `String` |
| `pmtTyCd` | `String` |
| `pchsSttsCd` | `String` |
| `cfmDt` | `String` |
| `pchsDt` | `String` |
| `wrhsDt` | `String` |
| `cnclReqDt` | `String` |
| `cnclDt` | `String` |
| `rfdDt` | `String` |
| `totItemCnt` | `Integer` |
| `taxblAmtA` | `java.math.BigDecimal` |
| `taxblAmtB` | `java.math.BigDecimal` |
| `taxblAmtC` | `java.math.BigDecimal` |
| `taxblAmtD` | `java.math.BigDecimal` |
| `taxblAmtE` | `java.math.BigDecimal` |
| `taxRtA` | `java.math.BigDecimal` |
| `taxRtB` | `java.math.BigDecimal` |
| `taxRtC` | `java.math.BigDecimal` |
| `taxRtD` | `java.math.BigDecimal` |
| `taxRtE` | `java.math.BigDecimal` |
| `taxAmtA` | `java.math.BigDecimal` |
| `taxAmtB` | `java.math.BigDecimal` |
| `taxAmtC` | `java.math.BigDecimal` |
| `taxAmtD` | `java.math.BigDecimal` |
| `taxAmtE` | `java.math.BigDecimal` |
| `totTaxblAmt` | `java.math.BigDecimal` |
| `totTaxAmt` | `java.math.BigDecimal` |
| `totAmt` | `java.math.BigDecimal` |
| `remark` | `String` |
| `regrId` | `String` |
| `regrNm` | `String` |
| `modrId` | `String` |
| `modrNm` | `String` |
| `itemList` | `List<com.kra.vsdc.execute.model.TrnsPurchaseSaveItem>` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "invcNo": null,
  "orgInvcNo": null,
  "spplrTin": null,
  "spplrBhfId": null,
  "spplrNm": null,
  "spplrInvcNo": null,
  "regTyCd": null,
  "pchsTyCd": null,
  "rcptTyCd": null,
  "pmtTyCd": null,
  "pchsSttsCd": null,
  "cfmDt": null,
  "pchsDt": null,
  "wrhsDt": null,
  "cnclReqDt": null,
  "cnclDt": null,
  "rfdDt": null,
  "totItemCnt": null,
  "taxblAmtA": null,
  "taxblAmtB": null,
  "taxblAmtC": null,
  "taxblAmtD": null,
  "taxblAmtE": null,
  "taxRtA": null,
  "taxRtB": null,
  "taxRtC": null,
  "taxRtD": null,
  "taxRtE": null,
  "taxAmtA": null,
  "taxAmtB": null,
  "taxAmtC": null,
  "taxAmtD": null,
  "taxAmtE": null,
  "totTaxblAmt": null,
  "totTaxAmt": null,
  "totAmt": null,
  "remark": null,
  "regrId": null,
  "regrNm": null,
  "modrId": null,
  "modrNm": null,
  "itemList": null
}
```

### `POST /trnsPurchase/selectTrnsPurchaseSales`
- **Purpose:** Retrieve supplier-declared purchase/sales records
- **Controller:** `TrnsPurchaseExcute`
- **Request:** `com.kra.vsdc.execute.model.TrnsPurchaseSalesReq`
- **Response:** `com.kra.vsdc.execute.model.TrnsPurchaseSalesRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `lastReqDt` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "lastReqDt": null
}
```

### `POST /imports/selectImportItems`
- **Purpose:** Fetch imported item declarations
- **Controller:** `ImportItemExcute`
- **Request:** `com.kra.vsdc.execute.model.ImportItemReq`
- **Response:** `com.kra.vsdc.execute.model.ImportItemRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `lastReqDt` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "lastReqDt": null
}
```

### `POST /imports/updateImportItems`
- **Purpose:** Update imported item declarations
- **Controller:** `ImportItemExcute`
- **Request:** `com.kra.vsdc.execute.model.ImportItemUpdateReq`
- **Response:** `com.kra.vsdc.execute.model.ImportItemUpdateRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `taskCd` | `String` |
| `dclDe` | `String` |
| `itemSeq` | `Integer` |
| `hsCd` | `String` |
| `itemClsCd` | `String` |
| `itemCd` | `String` |
| `imptItemSttsCd` | `String` |
| `remark` | `String` |
| `modrId` | `String` |
| `modrNm` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "taskCd": null,
  "dclDe": null,
  "itemSeq": null,
  "hsCd": null,
  "itemClsCd": null,
  "itemCd": null,
  "imptItemSttsCd": null,
  "remark": null,
  "modrId": null,
  "modrNm": null
}
```

### `POST /stock/selectStockItems`
- **Purpose:** Retrieve stock movement records
- **Controller:** `StockIoExcute`
- **Request:** `com.kra.vsdc.execute.model.StockMoveReq`
- **Response:** `com.kra.vsdc.execute.model.StockMoveRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `lastReqDt` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "lastReqDt": null
}
```

### `POST /stock/saveStockItems`
- **Purpose:** Submit stock movement
- **Controller:** `StockIoExcute`
- **Request:** `com.kra.vsdc.execute.model.StockIoSaveReq`
- **Response:** `com.kra.vsdc.execute.model.StockIoSaveRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `sarNo` | `Integer` |
| `orgSarNo` | `Integer` |
| `regTyCd` | `String` |
| `custTin` | `String` |
| `custNm` | `String` |
| `custBhfId` | `String` |
| `sarTyCd` | `String` |
| `ocrnDt` | `String` |
| `totItemCnt` | `int` |
| `totTaxblAmt` | `java.math.BigDecimal` |
| `totTaxAmt` | `java.math.BigDecimal` |
| `totAmt` | `java.math.BigDecimal` |
| `remark` | `String` |
| `regrId` | `String` |
| `regrNm` | `String` |
| `modrId` | `String` |
| `modrNm` | `String` |
| `itemList` | `List<com.kra.vsdc.execute.model.StockIoItemSaveReq>` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "sarNo": null,
  "orgSarNo": null,
  "regTyCd": null,
  "custTin": null,
  "custNm": null,
  "custBhfId": null,
  "sarTyCd": null,
  "ocrnDt": null,
  "totItemCnt": null,
  "totTaxblAmt": null,
  "totTaxAmt": null,
  "totAmt": null,
  "remark": null,
  "regrId": null,
  "regrNm": null,
  "modrId": null,
  "modrNm": null,
  "itemList": null
}
```

### `POST /stockMaster/saveStockMaster`
- **Purpose:** Submit stock master/balance
- **Controller:** `StockMasterExcute`
- **Request:** `com.kra.vsdc.execute.model.StockMasterSaveReq`
- **Response:** `com.kra.vsdc.execute.model.StockMasterSaveRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `itemCd` | `String` |
| `rsdQty` | `java.math.BigDecimal` |
| `regrId` | `String` |
| `regrNm` | `String` |
| `modrId` | `String` |
| `modrNm` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "itemCd": null,
  "rsdQty": null,
  "regrId": null,
  "regrNm": null,
  "modrId": null,
  "modrNm": null
}
```

### `POST /notices/selectNotices`
- **Purpose:** Retrieve notices
- **Controller:** `NoticeExcute`
- **Request:** `com.kra.vsdc.execute.model.NoticeReq`
- **Response:** `com.kra.vsdc.execute.model.NoticeRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `lastReqDt` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "lastReqDt": null
}
```

### `POST /reports/saveZReports`
- **Purpose:** Submit Z report
- **Controller:** `ReportExcute`
- **Request:** `com.kra.vsdc.execute.model.ReportZSaveUpdateReq`
- **Response:** `com.kra.vsdc.execute.model.ReportZSaveRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `rptDe` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "rptDe": null
}
```

### `POST /reports/checkZReport`
- **Purpose:** Check Z report status
- **Controller:** `ReportExcute`
- **Request:** `com.kra.vsdc.execute.model.ReportZCheckReq`
- **Response:** `com.kra.vsdc.execute.model.ReportZCheckRes`

| Field | Java type |
|---|---|
| `tin` | `String` |
| `bhfId` | `String` |
| `rptDe` | `String` |

**Minimal shape (field names only; not a runnable request):**
```json
{
  "tin": null,
  "bhfId": null,
  "rptDe": null
}
```

## Response conventions and error handling

Many endpoints return a `resultCd`, `resultMsg`, `resultDt`, and `data` envelope, but **do not assume every DTO has the same payload**. See the compiled response DTO for each route.

### Known local error case (observed in runtime logs)

`POST /code/selectCodes` returned `899` / `An error regarding Client occurred.` because `DeviceManager.getKey()` raised `NoSuchFileException` for `/home/dg/AppData/EbmData/P052058389S_01/Ebm/device/cmcKey`. This is a **local device-state/client-header error**, not proof of an upstream KRA rejection.

Initialization returned `902` / `This device is installed` for the already registered device. This does not restore a missing local key. Preserve/migrate original initialized `EbmData` securely.

### Item registration resend caveat

Previous analysis of the `ItemExcute.saveItem()` bytecode identified handling for result codes `896` and `894` that can persist a resend file and return a success-shaped result. Treat this as a **previous bytecode-analysis finding requiring a regression test**; do not equate local success with confirmed remote registration.

## Example calls

### General reference codes

```bash
curl -sS -X POST 'http://127.0.0.1:8088/code/selectCodes' \
  -H 'Content-Type: application/json' \
  -d '{"tin":"P052058389S","bhfId":"01","lastReqDt":"20200101000000"}'
```

### Item classifications

```bash
curl -sS -X POST 'http://127.0.0.1:8088/itemClass/selectItemsClass' \
  -H 'Content-Type: application/json' \
  -d '{"tin":"P052058389S","bhfId":"01","lastReqDt":"20200101000000"}'
```

### Item registration

`POST /items/saveItems` uses `ItemSaveReq`. See its **full field inventory above**. Supply KRA-valid `itemCd`, `itemClsCd`, `itemTyCd`, `pkgUnitCd`, `qtyUnitCd`, `taxTyCd`, etc. Do not submit the null-field skeleton verbatim.

## How to verify a contract after a JAR upgrade

```bash
jar tf vscu-v2.0.21.jar | grep "BOOT-INF/classes/com/kra/vsdc/excute/"
```

For method signatures and annotations, extract `BOOT-INF/classes` and run `javap -p -v` on the relevant controller and DTO. Compare routes and fields against this file; this reference is pinned to **v2.0.21**.

## Open verification tasks

1. Verify exact required/optional fields and constraints for each request DTO (Java bean validation, service logic, KRA acceptance).
2. Inspect nested DTO classes for sales lines, purchases, stock items, item compositions, etc.
3. Verify `/initializer/initializer` verb and semantics before use.
4. Capture sanitized success/error response samples from the running sandbox.
5. Confirm whether queued resend outcomes are distinguishable from KRA-confirmed outcomes.
6. Validate branch-specific state and device initialization before syncing.
