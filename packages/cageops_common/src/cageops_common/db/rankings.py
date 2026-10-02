"""Constants behind the `fight_pre_rankings` view (migration 0004).

The view is plain SQL, so these values are written into the migration as literals. A test
checks that the view definition in the database still matches them. To change one, add a
new migration that recreates the view and update the constant in the same commit.
"""

# A ranking older than this (relative to the fight date) is considered stale: rank is NULL.
# Snapshots are weekly, and title changes show up 2-30 days after an event, so ranks can be
# stale but never early. 21 days tolerates a few missed weeks without trusting old lists.
RANKING_MAX_AGE_DAYS = 21

# When more than one source has a fresh snapshot, the first listed source wins.
RANKING_SOURCE_PRIORITY = ("jerzyszocik", "martj42")
