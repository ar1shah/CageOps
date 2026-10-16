# Upcoming fight-details pages (saved by hand, 2026-10-15)

Two `fight-details` pages for bouts on a card that had not happened yet (UFC 333), saved from a
normal browser session (the D-017 practice) and minimized with `scripts/minimize_fixture.py`
(deletes only).

| file | page says |
|---|---|
| `fight_upcoming_ce1388d6c7651d2e.html` | "UFC Featherweight Title Bout", belt image |
| `fight_upcoming_b9004bd72276a190.html` | "Flyweight Bout", no belt |

They are **not** in the parent folder on purpose: `test_layout.py` requires every page there to
parse with its own parser, and `parse_fight` cannot read these (they have no result block). What
they are evidence for is D-044: an upcoming fight page carries the title marker but **no round
count** ("Time format" and the whole Method / Round / Time / Referee block are absent), so there
is no source for `scheduled_rounds`, and the scheduled-fight job was not built.
`tests/parsers/test_upcoming_fight_page.py` pins exactly that.
