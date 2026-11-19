from app import parse
from tests import fixtures as fx


def test_parse_movie_input_variants():
    r = parse.parse_movie_input("https://in.bookmyshow.com/movies/chennai/avengers-doomsday/ET00439706", "mumbai")
    assert (r.city, r.slug, r.event_code, r.title) == ("chennai", "avengers-doomsday", "ET00439706", "Avengers Doomsday")
    r = parse.parse_movie_input("https://in.bookmyshow.com/movies/chennai/dhurandhar-the-revenge/buytickets/ET00478890/20261218", "x")
    assert (r.city, r.slug, r.event_code) == ("chennai", "dhurandhar-the-revenge", "ET00478890")
    r = parse.parse_movie_input("et00439706", "chennai")
    assert r.event_code == "ET00439706" and r.city == "chennai"
    assert parse.parse_movie_input("doomsday", "chennai") is None


def test_rank_movies_by_name():
    html = ('<a href="/movies/chennai/avengers-doomsday/ET00439706">x</a>'
            '<a href="/movies/om-operation-mole/ET00504901">y</a>'
            '<a href="/movies/avengers-doomsday-msinfinity-vsn-3d/ET00526306">z</a>')
    refs = parse.rank_movies(parse.movie_links_from_html(html), "chennai", "doomsday")
    assert [r.event_code for r in refs] == ["ET00439706", "ET00526306"]


def test_theatre_matching():
    assert parse.theatre_matches(fx.PHOENIX, "PVR Phoenix mall Velachery")
    assert parse.theatre_matches(fx.PHOENIX, "pvr velachery")
    assert parse.theatre_matches(fx.PHOENIX, "INOX, PVR")
    assert not parse.theatre_matches("PVR: Grand Galada, Pallavaram", "PVR Phoenix Velachery")
    assert not parse.theatre_matches("AGS Cinemas: Maduravoyal", "PVR")
    assert parse.theatre_matches("anything", "")


def test_parse_payload():
    p = fx.OPEN_PHOENIX
    assert parse.parse_movie_info(p) == {"name": "Avengers: Doomsday", "language": "English • 3D, IMAX 3D"}
    assert [(d.date_code, d.status) for d in parse.parse_dates(p)] == [("20261218", "BOOKABLE"), ("20261219", "BOOKABLE")]
    shows = parse.parse_shows(p)
    assert len(shows) == 3
    ph = [s for s in shows if s.venue_code == "PVPH"]
    assert ph[0].screen_attr == "IMAX 3D" and ph[0].available and not ph[1].available
    assert parse.parse_format_codes(p, "ET00439706") == ["ET00526306"]
    assert parse.parse_shows(fx.NOT_OPEN) == []
    assert all(d.status == "NOT_OPEN" for d in parse.parse_dates(fx.NOT_OPEN))


def test_filters():
    shows = parse.parse_shows(fx.OPEN_PHOENIX)
    assert len(parse.filter_shows(shows, theatre="PVR Phoenix Velachery")) == 2
    assert [s.time for s in parse.filter_shows(shows, theatre="phoenix", periods="night")] == ["07:30 PM"]
    assert [s.time for s in parse.filter_shows(shows, periods="morning")] == ["09:00 AM", "10:45 AM"]
    assert parse.filter_shows(shows, dates="20261219") == []


def test_change_detection():
    theatre = "PVR Phoenix Velachery"
    s0 = parse.build_state(parse.filter_shows(parse.parse_shows(fx.OPEN_ELSEWHERE), theatre),
                           parse.parse_dates(fx.OPEN_ELSEWHERE))
    assert s0["shows"] == {}
    s1 = parse.build_state(parse.filter_shows(parse.parse_shows(fx.OPEN_PHOENIX), theatre),
                           parse.parse_dates(fx.OPEN_PHOENIX))
    ch = parse.detect_changes(s0, s1)
    assert any("Date opened: 19/12" in c for c in ch)
    assert sum("New show" in c for c in ch) == 2  # one line per session, not per category
    # sold out -> available
    s2 = {"shows": {k: dict(v) for k, v in s1["shows"].items()}, "dates": s1["dates"]}
    for v in s2["shows"].values():
        if v["time"] == "10:45 AM":
            v["status"] = "3"
    ch2 = parse.detect_changes(s1, s2)
    assert len(ch2) == 1 and "Back in stock" in ch2[0] and "10:45 AM" in ch2[0]
    assert parse.detect_changes({}, s1) == []
