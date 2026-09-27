from __future__ import annotations

from dataclasses import dataclass

from binance_scanner.strategies import SignalCandidate


@dataclass(frozen=True, slots=True)
class RiskConfig:
    risk_per_trade_fraction: float = 0.01
    max_notional_fraction: float = 0.10
    max_total_exposure_fraction: float = 0.50
    min_available_balance_reserve_fraction: float = 0.20

    def __post_init__(self) -> None:
        fractions = (
            self.risk_per_trade_fraction,
            self.max_notional_fraction,
            self.max_total_exposure_fraction,
            self.min_available_balance_reserve_fraction,
        )
        if any(fraction <= 0 or fraction >= 1 for fraction in fractions):
            raise ValueError("risk fractions must be greater than 0 and less than 1")


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    accepted: bool
    reasons: tuple[str, ...]
    quantity: float | None
    notional: float | None
    estimated_loss: float | None
    max_loss_fraction: float


class RiskEngine:
    def __init__(self, config: RiskConfig | None = None) -> None:
        self.config = config or RiskConfig()

    def assess(
        self,
        candidate: SignalCandidate,
        account_balance: float,
        current_exposure: float = 0.0,
        available_balance: float | None = None,
    ) -> RiskAssessment:
        reasons: list[str] = []
        if candidate.status != "candidate" or candidate.side != "BUY":
            reasons.append("signal is not an actionable long candidate")
        if account_balance <= 0:
            reasons.append("account balance must be positive")
        if current_exposure < 0:
            reasons.append("current exposure cannot be negative")
        if available_balance is None:
            available_balance = account_balance - current_exposure
        if available_balance < 0:
            reasons.append("available balance cannot be negative")
        if candidate.entry_price is None or candidate.stop_price is None:
            reasons.append("entry and stop prices are required")
        elif candidate.entry_price <= candidate.stop_price:
            reasons.append("stop price must be below entry price")

        if reasons:
            return self._rejected(reasons)

        assert candidate.entry_price is not None
        assert candidate.stop_price is not None
        risk_per_unit = candidate.entry_price - candidate.stop_price
        risk_budget = account_balance * self.config.risk_per_trade_fraction
        max_notional = account_balance * self.config.max_notional_fraction
        quantity_by_risk = risk_budget / risk_per_unit
        quantity_by_notional = max_notional / candidate.entry_price
        quantity = min(quantity_by_risk, quantity_by_notional)
        notional = quantity * candidate.entry_price
        estimated_loss = quantity * risk_per_unit
        max_exposure = account_balance * self.config.max_total_exposure_fraction
        reserve = account_balance * self.config.min_available_balance_reserve_fraction

        if current_exposure + notional > max_exposure:
            reasons.append("maximum total exposure would be exceeded")
        if available_balance - notional < reserve:
            reasons.append("minimum available-balance reserve would be breached")
        if notional <= 0 or estimated_loss <= 0:
            reasons.append("calculated position size is not positive")
        if reasons:
            return self._rejected(reasons)
        return RiskAssessment(
            accepted=True,
            reasons=("position is within configured risk limits",),
            quantity=quantity,
            notional=notional,
            estimated_loss=estimated_loss,
            max_loss_fraction=estimated_loss / account_balance,
        )

    def _rejected(self, reasons: list[str]) -> RiskAssessment:
        return RiskAssessment(
            accepted=False,
            reasons=tuple(reasons),
            quantity=None,
            notional=None,
            estimated_loss=None,
            max_loss_fraction=0.0,
        )
