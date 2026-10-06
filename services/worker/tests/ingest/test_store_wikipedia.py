"""Writing a Wikipedia event: stable keys, names, and per-field precedence against ufcstats (D-029).

The pages are trimmed copies of real articles. ufcstats rows are made by hand where a test needs
them, because the local database starts empty."""

import itertools
from datetime import timedelta

import pytest
from sqlalchemy import text

from cageops_worker.ingest import mapping_wikipedia
from cageops_worker.ingest.errors import MappingError
from cageops_worker.ingest.mapping_wikipedia import map_bout
from cageops_worker.ingest.store_wikipedia import write_wikipedia_event
from cageops_worker.ingest.upsert import UpsertCounts

_ids = itertools.count(1)


def hexid() -> str:
    return f"{next(_ids):016x}"


def write(db, page, bouts=None):
    mapped = [map_bout(b) for b in (bouts if bouts is not None else page.bouts)]
    with db.begin() as conn:
        return write_wikipedia_event(conn, page, mapped)


def rows(db, sql, **params):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def one(db, sql, **params):
    [row] = rows(db, sql, **params)
    return row


def count(db, table):
    return one(db, f"SELECT count(*) AS n FROM {table}")["n"]


def versions(db):
    return one(
        db,
        "SELECT (SELECT string_agg(xmin::text, ',' ORDER BY id) FROM fights) AS f,"
        " (SELECT string_agg(xmin::text, ',' ORDER BY id) FROM events) AS e,"
        " (SELECT string_agg(xmin::text, ',' ORDER BY id) FROM fighters) AS p",
    )


def ufcstats_card(db, page, bouts, *, completed=None, day=0):
    """A ufcstats event (real-looking hex ids) with these bouts. completed=None: scheduled with no
    result. completed="first": the first-named fighter won, a ufcstats result."""
    with db.begin() as conn:
        event_id = conn.execute(
            text(
                "INSERT INTO events (ufcstats_id, name, event_date)"
                " VALUES (:u, :n, :d) RETURNING id"
            ),
            {"u": hexid(), "n": page.name, "d": page.event_date + timedelta(days=day)},
        ).scalar_one()
        fighters, fights = {}, []
        for bout in bouts:
            for person in (bout.first, bout.second):
                if person.name not in fighters:
                    fighters[person.name] = conn.execute(
                        text(
                            "INSERT INTO fighters (ufcstats_id, name) VALUES (:u, :n) RETURNING id"
                        ),
                        {"u": hexid(), "n": person.name},
                    ).scalar_one()
            first, second = fighters[bout.first.name], fighters[bout.second.name]
            done = completed is not None
            fights.append(
                conn.execute(
                    text(
                        "INSERT INTO fights (ufcstats_id, event_id, fighter_a_id, fighter_b_id,"
                        " weight_class, gender, status, is_title_fight, outcome, winner_id, method,"
                        " has_round_stats, result_source) VALUES (:u, :e, :a, :b, 'Lightweight',"
                        " 'M', :s, :t, :o, :w, :m, false, :src) RETURNING id"
                    ),
                    {
                        "u": hexid(),
                        "e": event_id,
                        "a": min(first, second),
                        "b": max(first, second),
                        "s": "completed" if done else "scheduled",
                        "t": False if done else None,
                        "o": "win" if done else None,
                        "w": (first if completed == "first" else second) if done else None,
                        "m": "decision" if done else None,
                        "src": "ufcstats" if done else None,
                    },
                ).scalar_one()
            )
    return event_id, fights


# -- a fresh card ------------------------------------------------------------------------------


