"""认证与会话：JWT 签发/校验、FastAPI 依赖。"""
import datetime as dt
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request

from . import config
from .database import get_user_by_id
from .security import verify_password

ALG = "HS256"


def create_token(user_id: int) -> str:
    payload = {
        "sub": str(user_id),
        "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=config.JWT_EXPIRE_DAYS),
        "iat": dt.datetime.now(dt.timezone.utc),
    }
    return jwt.encode(payload, config.JWT_SECRET, algorithm=ALG)


def login(username: str, password: str) -> Optional[dict]:
    from .database import get_user_by_username

    user = get_user_by_username(username)
    if not user or not user["enabled"]:
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return {"token": create_token(user["id"]), "user": _public_user(user)}


def _public_user(row) -> dict:
    return {
        "id": row["id"],
        "username": row["username"],
        "role": row["role"],
        "name": row["name"],
        "student_no": row["student_no"],
        "department": row["department"],
        "class_name": row["class_name"],
    }


def get_current_user(request: Request) -> dict:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = auth[7:]
    try:
        payload = jwt.decode(token, config.JWT_SECRET, algorithms=[ALG])
        uid = int(payload["sub"])
    except Exception:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    row = get_user_by_id(uid)
    if not row or not row["enabled"]:
        raise HTTPException(status_code=401, detail="账号不存在或已被禁用")
    return dict(row)


def require_roles(*roles: str):
    def dep(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(status_code=403, detail="没有权限执行此操作")
        return user

    return dep
