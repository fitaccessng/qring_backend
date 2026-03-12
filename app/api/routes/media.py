from fastapi import APIRouter, Response

from app.services.voice_note_service import load_voice_note
from app.services.payment_proof_service import load_payment_proof

router = APIRouter()


@router.get("/media/voice-notes/{filename}")
def media_voice_note(filename: str):
    data, content_type = load_voice_note(filename)
    return Response(content=data, media_type=content_type)


@router.get("/media/payment-proofs/{filename}")
def media_payment_proof(filename: str):
    data, content_type = load_payment_proof(filename)
    return Response(content=data, media_type=content_type)