def test_a_card_nobody_has_seen_becomes_wikipedia_rows_with_no_ufcstats_ids(db, wiki_page):
    page = wiki_page("UFC_323")

    result = write(db, page)

    assert result.event == UpsertCounts(inserted=1)
    assert result.fights == UpsertCounts(inserted=14)
    assert result.fighters.inserted == 28 and len(result.stubs) == 28
    event = one(db, "SELECT * FROM events")
    assert (event["wikipedia_article_id"], event["ufcstats_id"], event["name"]) == (
        80893887,
        None,
        "UFC 323: Dvalishvili vs. Yan 2",
    )
    assert event["event_date"] == page.event_date
    assert count(db, "fights WHERE ufcstats_id IS NULL AND result_source = 'wikipedia'") == 14
    assert count(db, "fighters WHERE ufcstats_id IS NULL AND wikipedia_title IS NOT NULL") == 28


def test_the_results_are_stored_as_facts_and_no_prose(db, wiki_page):
    write(db, wiki_page("UFC_323"))

    draw = one(
        db,
        "SELECT f.* FROM fights f JOIN fighters a ON a.id = f.fighter_a_id"
        " JOIN fighters b ON b.id = f.fighter_b_id"
        " WHERE a.name = 'Bogdan Guskov' OR b.name = 'Bogdan Guskov'",
    )
    assert (draw["outcome"], draw["winner_id"], draw["method"], draw["decision_type"]) == (
        "draw",
        None,
        "decision",
        "majority",
    )
    assert (draw["finish_round"], draw["finish_time_sec"], draw["is_title_fight"]) == (
        3,
        300,
        False,
    )
    assert (draw["status"], draw["has_round_stats"], draw["scheduled_rounds"]) == (
        "completed",
        False,
        None,
    )
    assert count(db, "fights WHERE method_detail IS NOT NULL OR referee IS NOT NULL") == 0
    assert count(db, "fights WHERE is_title_fight") == 2  # Yan-Dvalishvili and Van-Pantoja
    winner = one(
        db,
        "SELECT w.name FROM fights f JOIN fighters w ON w.id = f.winner_id WHERE f.is_title_fight"
        " AND f.method = 'decision'",
    )
    assert winner["name"] == "Petr Yan"


def test_a_rerun_writes_nothing_at_all(db, wiki_page):
    page = wiki_page("UFC_323")
    write(db, page)
    before = versions(db)

    again = write(db, page)

    assert again.event == UpsertCounts(unchanged=1)
    assert again.fights == UpsertCounts(unchanged=14)
    assert again.fighters == UpsertCounts() and again.stubs == []
    assert versions(db) == before  # not one row version was rewritten


# -- keys that survive edits -------------------------------------------------------------------


def test_renaming_the_article_creates_no_new_event_fighter_or_fight(db, wiki_page):
    page = wiki_page("UFC_323")
    write(db, page)
    renamed = page.model_copy(
        update={"name": "UFC 323: Dvalishvili vs. Yan II", "canonical_title": "UFC_323_(renamed)"}
    )

    result = write(db, renamed)

    assert (count(db, "events"), count(db, "fights"), count(db, "fighters")) == (1, 14, 28)
    assert result.event == UpsertCounts(updated=1)
    assert one(db, "SELECT name FROM events")["name"] == "UFC 323: Dvalishvili vs. Yan II"


def test_editing_a_display_name_keeps_the_same_fighter_and_fight(db, wiki_page):
    page = wiki_page("UFC_323")
    write(db, page)
    before = (count(db, "fighters"), count(db, "fights"))
    edited_bouts = []
    for bout in page.bouts:
        first = bout.first
        if first.link_title:  # the link target is the key, so the display text may change
            first = first.model_copy(update={"name": first.name + " Jr."})
        edited_bouts.append(bout.model_copy(update={"first": first}))
    assert any(b.first.name.endswith(" Jr.") for b in edited_bouts)

    result = write(db, page, edited_bouts)

    assert (count(db, "fighters"), count(db, "fights")) == before
    assert result.stubs == [] and result.fights == UpsertCounts(unchanged=14)


