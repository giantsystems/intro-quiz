"""Artist-tag repair. The write path touches real files, so it's tested on real
(generated) MP3s rather than mocks — a tag write that silently no-ops would pass
any mock-based test and fail on the library.
"""
import os
import shutil
import subprocess
import tempfile

import pytest

from app import retag

pytest.importorskip("mutagen")
HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def make_mp3(path: str, artist: str, title: str = "Song", album_artist: str | None = None):
    """A one-second real MP3 with real ID3 tags."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                    "-i", "anullsrc=r=44100:cl=mono", "-t", "1", path], check=True)
    import mutagen
    a = mutagen.File(path, easy=True)
    a["artist"] = artist
    a["title"] = title
    if album_artist:
        a["albumartist"] = album_artist
    a.save()


# -- plan (pure, no files) -------------------------------------------------

def test_plan_prefers_album_artist_over_the_majority_vote():
    """A correct album_artist is direct evidence from the file itself; the
    majority vote is only a fallback guess.

    Note the evidence is weighed for the GROUP, not per file: one file vouching
    'AC/DC' via its album_artist carries the other four with it, even though
    'ACDC' has the most files. Deciding per file is what used to let one plan
    hold two targets at once — see
    test_plan_never_emits_both_directions_for_one_artist.
    """
    tracks = [
        {"path": "a", "artist": "AC, DC", "album_artist": "AC/DC"},
        {"path": "b", "artist": "ACDC", "album_artist": None},
        {"path": "c", "artist": "ACDC", "album_artist": None},
        {"path": "d", "artist": "ACDC", "album_artist": None},
        {"path": "e", "artist": "AC DC", "album_artist": None},
    ]
    changes = retag.plan(tracks)
    assert {c["target"] for c in changes} == {"AC/DC"}, "one spelling for the whole group"
    assert {c["why"] for c in changes} == {"album_artist"}
    assert len(changes) == 5, "every file moves to the vouched spelling"


def test_plan_never_emits_both_directions_for_one_artist():
    """The real Suede shape, and the bug it caused.

    11 files spelled 'suede' each carried album_artist='Suede'; the 2 files
    already spelled 'Suede' had none, so per-file logic sent them to a majority
    vote that 'suede' won on count. One run therefore wrote 11 files up to
    'Suede' AND 2 files back down to 'suede'. On the live library 13 of 180
    artists were self-contradictory like this and 45 files were written against
    their own plan ('AC/DC' -> 'Ac/Dc' among them).
    """
    tracks = [{"path": f"s{i}", "artist": "suede", "album_artist": "Suede"} for i in range(11)]
    tracks += [{"path": f"S{i}", "artist": "Suede", "album_artist": None} for i in range(2)]
    changes = retag.plan(tracks)
    assert {c["target"] for c in changes} == {"Suede"}, "one target for the whole group"
    assert len(changes) == 11, "only the 11 misspelled files move"


def test_plan_is_idempotent_on_a_mixed_group():
    """Applying a plan and re-planning must find nothing left. This is the
    property the per-file version broke: it converged only after a second pass,
    having churned files in the meantime."""
    tracks = [{"path": f"s{i}", "artist": "suede", "album_artist": "Suede"} for i in range(11)]
    tracks += [{"path": f"S{i}", "artist": "Suede", "album_artist": None} for i in range(2)]
    for c in retag.plan(tracks):                    # apply pass 1 in memory
        next(t for t in tracks if t["path"] == c["path"])["artist"] = c["target"]
    assert retag.plan(tracks) == [], "a second pass must have nothing to do"


def test_plan_counts_album_artist_votes_across_the_group():
    """When files disagree about the album artist too, the most-vouched spelling
    wins — not whichever file happens to be visited first."""
    tracks = [{"path": f"a{i}", "artist": "Beyonce", "album_artist": "Beyoncé"}
              for i in range(10)]
    tracks += [{"path": "b", "artist": "Beyonce", "album_artist": "Beyonce"}]
    changes = retag.plan(tracks)
    assert {c["target"] for c in changes} == {"Beyoncé"}, "10 votes beat 1"
    # all 11 files are spelled 'Beyonce', so all 11 move — including the one whose
    # own album_artist lost the vote
    assert len(changes) == 11


def test_plan_ignores_an_album_artist_for_a_different_act():
    """'Nothing Else Matters' on The Metallica Blacklist has artist=Phoebe
    Bridgers, album_artist=Metallica. Trusting album_artist there would retag
    every cover as Metallica — the worst outcome available to this script."""
    tracks = [{"path": "a", "artist": "Phoebe Bridgers", "album_artist": "Metallica"}]
    assert retag.plan(tracks) == [], "a compilation's album_artist is not the track artist"


def test_plan_leaves_a_single_spelling_alone():
    tracks = [{"path": "a", "artist": "Blondie", "album_artist": None},
              {"path": "b", "artist": "Blondie", "album_artist": None}]
    assert retag.plan(tracks) == []


def test_plan_majority_breaks_ties_on_the_longer_string():
    """'AC/DC' and 'ACDC' with one file each: prefer the punctuated form."""
    tracks = [{"path": "a", "artist": "AC/DC", "album_artist": None},
              {"path": "b", "artist": "ACDC", "album_artist": None}]
    changes = retag.plan(tracks)
    assert [c["target"] for c in changes] == ["AC/DC"]
    assert changes[0]["path"] == "b"


def test_plan_override_beats_everything():
    tracks = [{"path": "a", "artist": "AC, DC", "album_artist": "AC, DC"}]
    changes = retag.plan(tracks, overrides={"AC-DC": "AC/DC"})
    assert changes[0]["target"] == "AC/DC" and changes[0]["why"] == "override"


def test_plan_never_targets_the_spelling_it_already_has():
    tracks = [{"path": "a", "artist": "AC/DC", "album_artist": "AC/DC"},
              {"path": "b", "artist": "AC, DC", "album_artist": "AC/DC"}]
    assert [c["path"] for c in retag.plan(tracks)] == ["b"]


def test_plan_never_rewrites_an_article_only_difference():
    """The real library's dry run wanted 'The Beatles' -> 'Beatles' and
    'The Eagles' -> 'Eagles' (because the album_artist happened to omit it) while
    also pushing 'Stray Cats' -> 'The Stray Cats'. 206 of 811 changes were this,
    in both directions. artist_key already folds them, so the quiz sees one band
    either way and rewriting the files buys nothing.
    """
    tracks = [
        # album_artist drops the article — must NOT propagate
        {"path": "a", "artist": "The Beatles", "album_artist": "Beatles"},
        {"path": "b", "artist": "The Eagles", "album_artist": "Eagles"},
        # ...and the other direction
        {"path": "c", "artist": "Stray Cats", "album_artist": "The Stray Cats"},
        # majority disagreeing only by the article is also left alone
        {"path": "d", "artist": "Verve", "album_artist": None},
        {"path": "e", "artist": "The Verve", "album_artist": None},
        {"path": "f", "artist": "The Verve", "album_artist": None},
    ]
    assert retag.plan(tracks) == []


def test_plan_still_fixes_a_real_variant_on_an_articled_name():
    """Suppressing article churn must not suppress genuine fixes on bands whose
    name carries an article. 'The Bee-Gees' differs by punctuation, not by 'The',
    so it is still corrected — and the correction keeps the article."""
    tracks = [
        {"path": "a", "artist": "The Bee Gees", "album_artist": None},
        {"path": "b", "artist": "The Bee Gees", "album_artist": None},
        {"path": "c", "artist": "The Bee-Gees", "album_artist": "The Bee Gees"},
        # a case that differs by BOTH the article and punctuation is a real fix
        # too — dropping it would leave 'beegees' spelled three ways
        {"path": "d", "artist": "Bee.Gees", "album_artist": "The Bee Gees"},
    ]
    changes = {c["path"]: c["target"] for c in retag.plan(tracks)}
    assert changes == {"c": "The Bee Gees", "d": "The Bee Gees"}


def test_an_explicit_override_can_still_set_the_article():
    """The suppression is a default, not a prohibition — if you DO have an opinion
    about whether the name carries 'The', --map still applies it."""
    tracks = [{"path": "a", "artist": "Beatles", "album_artist": None}]
    changes = retag.plan(tracks, overrides={"Beatles": "The Beatles"})
    assert [(c["target"], c["why"]) for c in changes] == [("The Beatles", "override")]


def test_article_only_helper_is_not_fooled_by_punctuation_or_case():
    assert retag._article_only("The Corrs", "corrs")
    assert retag._article_only("A Perfect Circle", "Perfect Circle")
    # both have the article, or neither: not an article-only difference
    assert not retag._article_only("The Beatles", "The Beatless")
    assert not retag._article_only("Beatles", "Beatless")
    # a band whose name genuinely starts with a word that isn't an article
    assert not retag._article_only("Theory of a Deadman", "Deadman")


# -- run guards ------------------------------------------------------------

def test_run_refuses_without_music_dirs(monkeypatch):
    """This writes to the music library — it must never run on a default path."""
    monkeypatch.setattr(retag, "MUSIC_DIRS", [])
    with pytest.raises(retag.RetagError) as e:
        retag.run()
    assert "MUSIC_DIRS" in str(e.value)


def test_run_refuses_a_missing_root(monkeypatch):
    monkeypatch.setattr(retag, "MUSIC_DIRS", ["/nope/not/here"])
    with pytest.raises(retag.RetagError) as e:
        retag.run()
    assert "not found" in str(e.value)


# -- the real thing --------------------------------------------------------

@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg to generate test MP3s")
def test_dry_run_changes_no_tag_on_disk(monkeypatch, tmp_path):
    import mutagen
    root = tmp_path / "lib"; root.mkdir()
    make_mp3(str(root / "1.mp3"), "AC/DC", "Back In Black")
    make_mp3(str(root / "2.mp3"), "AC/DC", "Highway To Hell")
    make_mp3(str(root / "3.mp3"), "AC, DC", "Thunderstruck")
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))

    out = retag.run(write=False)
    assert out["dry_run"] is True and out["changes"] == 1 and out["written"] == 0
    assert out["plan"] == {"AC/DC": {"AC, DC (by majority)": 1}}
    assert mutagen.File(str(root / "3.mp3"), easy=True)["artist"] == ["AC, DC"]


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg to generate test MP3s")
def test_write_fixes_the_tag_and_leaves_the_path_alone(monkeypatch, tmp_path):
    """The path must not change: Navidrome ids tracks by path, so a rename
    re-ids every track and orphans its clips."""
    import mutagen
    root = tmp_path / "lib"; root.mkdir()
    make_mp3(str(root / "a.mp3"), "AC/DC", "Back In Black")
    make_mp3(str(root / "b.mp3"), "AC/DC", "Highway To Hell")
    bad = root / "AC+DC" / "c.mp3"
    bad.parent.mkdir()
    make_mp3(str(bad), "AC, DC", "Thunderstruck")
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))

    out = retag.run(write=True)
    assert out["written"] == 1 and out["dry_run"] is False
    assert mutagen.File(str(bad), easy=True)["artist"] == ["AC/DC"]
    assert bad.exists(), "the file must not be renamed or moved"
    assert bad.parent.name == "AC+DC", "the mangled FOLDER name is left alone"
    # the other two were already right and must be untouched
    assert mutagen.File(str(root / "a.mp3"), easy=True)["artist"] == ["AC/DC"]


def test_scan_raises_rather_than_returning_a_truncated_file_list():
    """An aborted scan must NOT return what it found so far.

    plan() over a truncated list looks exactly like a library that legitimately
    has no more fixes to make, so the job would report "0 changes" and a caller
    could reasonably conclude the tags were clean. Needs no ffmpeg: the cancel
    check is the first thing in the loop, before any file is opened.
    """
    from app import jobs
    jobs._CANCEL.set()
    try:
        with pytest.raises(jobs.JobAborted):
            retag.scan(["/nope/a.mp3", "/nope/b.mp3"], {})
    finally:
        jobs._CANCEL.clear()


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg to generate test MP3s")
def test_an_aborted_write_keeps_its_journal_so_a_rerun_resumes(monkeypatch, tmp_path):
    """Stopping the WRITE loop is a clean, resumable outcome — the opposite of the
    scan. Every file written is journalled and flushed, so the count is true and a
    rerun continues at the file it stopped on."""
    from app import jobs
    root = tmp_path / "lib"; root.mkdir()
    # The majority spelling IS the target, so the well-tagged files have to
    # outnumber the broken ones or plan() decides 'AC, DC' is correct and there is
    # nothing to write at all.
    for n in "abcde":
        make_mp3(str(root / f"{n}.mp3"), "AC/DC", f"Song {n}")
    for n in "wxyz":
        make_mp3(str(root / f"{n}.mp3"), "AC, DC", f"Song {n}")
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))

    # Hooked on _recache rather than _open_audio: the SCAN opens files too, so
    # counting opens would trip the cancel during the scan phase and raise instead
    # of exercising the write loop. _recache runs only on a successful write.
    real_recache = retag._recache
    written = []

    def counting_recache(cache, path, artist, album_artist):
        written.append(path)
        if len(written) == 2:
            jobs._CANCEL.set()      # abort after the second file is written
        return real_recache(cache, path, artist, album_artist)

    monkeypatch.setattr(retag, "_recache", counting_recache)
    try:
        out = retag.run(write=True)
        assert out["written"] == 2, "should have stopped part-way, not written all four"
        jobs._CANCEL.clear()
        # the rest happen on the next run, and the first two are not redone
        again = retag.run(write=True)
        assert again["written"] == 2
    finally:
        jobs._CANCEL.clear()
    import mutagen
    for n in "abcdewxyz":
        assert mutagen.File(str(root / f"{n}.mp3"), easy=True)["artist"] == ["AC/DC"]


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg to generate test MP3s")
def test_write_is_journalled_so_a_rerun_resumes(monkeypatch, tmp_path):
    root = tmp_path / "lib"; root.mkdir()
    make_mp3(str(root / "a.mp3"), "AC/DC", "One")
    make_mp3(str(root / "b.mp3"), "AC/DC", "Two")
    make_mp3(str(root / "c.mp3"), "AC, DC", "Three")
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))

    assert retag.run(write=True)["written"] == 1
    assert os.path.exists(retag._journal_path())
    # second pass: the tag is already right AND journalled — no work, no error
    assert retag.run(write=True)["written"] == 0


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg to generate test MP3s")
def test_the_journal_does_not_block_a_different_write_to_the_same_file(monkeypatch, tmp_path):
    """The journal keyed on path alone skipped any file it had ever written, even
    when the new plan wanted a DIFFERENT spelling — which is how a stale journal
    blocked 21 corrections on the live library. It must skip only the exact write
    it already performed."""
    import mutagen
    root = tmp_path / "lib"; root.mkdir()
    make_mp3(str(root / "a.mp3"), "AC/DC", "One")
    make_mp3(str(root / "b.mp3"), "AC/DC", "Two")
    make_mp3(str(root / "c.mp3"), "AC, DC", "Three")
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))

    assert retag.run(write=True)["written"] == 1
    assert mutagen.File(str(root / "c.mp3"), easy=True)["artist"] == ["AC/DC"]

    # c.mp3 is in the journal. Now an override wants a different spelling for it;
    # the journal must not veto that.
    out = retag.run(write=True, overrides={"AC/DC": "AC-DC"})
    assert out["written"] == 3, "all three files move to the overridden spelling"
    assert mutagen.File(str(root / "c.mp3"), easy=True)["artist"] == ["AC-DC"]


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg to generate test MP3s")
def test_a_write_run_converges_in_one_pass(monkeypatch, tmp_path):
    """End-to-end idempotence on real files: after one write run, a dry run must
    report nothing to do. The per-file planner needed two passes and churned
    files in between."""
    root = tmp_path / "lib"; root.mkdir()
    # NOT 's0.mp3' / 'S0.mp3' — macOS is case-insensitive, so those are one file
    for i in range(11):
        make_mp3(str(root / f"lower{i}.mp3"), "suede", f"Song {i}", album_artist="Suede")
    for i in range(2):
        make_mp3(str(root / f"upper{i}.mp3"), "Suede", f"Other {i}")
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))

    assert retag.run(write=True)["written"] == 11
    assert retag.run(write=False)["changes"] == 0, "one pass must be enough"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg to generate test MP3s")
def test_a_write_updates_the_cache_not_just_the_journal(monkeypatch, tmp_path):
    """Rewriting 'AC, DC' -> 'AC/DC' changes neither the file size nor the
    whole-second mtime, so the (mtime, size) cache key does NOT invalidate. If the
    write doesn't also refresh the cache entry, the cache keeps serving the old
    artist and every rerun re-plans the same writes — invisible while the journal
    survives, but a whole-library rewrite on a fresh container with empty /data.
    """
    root = tmp_path / "lib"; root.mkdir()
    make_mp3(str(root / "a.mp3"), "AC/DC", "One")
    make_mp3(str(root / "b.mp3"), "AC/DC", "Two")
    make_mp3(str(root / "c.mp3"), "AC, DC", "Three")
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))

    assert retag.run(write=True)["written"] == 1
    # drop the journal — the cache alone must now know the file is already right
    os.remove(retag._journal_path())
    assert retag.run(write=False)["changes"] == 0, \
        "cache still holds the pre-write artist; a rerun would rewrite the library"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg to generate test MP3s")
def test_scan_cache_survives_a_restart_and_invalidates_on_edit(monkeypatch, tmp_path):
    """The cache is what makes a 22k-file scan interruptible. It must also not
    go stale: an edited file has to be re-read, or a fix would be planned from
    the tag the file used to have."""
    import mutagen
    root = tmp_path / "lib"; root.mkdir()
    make_mp3(str(root / "a.mp3"), "AC/DC", "One")
    make_mp3(str(root / "b.mp3"), "AC, DC", "Two")
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))

    assert retag.run(write=False)["changes"] == 1
    cache = retag._load_cache()
    assert len(cache) == 2, "the scan must be cached for a restart to be cheap"

    # a genuinely cached second run reads nothing new and agrees
    assert retag.run(write=False)["changes"] == 1

    # now edit b.mp3 behind the cache's back: mtime+size change, so it re-reads
    a = mutagen.File(str(root / "b.mp3"), easy=True)
    a["artist"] = "AC/DC"
    a["title"] = "Two But Longer Title To Change The Size"
    a.save()
    os.utime(str(root / "b.mp3"), (0, 0))
    assert retag.run(write=False)["changes"] == 0, "stale cache entry was not invalidated"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg to generate test MP3s")
def test_only_filter_limits_the_writes(monkeypatch, tmp_path):
    import mutagen
    root = tmp_path / "lib"; root.mkdir()
    make_mp3(str(root / "a.mp3"), "AC/DC", "One")
    make_mp3(str(root / "b.mp3"), "AC/DC", "Two")
    make_mp3(str(root / "c.mp3"), "AC, DC", "Three")
    make_mp3(str(root / "d.mp3"), "Coldplay", "Yellow")
    make_mp3(str(root / "e.mp3"), "Coldplay", "Clocks")
    make_mp3(str(root / "f.mp3"), "coldplay", "Trouble")
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))

    assert retag.run(write=False)["changes"] == 2
    out = retag.run(write=True, only=["AC/DC"])
    assert out["written"] == 1
    assert mutagen.File(str(root / "f.mp3"), easy=True)["artist"] == ["coldplay"], \
        "--only must not touch other artists"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg to generate test MP3s")
def test_a_corrupt_file_does_not_abort_the_run(monkeypatch, tmp_path):
    root = tmp_path / "lib"; root.mkdir()
    make_mp3(str(root / "a.mp3"), "AC/DC", "One")
    make_mp3(str(root / "b.mp3"), "AC/DC", "Two")
    make_mp3(str(root / "c.mp3"), "AC, DC", "Three")
    (root / "junk.mp3").write_bytes(b"this is not an mp3")
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))

    out = retag.run(write=True)
    assert out["written"] == 1, "the corrupt file must be skipped, not fatal"


def test_empty_library_is_not_an_error(monkeypatch, tmp_path):
    root = tmp_path / "empty"; root.mkdir()
    monkeypatch.setattr(retag, "MUSIC_DIRS", [str(root)])
    monkeypatch.setattr(retag, "STATE_DIR", str(tmp_path))
    out = retag.run(write=True)
    assert out == {"files": 0, "changes": 0, "written": 0, "dry_run": False}
