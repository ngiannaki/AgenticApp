"""Answer a pending ask_user question.

The turn that raised the question is suspended inside its own SSE stream
(see routers/chat.py and streaming_agent.py's _handle_ask_user); posting an
answer here just resolves the Future that coroutine is awaiting, letting that
same stream resume and continue emitting events.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from backend.apps.api.auth import AuthUser, get_current_user

from backend.apps.api.schemas import QuestionAnswerRequest, QuestionAnswerResponse

router = APIRouter(prefix="/api/sessions", tags=["questions"])


@router.post(
    "/{session_id}/questions/{question_id}/answer",
    response_model=QuestionAnswerResponse,
)
def answer_question(
    session_id: str,
    question_id: str,
    answer: QuestionAnswerRequest,
    request: Request,
    user: AuthUser = Depends(get_current_user),
) -> QuestionAnswerResponse:
    session = request.app.state.session_manager.get(session_id)
    if session is None or session.user_id != user.user_id:
        raise HTTPException(status_code=404, detail="Session not found")

    accepted = session.resolve_question(question_id, answer)
    if not accepted:
        raise HTTPException(status_code=404, detail="Question not found or already answered")

    return QuestionAnswerResponse(question_id=question_id, accepted=True)