def test_a_fighter_with_no_article_link_has_nothing_stable_so_a_rename_is_held_for_review(
    db, wiki_page
):
    page = wiki_page("UFC_323")
    write(db, page)
    unlinked = next(
        b for b in page.bouts if b.first.link_title is None or b.second.link_title is None
    )
    side = "first" if unlinked.first.link_title is None else "second"
    person = getattr(unlinked, side)
    renamed = unlinked.model_copy(
        update={side: person.model_copy(update={"name": person.name + " Jr."})}
    )
    edited = [renamed if b is unlinked else b for b in page.bouts]

    with pytest.raises(MappingError, match=r"suspect duplicate fighter .*\(link None\)"):
        write(db, page, edited)


def test_a_new_spelling_with_a_new_link_is_held_for_review_and_adds_nothing(db, wiki_page):
    page = wiki_page("UFC_323")
    write(db, page)
    before = (count(db, "fighters"), count(db, "fights"), count(db, "events"))
    bout = page.bouts[0]
    respelled = bout.first.model_copy(
        update={"name": bout.first.name.replace("a", "e", 1), "link_title": "Some_Other_Link"}
    )
    edited = [bout.model_copy(update={"first": respelled}), *page.bouts[1:]]

    with pytest.raises(MappingError, match="suspect duplicate fighter.*Some_Other_Link"):
        write(db, page, edited)

    assert (count(db, "fighters"), count(db, "fights"), count(db, "events")) == before


# -- names -------------------------------------------------------------------------------------


def test_an_existing_fighter_is_found_by_name_and_remembers_the_link(db, wiki_page):
    page = wiki_page("UFC_323")
    ufcstats_card(db, page, page.bouts[:1])  # Petr Yan and Merab Dvalishvili exist, no wiki title
    result = write(db, page)

    yan = one(db, "SELECT ufcstats_id, wikipedia_title FROM fighters WHERE name = 'Petr Yan'")
    assert yan["ufcstats_id"] is not None and yan["wikipedia_title"] == "Petr_Yan"
    assert result.fighters.updated == 2 and result.fighters.inserted == 26
    assert count(db, "fighters WHERE name = 'Petr Yan'") == 1


def test_a_reviewed_alias_resolves_a_different_spelling(db, wiki_page):
    page = wiki_page("UFC_323")
    with db.begin() as conn:
        fighter_id = conn.execute(
            text(
                "INSERT INTO fighters (ufcstats_id, name) VALUES (:u, 'Piotr Yanov') RETURNING id"
            ),
            {"u": hexid()},
        ).scalar_one()
        conn.execute(
            text("INSERT INTO fighter_aliases (source, alias_norm, fighter_id) VALUES ('wikipedia',"
                 " 'petr yan', :f)"), {"f": fighter_id})  # fmt: skip

    write(db, page, page.bouts[:1])

    assert count(db, "fighters WHERE name = 'Petr Yan'") == 0  # no stub: the alias won
    assert (
        one(db, "SELECT wikipedia_title FROM fighters WHERE id = :i", i=fighter_id)[
            "wikipedia_title"
        ]
        == "Petr_Yan"
    )


def test_two_fighters_with_one_name_are_ambiguous_and_the_message_names_the_link(db, wiki_page):
    page = wiki_page("UFC_323")
    with db.begin() as conn:
        for _ in range(2):
            conn.execute(
                text("INSERT INTO fighters (ufcstats_id, name) VALUES (:u, 'Petr Yan')"),
                {"u": hexid()},
            )

    with pytest.raises(MappingError, match=r"ambiguous fighter 'Petr Yan' \(link 'Petr_Yan'\)"):
        write(db, page, page.bouts[:1])

    assert count(db, "events") == 0 and count(db, "fights") == 0


