"""bms-check (GitHub Actions mode) with BookMyShow and notifiers faked."""
import json
import sys

from app import bms, cli, notify
from tests import fixtures as fx


def test_cli_flow(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state.json"
    current = {"p": fx.NOT_OPEN}
    sent = []

    class Fake:
        async def showtimes(self, ec, dc, city, **kw):
            return bms.Fetched(current["p"] if not dc else {"data": {}}, "http")

        async def aclose(self):
            pass

    async def tg(text, settings=None):
        sent.append(text)
        return True, "sent"

    async def nt(*a, **k):
        return False, "ntfy not configured"

    monkeypatch.setattr(bms, "Bms", Fake)
    monkeypatch.setattr(notify, "telegram", tg)
    monkeypatch.setattr(notify, "ntfy", nt)

    def run():
        monkeypatch.setattr(sys, "argv", ["bms-check", "--movie",
                                          "https://in.bookmyshow.com/movies/chennai/avengers-doomsday/ET00439706",
                                          "--theatre", "PVR Phoenix mall Velachery", "--state", str(state)])
        try:
            cli.main()
        except SystemExit as e:
            assert e.code == 0

    run()
    assert sent == [] and json.loads(state.read_text())["status"] == "not_open"
    mtime = state.stat().st_mtime_ns
    run()  # nothing changed -> file untouched (no git commit)
    assert state.stat().st_mtime_ns == mtime

    current["p"] = fx.OPEN_ELSEWHERE
    run()
    assert sent == []

    current["p"] = fx.OPEN_PHOENIX
    run()
    assert len(sent) == 1 and "tickets open" in sent[0] and "Phoenix MarketCity" in sent[0]
    run()
    assert len(sent) == 1  # no duplicate
    assert "Phoenix MarketCity" in capsys.readouterr().out
