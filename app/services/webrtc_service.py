def build_offer_payload(sdp: str, session_id: str, sender_id: str) -> dict:
    return {
        "type": "offer",
        "sdp": sdp,
        "sessionId": session_id,
        "senderId": sender_id,
    }


def build_answer_payload(sdp: str, session_id: str, sender_id: str) -> dict:
    return {
        "type": "answer",
        "sdp": sdp,
        "sessionId": session_id,
        "senderId": sender_id,
    }