def test_a_fighter_confirmed_as_new_is_let_through_the_similar_name_check(
    db, wiki_page, monkeypatch
):
    page = wiki_page("UFC_323")
    with db.begin() as conn:  # an existing fighter whose name is one letter off a new one
        conn.execute(
            text("INSERT INTO fighters (ufcstats_id, name) VALUES (:u, 'Petr Yann')"),
            {"u": hexid()},
        )
    with pytest.raises(MappingError, match="suspect duplicate"):
        write(db, page, page.bouts[:1])

    monkeypatch.setattr(
        "cageops_worker.ingest.store_wikipedia.load_distinct_titles",
        lambda: frozenset({"Petr_Yan"}),
    )
    result = write(db, page, page.bouts[:1])

    assert result.fights.inserted == 1 and count(db, "fighters WHERE name LIKE 'Petr Yan%'") == 2


# -- precedence against ufcstats ---------------------------------------------------------------


def test_a_scheduled_ufcstats_bout_gets_the_wikipedia_result_and_keeps_its_ids(db, wiki_page):
    page = wiki_page("UFC_323")
    event_id, [fight_id] = ufcstats_card(db, page, page.bouts[:1])  # Yan vs Dvalishvili, scheduled
    ufcstats_event = one(db, "SELECT ufcstats_id FROM events")["ufcstats_id"]

    result = write(db, page)

    fight = one(db, "SELECT * FROM fights WHERE id = :i", i=fight_id)
    assert (fight["status"], fight["result_source"], fight["outcome"]) == (
        "completed",
        "wikipedia",
        "win",
    )
    assert fight["ufcstats_id"] is not None and fight["is_title_fight"] is True
    assert count(db, "events") == 1  # the article attached to the ufcstats event, no second event
    event = one(db, "SELECT * FROM events WHERE id = :i", i=event_id)
    assert (event["ufcstats_id"], event["wikipedia_article_id"]) == (ufcstats_event, 80893887)
    assert result.fights.updated == 1 and result.fights.inserted == 13  # the other 13 are new


def test_a_ufcstats_result_is_never_replaced_and_a_disagreement_is_reported(db, wiki_page):
    page = wiki_page("UFC_323")
    # ufcstats says the FIRST-named fighter won; Wikipedia says the same for Yan, so flip one.
    _, [fight_id] = ufcstats_card(db, page, page.bouts[:1], completed="second")
    before = one(db, "SELECT * FROM fights WHERE id = :i", i=fight_id)

    result = write(db, page, page.bouts[:1])

    assert one(db, "SELECT * FROM fights WHERE id = :i", i=fight_id) == before
    assert result.already_covered == 1 and result.skipped is True
    assert result.fights == UpsertCounts()
    [anomaly] = [a for a in result.anomalies if a.startswith("source_disagreement")]
    assert f"fight={fight_id}" in anomaly and "winner_id=" in anomaly


def test_a_card_ufcstats_already_covers_in_full_is_skipped(db, wiki_page):
    page = wiki_page("UFC_323")
    ufcstats_card(db, page, page.bouts, completed="first")

    result = write(db, page)

    assert result.skipped is True and result.already_covered == 14
    assert count(db, "fights WHERE result_source = 'wikipedia'") == 0
    assert count(db, "events") == 1


def test_only_the_uncovered_bouts_are_written_when_ufcstats_has_some(db, wiki_page):
    page = wiki_page("UFC_323")
    ufcstats_card(db, page, page.bouts[:5], completed="first")

    result = write(db, page)

    assert result.skipped is False and result.already_covered == 5
    assert result.fights.inserted == 9
    assert count(db, "fights WHERE result_source = 'ufcstats'") == 5
    assert count(db, "events") == 1  # the nine went into the ufcstats event


