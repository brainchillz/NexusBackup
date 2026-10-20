"""Request authentication: session cookie (UI) or Bearer token (CLI, scripts), and
Bearer proxy token for the agent endpoints."""
from fastapi import Depends, HTTPException, Request

from ..core import auth

COOKIE = 'hvb_session'


def current_user(request: Request):
    h = request.headers.get('authorization', '')
    if h.lower().startswith('bearer '):
        u = auth.authenticate_token(h[7:].strip())
        if u:
            return u
        raise HTTPException(401, 'invalid token')
    u = auth.read_session(request.cookies.get(COOKIE))
    if u:
        return u
    raise HTTPException(401, 'not signed in')


def require(role):
    def dep(user=Depends(current_user)):
        if not auth.role_at_least(user['role'], role):
            raise HTTPException(403, f'requires role {role}')
        return user
    return dep


def current_proxy(request: Request):
    h = request.headers.get('authorization', '')
    if h.lower().startswith('bearer '):
        p = auth.authenticate_proxy(h[7:].strip())
        if p:
            return p
    raise HTTPException(401, 'invalid proxy token')
