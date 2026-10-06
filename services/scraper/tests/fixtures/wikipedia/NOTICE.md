# Notice: these fixtures are CC BY-SA 4.0, not the repository licence

The HTML files in this directory are trimmed copies of English Wikipedia articles. Their text is
© the Wikipedia contributors and is licensed under the
[Creative Commons Attribution-ShareAlike 4.0 International License](https://creativecommons.org/licenses/by-sa/4.0/)
(Wikipedia's [Terms of Use](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use)). They are
**not** covered by this repository's licence. Anything you build from these files must keep the
same licence and credit the articles. Each article's authors are listed in its page history.

Retrieved on 2026-10-06 with this project's own fetcher (robots.txt checked, one request per
second, a User-Agent that names the project and a contact). The pages are used only to test the
parsers in `services/scraper/src/cageops_scraper/parsers/wikipedia_*.py`.

| File | Article | Page id | Revision read |
|---|---|---|---|
| `2026_in_UFC.html` | [2026 in UFC](https://en.wikipedia.org/wiki/2026_in_UFC) | 80932642 | [1378832013](https://en.wikipedia.org/w/index.php?title=2026_in_UFC&oldid=1378832013) |
| `UFC_332.html` | [UFC 332](https://en.wikipedia.org/wiki/UFC_332) | 83826247 | [1378760261](https://en.wikipedia.org/w/index.php?title=UFC_332&oldid=1378760261) |
| `UFC_Fight_Night_279.html` | [UFC Fight Night: Kape vs. Horiguchi](https://en.wikipedia.org/wiki/UFC_Fight_Night:_Kape_vs._Horiguchi) | 82772799 | [1369737950](https://en.wikipedia.org/w/index.php?title=UFC_Fight_Night:_Kape_vs._Horiguchi&oldid=1369737950) |
| `UFC_323.html` | [UFC 323](https://en.wikipedia.org/wiki/UFC_323) | 80893887 | [1345983795](https://en.wikipedia.org/w/index.php?title=UFC_323&oldid=1345983795) |
| `UFC_321.html` | [UFC 321](https://en.wikipedia.org/wiki/UFC_321) | 80330203 | [1374969661](https://en.wikipedia.org/w/index.php?title=UFC_321&oldid=1374969661) |
| `UFC_259.html` | [UFC 259](https://en.wikipedia.org/wiki/UFC_259) | 65611436 | [1370512355](https://en.wikipedia.org/w/index.php?title=UFC_259&oldid=1370512355) |
| `UFC_244.html` | [UFC 244](https://en.wikipedia.org/wiki/UFC_244) | 60744212 | [1345997506](https://en.wikipedia.org/w/index.php?title=UFC_244&oldid=1345997506) |
| `UFC_249.html` | [UFC 249](https://en.wikipedia.org/wiki/UFC_249) | 62581056 | [1336221891](https://en.wikipedia.org/w/index.php?title=UFC_249&oldid=1336221891) |

## What was changed

`scripts/minimize_wikipedia_fixture.py` removed everything except the page head (title, canonical
link and the inline config that carries the page and revision ids), the heading, the infobox, the
"Results" section's table(s) and only the footnotes those tables point at; for `2026_in_UFC.html`
the "Past events" heading and table. Prose, images, navigation and all other tables were dropped.
The kept nodes are otherwise as saved (re-serialized, not byte-identical).
