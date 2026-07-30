"""Family-score ingest + difficulty tier assignment."""

# Tier thresholds. Family play data is sparse (one Navidrome user scrobbles),
# so any repeat listening is a strong "the house knows this" signal, and
# Last.fm listeners carry the rest of the tiering.
FAMILY_KNOWN_PLAYS = 2
# A song this famous is an 'easy' whether or not the house has scrobbled it.
#
# 'easy' USED to mean family plays alone, and that made the tier reachable only by
# ingest_annotations() — which needs a hand-exported dump of Navidrome's own DB and is
# in no job or pipeline. On the live library play_count>0 and starred>0 were BOTH zero
# across all 23,083 rows, so assign_tiers put nothing in 'easy' at all... and the
# default game asks for tiers ['easy','medium']. Every game ever played has silently
# been medium-only, and the whole "start with songs the house knows" design was inert.
#
# 1M listeners is the ceiling of this library, not a guess: it selects 1,174 quizzable
# tracks (Smells Like Teen Spirit, Mr. Brightside, Billie Jean, Poker Face) and leaves
# medium a healthy 5,433. Family plays still win on their own, so this is a floor under
# 'easy' rather than a replacement — ingest the annotations and the tier only grows.
GLOBAL_VERY_WELL_KNOWN = 1_000_000
GLOBAL_WELL_KNOWN = 200_000   # listeners
GLOBAL_KNOWN = 30_000


def ingest_annotations(conn, rows: list[dict]) -> dict:
    """rows: [{id, play_count, starred}] aggregated across all Navidrome users."""
    conn.execute("UPDATE tracks SET play_count=0, starred=0")
    matched = 0
    for r in rows:
        matched += conn.execute(
            "UPDATE tracks SET play_count=?, starred=? WHERE id=?",
            (int(r.get("play_count") or 0), int(r.get("starred") or 0), r["id"])).rowcount
    conn.commit()
    return {"received": len(rows), "matched": matched}


def assign_tiers(conn) -> dict:
    """easy = the house knows it, or the whole world does; medium = the world knows it;
    hard = plausible deep cut; tiebreak = the rest. Only active tracks with a global
    score are tiered.

    'easy' has two independent routes in on purpose. Family plays are the better signal
    but depend on ingest_annotations(), which needs a hand-exported Navidrome dump — so
    on a library that has never had one, the listener floor is what keeps the tier from
    being empty. See GLOBAL_VERY_WELL_KNOWN for what that emptiness actually broke.
    """
    conn.execute("UPDATE tracks SET tier=NULL")
    conn.execute(
        "UPDATE tracks SET tier='easy' WHERE active=1 AND (play_count>=? OR starred>0 "
        "OR global_listeners>=?)",
        (FAMILY_KNOWN_PLAYS, GLOBAL_VERY_WELL_KNOWN))
    conn.execute(
        "UPDATE tracks SET tier='medium' WHERE active=1 AND tier IS NULL "
        "AND global_listeners>=?", (GLOBAL_WELL_KNOWN,))
    conn.execute(
        "UPDATE tracks SET tier='hard' WHERE active=1 AND tier IS NULL "
        "AND global_listeners>=?", (GLOBAL_KNOWN,))
    conn.execute(
        "UPDATE tracks SET tier='tiebreak' WHERE active=1 AND tier IS NULL "
        "AND global_listeners IS NOT NULL AND global_listeners>0")
    conn.commit()
    counts = {r["tier"]: r["c"] for r in conn.execute(
        "SELECT tier, COUNT(*) c FROM tracks WHERE tier IS NOT NULL GROUP BY tier")}
    return counts
