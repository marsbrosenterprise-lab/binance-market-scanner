from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from binance_scanner.models import AuditEvent, TradeProposal
from binance_scanner.proposals import approve_proposal, create_proposal
from binance_scanner.risk import RiskAssessment
from binance_scanner.strategies import SignalCandidate


class FakeResult:
    def __init__(self, proposal: TradeProposal | None) -> None:
        self.proposal = proposal

    def scalar_one_or_none(self) -> TradeProposal | None:
        return self.proposal


class FakeSession:
    def __init__(self, proposal: TradeProposal | None = None) -> None:
        self.proposal = proposal
        self.added: list[object] = []
        self.commits = 0

    def add(self, value: object) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for value in self.added:
            if isinstance(value, TradeProposal) and value.id is None:
                value.id = 7

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, _: object) -> FakeResult:
        return FakeResult(self.proposal)


def _candidate() -> SignalCandidate:
    return SignalCandidate(
        symbol="BTCUSDT",
        interval="1m",
        strategy_name="test",
        strategy_version="1.0.0",
        status="candidate",
        side="BUY",
        confidence=0.8,
        entry_price=100.0,
        stop_price=95.0,
        target_price=110.0,
        risk_reward=2.0,
        rationale=("test candidate",),
    )


def _risk() -> RiskAssessment:
    return RiskAssessment(
        accepted=True,
        reasons=("within limits",),
        quantity=1.0,
        notional=100.0,
        estimated_loss=5.0,
        max_loss_fraction=0.005,
    )


@pytest.mark.asyncio
async def test_creation_records_pending_proposal_audit_event() -> None:
    session = FakeSession()

    proposal = await create_proposal(session, _candidate(), _risk(), expiry_seconds=300)

    assert proposal.id == 7
    assert proposal.state == "pending"
    assert any(
        isinstance(item, AuditEvent) and item.event_type == "proposal_created"
        for item in session.added
    )


@pytest.mark.asyncio
async def test_valid_approval_is_single_version_transition() -> None:
    proposal = TradeProposal(
        id=7,
        symbol="BTCUSDT",
        interval="1m",
        state="pending",
        version=1,
        signal_json="{}",
        risk_json="{}",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    session = FakeSession(proposal)

    approved = await approve_proposal(session, 7, "operator", expected_version=1)

    assert approved is proposal
    assert proposal.state == "approved"
    assert proposal.version == 2
    assert proposal.approval_actor == "operator"
    assert session.commits == 1
    assert any(
        isinstance(item, AuditEvent) and item.event_type == "proposal_approved"
        for item in session.added
    )


@pytest.mark.asyncio
async def test_stale_approval_is_rejected_and_audited() -> None:
    proposal = TradeProposal(
        id=7,
        symbol="BTCUSDT",
        interval="1m",
        state="pending",
        version=2,
        signal_json="{}",
        risk_json="{}",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    session = FakeSession(proposal)

    result = await approve_proposal(session, 7, "operator", expected_version=1)

    assert result is None
    assert proposal.state == "pending"
    assert proposal.version == 2
    assert any(
        isinstance(item, AuditEvent) and item.event_type == "proposal_approval_rejected"
        for item in session.added
    )


@pytest.mark.asyncio
async def test_expired_proposal_cannot_be_approved() -> None:
    proposal = TradeProposal(
        id=7,
        symbol="BTCUSDT",
        interval="1m",
        state="pending",
        version=1,
        signal_json="{}",
        risk_json="{}",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    session = FakeSession(proposal)

    result = await approve_proposal(session, 7, "operator", expected_version=1)

    assert result is None
    assert proposal.state == "expired"
    assert any(
        isinstance(item, AuditEvent) and item.event_type == "proposal_expired"
        for item in session.added
    )
