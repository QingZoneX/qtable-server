from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from typing import Literal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.security import JWTError, decode_access_token
from app.db.session import get_db
from app.models.user import User
from app.models.workspace_member import WorkspaceMember, WorkspaceRole

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


async def get_current_user(
    token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)
) -> User:
    try:
        payload = decode_access_token(token)
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user_id = payload.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def require_workspace_role(
    workspace_id: int,
    current_user: User,
    min_role: Literal["viewer", "editor", "owner"] = "viewer",
    db: AsyncSession = Depends(get_db),
) -> WorkspaceMember:
    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.user_id == current_user.id,
            WorkspaceMember.workspace_id == str(workspace_id),
        )
    )
    member = result.scalars().first()
    if not member:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to workspace")
    role_rank = {"viewer": 1, "editor": 2, "owner": 3}
    min_rank = role_rank.get(min_role)
    if min_rank is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role")
    role_value = member.role.value if hasattr(member.role, "value") else str(member.role)
    member_rank = role_rank.get(role_value)
    if member_rank is None or member_rank < min_rank:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
    return member
