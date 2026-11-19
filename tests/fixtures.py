"""Synthetic payloads in the shape of /api/movies-data/v4/showtimes-by-event/primary-dynamic."""


def show(time, code, session, date, cats, attr=""):
    return {"title": time, "screenAttr": attr, "additionalData": {
        "sessionId": session, "showTimeCode": code, "showDateCode": date,
        "categories": [{"priceDesc": n, "curPrice": p, "availStatus": s} for n, p, s in cats]}}


def venue(code, name, shows):
    return {"type": "venue-card", "additionalData": {"venueCode": code, "venueName": name}, "showtimes": shows}


def payload(dates, venues, name="Avengers: Doomsday", lang="English • 3D, IMAX 3D", formats=()):
    return {"data": {
        "topStickyWidgets": [
            {"type": "horizontal-text-list", "data": [{"leftText": {"data": [{"components": [{"text": lang}]}]}}]},
            {"type": "horizontal-block-list", "data": [
                {"id": d, "styleId": st, "data": [{"text": "FRI"}, {"text": d[-2:]}, {"text": "DEC"}]} for d, st in dates]},
        ],
        "bottomSheetData": {"format-selector": {"widgets": [
            {"type": "vertical-text-list", "data": [{"styleId": "bottomsheet-subtitle", "text": name}]},
            {"type": "list", "data": [{"cta": {"url": f"/buytickets/{c}"}} for c in formats]},
        ]}},
        "showtimeWidgets": [{"type": "groupList", "data": [{"type": "venueGroup", "data": venues}]}] if venues else [],
    }}


NOT_OPEN = payload([("20261218", "date-disabled"), ("20261219", "date-disabled")], [])

PHOENIX = "PVR: Phoenix MarketCity, Velachery"
OPEN_ELSEWHERE = payload(
    [("20261218", "date-selected"), ("20261219", "date-disabled")],
    [venue("AGSM", "AGS Cinemas: Maduravoyal", [show("09:00 AM", "0900", "1", "20261218", [("DIAMOND", "210.00", "3")])])],
)
OPEN_PHOENIX = payload(
    [("20261218", "date-selected"), ("20261219", "date-default")],
    [
        venue("AGSM", "AGS Cinemas: Maduravoyal", [show("09:00 AM", "0900", "1", "20261218", [("DIAMOND", "210.00", "3")])]),
        venue("PVPH", PHOENIX, [
            show("07:30 PM", "1930", "77", "20261218", [("RECLINER", "650.00", "0"), ("PRIME", "390.00", "2")], "IMAX 3D"),
            show("10:45 AM", "1045", "78", "20261218", [("PRIME", "390.00", "0")], "3D"),
        ]),
    ],
    formats=["ET00526306"],
)
