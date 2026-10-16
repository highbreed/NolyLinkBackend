"""Typed VSCU exceptions.

Every VSCUError carries enough to reconstruct what happened without re-reading logs: which
endpoint, what the local VSCU app (or real KRA, passed through) said, the HTTP transport
outcome, and whether retrying is safe. `retryable` is deliberately conservative — see each
subclass — because a blind retry on a fiscal endpoint can duplicate a real transaction; the
reconciliation flow (Phase 4) is what actually decides "safe to resubmit", this flag is only a
starting hint.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class VSCUErrorContext:
    endpoint: str
    result_cd: str | None = None
    result_msg: str | None = None
    result_dt: str | None = None
    http_status: int | None = None
    correlation_id: str | None = None
    sap_doc_reference: str | None = None
    invoice_no: int | None = None


class VSCUError(Exception):
    """Base for every VSCU integration error. Never include cmcKey/signKey/intrlKey/passwords
    in args or str(self) — those never reach this layer in the first place (the local VSCU app
    strips them before responding), but nothing here should assume that of every future caller.
    """

    retryable: bool = False

    def __init__(self, context: VSCUErrorContext, message: str | None = None) -> None:
        self.context = context
        self.message = message or self._default_message()
        super().__init__(self.message)

    def _default_message(self) -> str:
        c = self.context
        return (
            f"VSCU {c.endpoint}: resultCd={c.result_cd!r} resultMsg={c.result_msg!r} "
            f"http_status={c.http_status!r}"
        )


class VSCUConfigurationError(VSCUError):
    """vscu_enabled is False, or a required setting (base URL, TIN, branch id) is missing.
    Never retryable — no request was ever attempted.
    """

    retryable = False


class VSCUTransportError(VSCUError):
    """The HTTP call to the local VSCU app itself failed — connection refused, DNS, timeout,
    or a non-2xx HTTP status — before any VSCU JSON envelope existed to inspect. Distinct from
    VSCUUnavailableError (896/894 resultCd — the local app answered, but *it* couldn't reach
    KRA); this is our own network to the local app being down.
    """

    retryable = True


class VSCUValidationError(VSCUError):
    """891-899/910-912-range resultCd from real KRA (passed through), or 834 from the local
    app (SalesType/ReceiptType combination invalid). A payload bug — do not retry unmodified.
    """

    retryable = False


class VSCUSequenceError(VSCUError):
    """836 (local) — the local VSCU app's own sequence counters were altered/deleted server-
    side. Per spec: stop automatic resubmission, resync sequences, reconcile before retrying.
    """

    retryable = False


class VSCUDuplicateSubmissionError(VSCUError):
    """924 (local) — this exact invoice number was already processed by the local app. Treat
    as success-equivalent once reconciled against our own transaction record, never as a
    reason to submit a *new* invoice number blindly.
    """

    retryable = False


class VSCUUnavailableError(VSCUError):
    """894/896 resultCd — the local VSCU app answered but could not reach real KRA (offline,
    or KRA itself down). In OFFLINE exec_mode this is expected/normal and the local app queues
    the transaction for its own resend scheduler — see TrnsSalesExcute.saveTrnsSales, which
    rewrites 894 -> 000 ("SIGNED BUT NOT TRANSMITTED") once it has queued the resend. Callers
    that see this exception directly are seeing the *pre*-rewrite state (e.g. from an endpoint
    that doesn't do that rewrite) — reconcile, don't resubmit blindly.
    """

    retryable = True


class VSCUFiscalizationError(VSCUError):
    """A sales/purchase fiscalization-specific failure that doesn't fit the categories above —
    kept distinct so fiscal-endpoint callers can catch it separately from reference-data calls.
    """

    retryable = False


class VSCUUnknownError(VSCUError):
    """A resultCd not in app/integrations/vscu/constants.py's known tables. Do not guess at
    retryability — surfaced as non-retryable so it requires a human look, per spec §40's rule
    against inventing behavior the source doesn't establish.
    """

    retryable = False
