from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from binance_scanner.models import SafetyControl


async def locked_safety_control(session: AsyncSession) -> SafetyControl:
    result = await session.execute(
        select(SafetyControl).where(SafetyControl.id == 1).with_for_update()
    )
    control = result.scalar_one_or_none()
    if control is None:
        control = SafetyControl(id=1, emergency_stop=False)
        session.add(control)
        await session.flush()
    return control


async def execution_is_stopped(session: AsyncSession) -> bool:
    result = await session.execute(select(SafetyControl).where(SafetyControl.id == 1))
    control = result.scalar_one_or_none()
    return bool(control and control.emergency_stop)
