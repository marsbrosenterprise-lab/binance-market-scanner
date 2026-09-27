from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from binance_scanner.models import AuditEvent, TradeProposal
from binance_scanner.risk import RiskAssessment
from binance_scanner.strategies import SignalCandidate


def proposal_payload(candidate: SignalCandidate, risk: RiskAssessment) -> tuple[str, str]:
    return json.dumps(asdict(candidate), separators=(",", ":")), json.dumps(
        asdict(risk), separators=(",", ":")
    )


async def record_audit_event(
    session: AsyncSession,
    event_type: str,
    component: str,
    payload: dict[str, object],
    correlation_id: str | None = None,
) -> None:
    session.add(
        AuditEvent(
            event_type=event_type,
            component=component,
            correlation_id=correlation_id,
            payload_json=json.dumps(payload, separators=(",", ":")),
        )
    )


async def create_proposal(
    session: AsyncSession,
    candidate: SignalCandidate,
    risk: RiskAssessment,
    expiry_seconds: int,
    actor: str = "system",
) -> TradeProposal:
    signal_json, risk_json = proposal_payload(candidate, risk)
    proposal = TradeProposal(
        symbol=candidate.symbol,
        interval=candidate.interval,
        state="pending" if risk.accepted else "rejected",
        version=1,
        signal_json=signal_json,
        risk_json=risk_json,
        expires_at=datetime.now(UTC) + timedelta(seconds=expiry_seconds),
    )
    session.add(proposal)
    await session.flush()
    await record_audit_event(
        session,
        "proposal_created",
        "proposal_service",
        {
            "proposal_id": proposal.id,
            "state": proposal.state,
            "symbol": proposal.symbol,
            "actor": actor,
        },
        str(proposal.id),
    )
    return proposal


async def expire_pending_proposals(session: AsyncSession) -> int:
    result = await session.execute(
        select(TradeProposal).where(
            TradeProposal.state == "pending", TradeProposal.expires_at <= datetime.now(UTC)
        )
    )
    proposals = result.scalars().all()
    for proposal in proposals:
        proposal.state = "expired"
        await record_audit_event(
            session,
            "proposal_expired",
            "proposal_service",
            {"proposal_id": proposal.id, "symbol": proposal.symbol},
            str(proposal.id),
        )
    return len(proposals)


async def approve_proposal(
    session: AsyncSession, proposal_id: int, actor: str, expected_version: int
) -> TradeProposal | None:
    result = await session.execute(
        select(TradeProposal).where(TradeProposal.id == proposal_id).with_for_update()
    )
    proposal = result.scalar_one_or_none()
    if proposal is None:
        return None
    if proposal.state != "pending" or proposal.version != expected_version:
        await record_audit_event(
            session,
            "proposal_approval_rejected",
            "approval_service",
            {"proposal_id": proposal.id, "state": proposal.state, "actor": actor},
            str(proposal.id),
        )
        await session.commit()
        return None
    if proposal.expires_at <= datetime.now(UTC):
        proposal.state = "expired"
        await record_audit_event(
            session,
            "proposal_expired",
            "approval_service",
            {"proposal_id": proposal.id, "actor": actor},
            str(proposal.id),
        )
        await session.commit()
        return None
    proposal.state = "approved"
    proposal.version += 1
    proposal.approval_actor = actor
    proposal.approved_at = datetime.now(UTC)
    await record_audit_event(
        session,
        "proposal_approved",
        "approval_service",
        {"proposal_id": proposal.id, "actor": actor, "version": proposal.version},
        str(proposal.id),
    )
    await session.commit()
    return proposal
