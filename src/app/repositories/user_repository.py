from __future__ import annotations

from motor.motor_asyncio import AsyncIOMotorDatabase

from src.app.db import crud
from src.app.db.base import utcnow
from src.app.db.models.user import User


async def get_or_create_by_email(
    db: AsyncIOMotorDatabase,
    *,
    email: str,
    display_name: str | None = None,
    jira_account_id: str | None = None,
) -> User:
    user = await crud.find_one(db, User, {"email": email})
    if user is None:
        user = User(
            email=email,
            display_name=display_name,
            jira_account_id=jira_account_id,
        )
        return await crud.insert(db, user)

    user.last_seen_at = utcnow()
    if display_name and not user.display_name:
        user.display_name = display_name
    if jira_account_id and not user.jira_account_id:
        user.jira_account_id = jira_account_id
    return await crud.save(db, user)
