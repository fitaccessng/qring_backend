from app.schemas.visitor import VisitorRequestCreate


def test_visitor_request_schema_normalizes_common_frontend_aliases():
    payload = VisitorRequestCreate(
        qrId="qr-1",
        residentId="door-1",
        visitorName="Jane Visitor",
        phone="+2348000000000",
        purposeOfVisit="Seeing Kelvin Ibeh",
        snapshot="abc123",
        photoMime="image/jpeg",
    )

    assert payload.doorId == "door-1"
    assert payload.name == "Jane Visitor"
    assert payload.phoneNumber == "+2348000000000"
    assert payload.purpose == "Seeing Kelvin Ibeh"
    assert payload.snapshotBase64 == "abc123"
    assert payload.snapshotMime == "image/jpeg"
