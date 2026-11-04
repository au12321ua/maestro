"""Setup, sessions, invitations and the caller's own account and PATs."""

from fastapi import APIRouter, Depends, Request, Response

from maestro.api.deps import (
    clear_session_cookie,
    client_source,
    maestro,
    ok,
    principal,
    set_session_cookie,
)
from maestro.api.models import (
    Envelope,
    InvitationAccept,
    InvitationInfo,
    InvitationLookup,
    IssuedPat,
    LoginRequest,
    Me,
    PasswordChange,
    Pat,
    PatCreate,
    RecycleEntry,
    SessionInfo,
    SetupRequest,
    SetupStatus,
)
from maestro.domain.states import PatPermission
from maestro.security import tokens
from maestro.services.principal import Principal

router = APIRouter(prefix="/api/v1")


def session_info(request: Request, token: str, me: dict) -> dict:
    return ok(request, {"user": me, "csrf_token": tokens.csrf_for(token)})


def _me(user: dict) -> dict:
    return {**user, "auth_channel": "web_session", "pat_id": None, "pat_permissions": None}


@router.get("/setup", response_model=Envelope[SetupStatus], tags=["setup"])
def setup_status(request: Request):
    return ok(request, {"needs_setup": maestro(request).accounts.needs_setup()})


@router.post("/setup", response_model=Envelope[SessionInfo], status_code=201, tags=["setup"])
def complete_setup(body: SetupRequest, request: Request, response: Response):
    service = maestro(request)
    user, token = service.accounts.initialize_admin(
        body.setup_token, body.username, body.display_name, body.password, client_source(request)
    )
    set_session_cookie(response, service, token)
    return session_info(request, token, _me(user))


@router.post("/session", response_model=Envelope[SessionInfo], status_code=201, tags=["session"])
def login(body: LoginRequest, request: Request, response: Response):
    service = maestro(request)
    user, token = service.accounts.login(body.username, body.password, client_source(request))
    set_session_cookie(response, service, token)
    return session_info(request, token, _me(user))


@router.get("/session", response_model=Envelope[SessionInfo], tags=["session"])
def current_session(request: Request, who: Principal = Depends(principal)):
    me = maestro(request).accounts.me(who)
    cookie = request.cookies.get("maestro_session")
    csrf = tokens.csrf_for(cookie) if who.session_id and cookie else None
    return ok(request, {"user": me, "csrf_token": csrf})


@router.delete("/session", status_code=204, tags=["session"])
def logout(request: Request, response: Response, who: Principal = Depends(principal)):
    service = maestro(request)
    service.accounts.logout(who)
    clear_session_cookie(response, service)
    response.status_code = 204


@router.post("/invitations/lookup", response_model=Envelope[InvitationInfo], tags=["session"])
def inspect_invitation(body: InvitationLookup, request: Request):
    info = maestro(request).accounts.inspect_invitation(body.token, client_source(request))
    return ok(request, info)


@router.post(
    "/invitations/accept", response_model=Envelope[SessionInfo], status_code=201, tags=["session"]
)
def accept_invitation(body: InvitationAccept, request: Request, response: Response):
    service = maestro(request)
    user, token = service.accounts.accept_invitation(
        body.token, body.username, body.display_name, body.password, client_source(request)
    )
    set_session_cookie(response, service, token)
    return session_info(request, token, _me(user))


@router.get("/me", response_model=Envelope[Me], tags=["me"])
def get_me(request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).accounts.me(who))


@router.post("/me/password", status_code=204, tags=["me"])
def change_password(body: PasswordChange, request: Request, who: Principal = Depends(principal)):
    maestro(request).accounts.change_password(who, body.current_password, body.new_password)
    return Response(status_code=204)


@router.get("/me/pats", response_model=Envelope[list[Pat]], tags=["me"])
def list_pats(request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).pats.list_own(who))


@router.post("/me/pats", response_model=Envelope[IssuedPat], status_code=201, tags=["me"])
def create_pat(body: PatCreate, request: Request, who: Principal = Depends(principal)):
    pat = maestro(request).pats.create(
        who,
        body.name,
        [PatPermission(x) for x in body.permissions],
        body.expires_in_days,
        body.run_limit,
    )
    return ok(request, pat)


@router.post("/me/pats/{pat_id}/revoke", response_model=Envelope[Pat], tags=["me"])
def revoke_pat(pat_id: str, request: Request, who: Principal = Depends(principal)):
    return ok(request, maestro(request).pats.revoke(who, pat_id))


@router.delete("/me/pats/{pat_id}", response_model=Envelope[RecycleEntry], tags=["me"])
def delete_pat(pat_id: str, request: Request, who: Principal = Depends(principal)):
    """Revokes the token at once and removes it from the list."""
    return ok(request, maestro(request).recycle.delete(who, "pat", pat_id))
