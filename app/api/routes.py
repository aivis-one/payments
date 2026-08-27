"""The three inbound routes of TOR section 8.

Every decision about *what happens* comes from ``app.domain.transitions``; this
module decides only what to persist and what to answer. Three consequences are
worth stating up front because each one is easy to get backwards:

* **The explorer call happens outside every transaction.** The internal retry
  series is up to four calls with 1/2/4 second pauses -- about seven seconds.
  Holding a database transaction open across that would pin a pooled
  connection for the whole window on an endpoint designed to be called
  concurrently.

* **A refusal can still write.** ``TxidAdmission`` against an invoice whose TTL
  has passed comes back refused *and* carrying ``next_status = expired``.
  Answering 409 without persisting would leave the invoice sitting in
  ``created`` past its deadline until something else touched it, and the
  product would never receive the ``expired`` event.

* **The read route writes nothing at all**, even when the clock says the status
  should change. It resolves and reports; the sweeper persists and emits.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import AuthDep, ClientDep, SessionDep, SettingsDep
from app.api.errors import network_not_supported, not_found, refusal
from app.api.schemas import (
    CreateInvoiceRequest,
    InvoiceCreated,
    InvoiceView,
    SubmitTxidRequest,
    TxidResult,
)
from app.config import Settings, UnknownNetworkError
from app.domain.events import InvoiceSnapshot, TimeChecked, TxidAdmission, TxidVerdict
from app.domain.policy import Policy
from app.domain.statuses import AttemptResultCode, InvoiceStatus, Verdict
from app.domain.transitions import Decision, decide
from app.explorers.registry import spec_for
from app.explorers.verify import verify_txid
from app.models import Invoice, InvoiceTxidAttempt

router = APIRouter(prefix="/api/v1", dependencies=[AuthDep])


def _now() -> datetime:
    return datetime.now(UTC)


def _snapshot(invoice: Invoice) -> InvoiceSnapshot:
    """The fields the transition function is allowed to see, and no more."""
    return InvoiceSnapshot(
        status=InvoiceStatus(invoice.status),
        invoice_amount_cents=invoice.invoice_amount_cents,
        attempts_used=invoice.attempts_used,
        expires_at=invoice.expires_at,
        slot_frozen_at=invoice.slot_frozen_at,
        active_txid=invoice.active_txid,
    )


def _remaining(settings: Settings, attempts_used: int) -> int:
    """Attempts left, never negative.

    ``MAX_TXID_ATTEMPTS`` is configurable while invoices outlive restarts, so
    lowering it leaves live invoices whose ``attempts_used`` is already above
    the new ceiling. The transition function handles that with ``>=``; here the
    equivalent is a floor at zero rather than a negative number on the wire.
    """
    return max(0, settings.MAX_TXID_ATTEMPTS - attempts_used)


def _apply(invoice: Invoice, decision: Decision) -> None:
    """Write a decision onto the row. Only fields the decision actually set."""
    invoice.status = decision.next_status.value
    effects = decision.effects
    if effects.attempts_used_delta:
        invoice.attempts_used += effects.attempts_used_delta
    if effects.slot_frozen_at is not None:
        invoice.slot_frozen_at = effects.slot_frozen_at
    if effects.active_txid is not None:
        invoice.active_txid = effects.active_txid
    if effects.credited_amount_cents is not None:
        invoice.credited_amount_cents = effects.credited_amount_cents
    if effects.underpaid is not None:
        invoice.underpaid = effects.underpaid


async def _load(session: AsyncSession, invoice_id: uuid.UUID) -> Invoice:
    invoice = await session.get(Invoice, invoice_id)
    if invoice is None:
        raise not_found()
    return invoice


@router.post("/invoices", response_model=InvoiceCreated, status_code=status.HTTP_201_CREATED)
async def create_invoice(
    body: CreateInvoiceRequest,
    session: SessionDep,
    settings: SettingsDep,
) -> Invoice:
    """Open an invoice and freeze its address and deadline onto the row.

    The network must resolve two ways at once: to a registered adapter and to a
    configured address. One expression, because the ways of getting an
    unserved network here are all the same shape -- a typo from the client, a
    rename applied on one side only, a second consumer, a network the product
    enabled before this service grew an adapter. Refusing on either condition
    covers all of them.

    Both must be checked *before* the row exists. The address is snapshotted,
    so an invoice created with a bad one is a payment instruction to nowhere
    that survives every later correction to config.

    ``expires_at`` is materialised here from ``INVOICE_TTL_MINUTES`` rather
    than recomputed later from config: changing the TTL must not move the
    deadline of an invoice already issued, exactly as rotating the address must
    not move its address.
    """
    try:
        spec_for(body.network)
        address = settings.wallet_address_for(body.network)
    except UnknownNetworkError:
        raise network_not_supported(body.network) from None

    if not address.strip():
        # Belt to the braces of the Settings validator: that one refuses to
        # boot on a blank address, so this branch should be unreachable in a
        # correctly wired process. It stays because the cost of being wrong
        # about that is an invoice nobody can ever pay.
        raise network_not_supported(body.network)

    invoice = Invoice(
        product_ref=body.product_ref,
        network=body.network,
        address=address,
        invoice_amount_cents=body.invoice_amount_cents,
        status=InvoiceStatus.CREATED.value,
        expires_at=_now() + timedelta(minutes=settings.INVOICE_TTL_MINUTES),
    )
    session.add(invoice)
    await session.commit()
    await session.refresh(invoice)
    return invoice


@router.get("/invoices/{invoice_id}", response_model=InvoiceView)
async def read_invoice(
    invoice_id: uuid.UUID,
    session: SessionDep,
    settings: SettingsDep,
) -> InvoiceView:
    """Report the invoice with time-dependent transitions resolved, writing nothing.

    The resolved status can differ from the stored one. That is the design of
    TOR section 11 p.7, not a drift: this route stays a pure read so that
    concurrent polling cannot race, and the status the client sees is never
    stale even though the row is. The event that matches it is emitted by the
    sweeper, so a client polling this route can legitimately see ``expired``
    before the webhook arrives.
    """
    invoice = await _load(session, invoice_id)
    policy = settings.policy_for(invoice.network)
    resolved = decide(_snapshot(invoice), TimeChecked(), _now(), policy)

    return InvoiceView(
        id=invoice.id,
        product_ref=invoice.product_ref,
        network=invoice.network,
        address=invoice.address,
        invoice_amount_cents=invoice.invoice_amount_cents,
        status=resolved.next_status.value,
        credited_amount_cents=invoice.credited_amount_cents,
        underpaid=invoice.underpaid,
        active_txid=invoice.active_txid,
        attempts_used=invoice.attempts_used,
        attempts_remaining=_remaining(settings, invoice.attempts_used),
        expires_at=invoice.expires_at,
        created_at=invoice.created_at,
    )


@router.post("/invoices/{invoice_id}/txid", response_model=TxidResult)
async def submit_txid(
    invoice_id: uuid.UUID,
    body: SubmitTxidRequest,
    session: SessionDep,
    settings: SettingsDep,
    client: ClientDep,
) -> TxidResult:
    """Admit, look up, persist, answer.

    The snapshot is taken once, at admission, and the same one is handed to the
    verdict event. It is deliberately not re-read after the explorer call and
    the row is not locked: concurrency is arbitrated by the INSERT against the
    partial unique index, so a lock on the invoice would add contention without
    removing the ``IntegrityError`` handling that is needed anyway.
    """
    invoice = await _load(session, invoice_id)
    policy = settings.policy_for(invoice.network)
    snapshot = _snapshot(invoice)

    admission = decide(snapshot, TxidAdmission(txid=body.txid), _now(), policy)

    if admission.idempotent_replay:
        # The slot is already held by this very TXID. Answering 409
        # slot_occupied would be the only one of the five codes that is false
        # here -- the slot is not held by another TXID, it is held by this one.
        return await _replay(session, settings, invoice, body.txid)

    if not admission.accepted:
        # The refusal may carry a resolve (created -> expired,
        # awaiting_confirmations -> stalled, or an attempts budget that shrank
        # under a live invoice). Persist it before answering, or the invoice
        # stays wrong until something else touches it.
        _apply(invoice, admission)
        await session.commit()
        raise refusal(admission.refused_by) if admission.refused_by else AssertionError

    result = await verify_txid(
        network=invoice.network,
        txid=body.txid,
        wallet_address=invoice.address,
        settings=settings,
        client=client,
    )

    # Fresh clock: up to seven seconds of retry can have passed, and
    # slot_frozen_at should record when the slot was actually taken.
    decision = decide(snapshot, TxidVerdict(verdict=result.verdict, txid=body.txid), _now(), policy)

    if decision.attempt_record is None:
        # api_error and invalid_format: no row, no attempt, no status change.
        return _result(settings, invoice, decision, result.verdict)

    inserted = await _insert_attempt(
        session, invoice, body.txid, decision.attempt_record, result.from_address
    )
    if not inserted:
        return await _on_collision(session, settings, invoice, snapshot, body.txid, policy)

    _apply(invoice, decision)
    await session.commit()
    return _result(settings, invoice, decision, result.verdict)


async def _insert_attempt(
    session: AsyncSession,
    invoice: Invoice,
    txid: str,
    code: AttemptResultCode,
    from_address: str | None,
) -> bool:
    """Insert the attempt row; return False if the unique index refused it.

    Wrapped in a savepoint so that a rejected INSERT does not poison the outer
    transaction -- without it the whole request would have to be abandoned
    rather than answered.

    Only a ``matched`` row can collide: the index is partial on
    ``result_code = 'matched'``. A second ``not_found`` for the same hash is a
    perfectly ordinary row and inserts without complaint.
    """
    attempt = InvoiceTxidAttempt(
        invoice_id=invoice.id,
        network=invoice.network,
        txid=txid,
        result_code=code.value,
        from_address=from_address,
    )
    try:
        async with session.begin_nested():
            session.add(attempt)
            await session.flush()
    except IntegrityError:
        return False
    return True


async def _on_collision(
    session: AsyncSession,
    settings: Settings,
    invoice: Invoice,
    snapshot: InvoiceSnapshot,
    txid: str,
    policy: Policy,
) -> TxidResult:
    """Somebody else already holds a matched row for this ``(network, txid)``.

    Two very different situations arrive here and the difference is whose
    invoice won:

    * **Ours.** Two concurrent submissions of the same hash against the same
      invoice; both passed admission because neither had committed yet. This is
      a double click, not a second attempt, and it must not cost anything.
    * **Someone else's.** The hash is genuinely spoken for. That is what
      ``already_used`` means, and it is produced here rather than by an
      explorer -- no adapter can know what another invoice holds.
    """
    winner = await session.scalar(
        select(InvoiceTxidAttempt).where(
            InvoiceTxidAttempt.network == invoice.network,
            InvoiceTxidAttempt.txid == txid,
            InvoiceTxidAttempt.result_code == AttemptResultCode.MATCHED.value,
        )
    )
    if winner is not None and winner.invoice_id == invoice.id:
        await session.rollback()
        return await _replay(session, settings, invoice, txid)

    already_used = decide(
        snapshot,
        TxidVerdict(verdict=Verdict.ALREADY_USED, txid=txid),
        _now(),
        policy,
    )
    assert already_used.attempt_record is not None
    await _insert_attempt(
        session, invoice, txid, already_used.attempt_record, from_address=None
    )
    _apply(invoice, already_used)
    await session.commit()
    return _result(settings, invoice, already_used, Verdict.ALREADY_USED)


async def _replay(
    session: AsyncSession, settings: Settings, invoice: Invoice, txid: str
) -> TxidResult:
    """Answer with what the winning submission already produced.

    Re-read rather than reconstructed: the winner may have been another process
    and its row is the only record of what it decided.
    """
    await session.refresh(invoice)
    winner = await session.scalar(
        select(InvoiceTxidAttempt).where(
            InvoiceTxidAttempt.invoice_id == invoice.id,
            InvoiceTxidAttempt.txid == txid,
        )
    )
    code = winner.result_code if winner is not None else AttemptResultCode.MATCHED.value
    return TxidResult(
        status=invoice.status,
        result_code=code,
        attempts_used=invoice.attempts_used,
        attempts_remaining=_remaining(settings, invoice.attempts_used),
    )


def _result(
    settings: Settings, invoice: Invoice, decision: Decision, verdict: Verdict
) -> TxidResult:
    """Build the answer from the decision, not from a second reading of it."""
    return TxidResult(
        status=decision.next_status.value,
        result_code=verdict.value,
        attempts_used=invoice.attempts_used,
        attempts_remaining=_remaining(settings, invoice.attempts_used),
    )