def test_a_wikipedia_result_that_changes_is_updated_and_reported(db, wiki_page):
    page = wiki_page("UFC_323")
    write(db, page)
    draw = next(b for b in page.bouts if b.versus == "vs.")
    flipped = draw.model_copy(update={"versus": "def.", "method_raw": "TKO (punches)"})
    edited = [flipped if b is draw else b for b in page.bouts]

    result = write(db, page, edited)

    assert result.fights == UpsertCounts(updated=1, unchanged=13)
    [anomaly] = [a for a in result.anomalies if a.startswith("result_changed")]
    assert "outcome=draw->win" in anomaly and "method=decision->ko_tko" in anomaly
    changed = one(
        db,
        "SELECT f.outcome, f.winner_id, f.decision_type FROM fights f"
        " JOIN fighters a ON a.id = f.fighter_a_id JOIN fighters b ON b.id = f.fighter_b_id"
        " WHERE a.name = :n OR b.name = :n",
        n=draw.second.name,
    )
    assert changed["outcome"] == "win" and changed["winner_id"] is not None
    assert changed["decision_type"] is None  # replaced as a unit, NULLs included


def test_a_cancelled_ufcstats_bout_is_left_alone_and_reported(db, wiki_page):
    page = wiki_page("UFC_323")
    _, [fight_id] = ufcstats_card(db, page, page.bouts[:1])
    with db.begin() as conn:
        conn.execute(text("UPDATE fights SET status = 'cancelled' WHERE id = :i"), {"i": fight_id})

    result = write(db, page, page.bouts[:1])

    assert one(db, "SELECT status, result_source FROM fights WHERE id = :i", i=fight_id) == {
        "status": "cancelled",
        "result_source": None,
    }
    assert f"wikipedia_result_for_cancelled_bout:fight={fight_id}" in result.anomalies


def test_a_ufcstats_scheduled_bout_wikipedia_does_not_list_is_reported_and_never_cancelled(
    db, wiki_page
):
    page = wiki_page("UFC_323")
    extra = page.bouts[5]  # on the card in ufcstats' view only
    _, fights = ufcstats_card(db, page, [page.bouts[0], extra])

    result = write(db, page, page.bouts[:1])

    scheduled = one(db, "SELECT count(*) AS n FROM fights WHERE status = 'scheduled'")["n"]
    assert scheduled == 1  # still scheduled, not cancelled
    assert f"ufcstats_scheduled_without_wikipedia_match:fight={fights[1]}" in result.anomalies


def test_a_ufcstats_event_a_day_off_is_still_the_same_event(db, wiki_page):
    page = wiki_page("UFC_323")
    ufcstats_card(db, page, page.bouts[:1], day=-1)  # ufcstats dates the card a day earlier

    write(db, page, page.bouts[:1])

    assert count(db, "events") == 1 and count(db, "fights") == 1
    assert one(db, "SELECT status FROM fights")["status"] == "completed"


def test_two_cards_on_one_date_are_told_apart_by_the_fighters(db, wiki_page):
    page = wiki_page("UFC_323")
    other_card = wiki_page("UFC_321")
    ufcstats_card(db, other_card, other_card.bouts[:2], day=0)  # some other card, same day
    event_b, _ = ufcstats_card(db, page, page.bouts[:1], day=0)

    write(db, page, page.bouts[:1])

    assert (
        one(db, "SELECT wikipedia_article_id FROM events WHERE id = :i", i=event_b)[
            "wikipedia_article_id"
        ]
        == 80893887
    )
    assert count(db, "events WHERE wikipedia_article_id IS NULL") == 1


def test_a_date_collision_with_an_unrelated_event_is_reported_not_merged(db, wiki_page):
    page = wiki_page("UFC_323")
    other = wiki_page("UFC_321")
    same_day = (page.event_date - other.event_date).days
    ufcstats_card(db, other, other.bouts[:1], day=same_day)  # an unrelated card, the same date

    result = write(db, page, page.bouts[:1])

    assert count(db, "events") == 2
    assert any(a.startswith("event_date_collision") for a in result.anomalies)


def test_the_wikipedia_writer_never_calls_the_ufcstats_scheduling_functions():
    import inspect

    from cageops_worker.ingest import store_wikipedia

    source = inspect.getsource(store_wikipedia)
    assert "write_scheduled_bouts" not in source and "reconcile_event_bouts" not in source
    assert "cancel_scheduled_for_event" not in source


