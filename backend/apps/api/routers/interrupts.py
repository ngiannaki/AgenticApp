"""Resolve a pending human-in-the-loop interrupt.

The turn that raised the interrupt is suspended inside its own SSE stream
(see routers/chat.py); posting a decision here just resolves the Future that
coroutine is awaiting, letting that same stream resume and continue emitting
events.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.apps.api.auth import AuthUser, get_current_user

from backend.apps.api.schemas import InterruptDecisionRequest, InterruptDecisionResponse

router = APIRouter(prefix="/api/sessions", tags=["interrupts"])


@router.post(
    "/{session_id}/interrupts/{interrupt_id}/resume",
    response_model=InterruptDecisionResponse,
)
def resume_interrupt(
    session_id: str,
    interrupt_id: str,
    decision: InterruptDecisionRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> InterruptDecisionResponse:
    session = request.app.state.session_manager.get(session_id)
    # Same 404 for "not yours" as for "doesn't exist" — approving someone
    # else's paused tool call is exactly what this check is here to stop.
    if session is None or session.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Session not found")

    if decision.action == "edit" and decision.arguments is None:
        raise HTTPException(status_code=422, detail="'edit' requires 'arguments'")

    accepted = session.resolve_interrupt(interrupt_id, decision)
    if not accepted:
        raise HTTPException(status_code=404, detail="Interrupt not found or already resolved")

    return InterruptDecisionResponse(interrupt_id=interrupt_id, accepted=True)
