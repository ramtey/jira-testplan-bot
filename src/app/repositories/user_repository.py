from __future__ import annotations

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.app.db.base import utcnow
from src.app.db.models.user import User


async def get_or_create_by_email(
    session: AsyncSession,
    *,
    email: str,
    display_name: str | None = None,
    jira_account_id: str | None = None,
) -> User:
    result = await session.exec(select(User).where(User.email == email))
    user = result.first()
    if user is None:
        user = User(
            email=email,
            display_name=display_name,
            jira_account_id=jira_account_id,
        )
        session.add(user)
        await session.flush()
        return user

    user.last_seen_at = utcnow()
    if display_name and not user.display_name:
        user.display_name = display_name
    if jira_account_id and not user.jira_account_id:
        user.jira_account_id = jira_account_id
    session.add(user)
    await session.flush()
    return user