def test_a_catch_weight_bout_reports_the_gender_it_assumed(db, wiki_page):
    page = wiki_page("UFC_332")
    catch = next(b for b in page.bouts if b.weight_class_raw.startswith("Catchweight"))

    result = write(db, page, [catch])

    assert "gender_assumed:Catch Weight" in result.anomalies
    assert one(db, "SELECT weight_class, gender FROM fights") == {
        "weight_class": "Catch Weight",
        "gender": "M",
    }


def test_the_module_still_imports_the_helpers_it_documents():
    assert mapping_wikipedia.map_bout and mapping_wikipedia.DuplicateIndex


def test_the_upsert_itself_refuses_to_touch_a_ufcstats_result_or_a_cancelled_bout(db, wiki_page):
    """The second guard: even if a result landed after we read, the write changes nothing."""
    from cageops_worker.ingest.store_wikipedia import upsert_result

    page = wiki_page("UFC_323")
    event_id, [done] = ufcstats_card(db, page, page.bouts[:1], completed="first")
    _, [cancelled] = ufcstats_card(db, page, page.bouts[1:2])
    with db.begin() as conn:
        conn.execute(text("UPDATE fights SET status = 'cancelled' WHERE id = :i"), {"i": cancelled})

    for fight_id in (done, cancelled):
        before = one(db, "SELECT * FROM fights WHERE id = :i", i=fight_id)
        row = {
            "event_id": before["event_id"], "fighter_a_id": before["fighter_a_id"],
            "fighter_b_id": before["fighter_b_id"], "outcome": "draw", "winner_id": None,
            "method": "decision", "decision_type": "majority", "method_detail": None,
            "finish_round": 3, "finish_time_sec": 300, "status": "completed",
            "result_source": "wikipedia", "is_title_fight": True, "weight_class": "Lightweight",
            "gender": "M", "has_round_stats": False,
        }  # fmt: skip
        with db.begin() as conn:
            counts = upsert_result(conn, row)

        assert counts == UpsertCounts(unchanged=1)
        assert one(db, "SELECT * FROM fights WHERE id = :i", i=fight_id) == before


def test_an_alias_row_in_the_csv_resolves_a_held_name_without_a_seed_rerun(
    db, wiki_page, tmp_path, monkeypatch
):
    """The workflow the error message promises: edit the alias file, then replay."""
    page = wiki_page("UFC_323")
    with db.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO fighters (ufcstats_id, name)"
                " VALUES ('c000000000000001', 'Piotr Yanov')"
            )
        )
    csv = tmp_path / "aliases.csv"
    csv.write_text(
        "source,alias,ufcstats_id,note\n"
        "wikipedia,Petr Yan,c000000000000001,reviewed\n"
        "mdabbert,Someone Else,ffffffffffffffff,another source: unknown here, must not matter\n"
    )
    monkeypatch.setattr("cageops_worker.ingest.store_wikipedia.ALIASES_CSV", csv)

    write(db, page, page.bouts[:1])

    assert count(db, "fighters WHERE name = 'Petr Yan'") == 0  # resolved to Piotr Yanov
    assert (
        one(db, "SELECT wikipedia_title FROM fighters WHERE ufcstats_id = 'c000000000000001'")[
            "wikipedia_title"
        ]
        == "Petr_Yan"
    )


def test_an_alias_row_for_wikipedia_that_points_nowhere_is_a_loud_error(
    db, wiki_page, tmp_path, monkeypatch
):
    csv = tmp_path / "aliases.csv"
    csv.write_text("source,alias,ufcstats_id,note\nwikipedia,Petr Yan,ffffffffffffffff,typo\n")
    monkeypatch.setattr("cageops_worker.ingest.store_wikipedia.ALIASES_CSV", csv)
    page = wiki_page("UFC_323")

    with pytest.raises(ValueError, match="points at unknown fighter"):
        write(db, page, page.bouts[:1])
