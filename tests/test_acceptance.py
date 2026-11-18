"""Acceptance criteria tested with generated audio and a synthetic transcript."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import wave

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cutdown import bwf, pipeline, state  # noqa: E402
from cutdown.config import Config  # noqa: E402
from cutdown.ingest import build_units, fill_speakers, run_ingest  # noqa: E402
from cutdown.render import check_exclusivity, check_no_admin, run_render  # noqa: E402
from cutdown.util import CutdownError, read_json, read_jsonl  # noqa: E402
from cutdown.wav import WavInfo  # noqa: E402

@pytest.fixture(scope="session")
def fixture_json(tmp_path_factory):
    path = tmp_path_factory.mktemp("recording") / "interview.json"
    turns = [
        (1.0, "HOST", "Tell me about"),
        (1.75, "GUEST", "that record."),
        (3.5, "GUEST", "I kept a copy."),
        (5.0, "HOST", "Where did you find it?"),
        (8.0, "GUEST", "It came from a corner shop."),
        (11.0, "HOST", "What happened next?"),
        (15.0, "GUEST", "We played it every night."),
        (18.0, "HOST", "Thanks for the story."),
    ]
    words = []
    for offset, speaker, line in turns:
        for index, word in enumerate(line.split()):
            start = offset + index * 0.25
            words.append({"word": word, "start": start, "end": start + 0.125,
                          "speaker": speaker})
    path.write_text(json.dumps({"word_segments": words}))
    return path


@pytest.fixture(scope="session")
def fixture_wav(fixture_json):
    path = fixture_json.with_suffix(".wav")
    with wave.open(str(path), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        samples = b"".join(i.to_bytes(2, "little", signed=True) for i in range(-400, 400))
        audio.writeframes(samples * 210)
    return path


@pytest.fixture(scope="session")
def build(tmp_path_factory, fixture_json):
    d = tmp_path_factory.mktemp("build")
    run_ingest(fixture_json, d, guest="Alex Vale")
    return d


@pytest.fixture(scope="session")
def build_norepair(tmp_path_factory, fixture_json):
    d = tmp_path_factory.mktemp("build_norepair")
    stats = run_ingest(fixture_json, d, guest="Alex Vale", repair=False)
    return d, stats


# --- 1. ingest reproduces the documented counts -----------------------------

def test_ingest_reproduces_documented_counts(build_norepair):
    _, s = build_norepair
    assert s["words"] == 32
    assert s["units"] == 8
    assert s["guest_label"] == "GUEST"
    assert s["guest_units"] == 4
    assert s["host_units"] == 4
    assert s["speech_starts"] == pytest.approx(1.0)


def test_every_word_is_timestamped(fixture_json):
    words = json.loads(fixture_json.read_text())["word_segments"]
    assert len(words) == 32
    assert all(w["start"] < w["end"] for w in words)


def test_boundary_repair_logs_the_known_late_labels(build):
    repairs = read_json(build / "repairs.json")
    assert len(repairs) == 1
    repair = repairs[0]
    assert repair["after"] == "Tell me about"
    assert repair["text"] == "that record."
    assert (repair["from"], repair["to"]) == ("GUEST", "HOST")


def test_repair_only_merges_across_short_gaps(build):
    for r in read_json(build / "repairs.json"):
        assert r["gap"] < 1.20, f"repaired across a {r['gap']}s pause"


def test_repair_is_disableable(build, build_norepair):
    assert len(read_jsonl(build / "units.jsonl")) < \
        len(read_jsonl(build_norepair[0] / "units.jsonl"))
    assert not (build_norepair[0] / "repairs.json").exists()


def test_timeline_is_never_model_facing(build):
    txt = (build / "units.txt").read_text()
    tl = read_json(build / "timeline.json")
    first = tl["S0001"]
    assert str(first["cut_in"]) not in txt
    assert str(first["start"]) not in txt


# --- 2. resumability ---------------------------------------------------------

def test_stage_state_makes_ingest_current(build, tmp_path, fixture_json):
    cfg = Config(Config.load().data)
    p = pipeline.Paths(build=build, outdir=tmp_path)
    io = {"whisper_json": fixture_json}
    assert not pipeline.is_current("ingest", p, cfg, **io)
    pipeline.mark_done("ingest", p, cfg, **io)
    assert pipeline.is_current("ingest", p, cfg, **io)
    # touching an output invalidates it
    (build / "units.jsonl").touch()
    assert not pipeline.is_current("ingest", p, cfg, **io)
    pipeline.mark_done("ingest", p, cfg, **io)
    # so does a config change
    cfg.data["ingest"]["gap_split"] = 9.9
    assert not pipeline.is_current("ingest", p, cfg, **io)


def test_reloading_context_does_not_invalidate_downstream_stages(tmp_path):
    """Regression: load_context used to rewrite context.json every invocation.

    Stages declare it as an input, so the bumped mtime made every model stage
    look stale and `run` re-ran them - the exact opposite of resumable.
    """
    from cutdown import context as ctxmod

    build = tmp_path / "build"
    build.mkdir()
    (build / "labels.jsonl").write_text("")
    p = pipeline.Paths(build=build, outdir=tmp_path)
    cfg = Config.load()

    pipeline.load_context(p, overrides={"guest": "Alex Vale"})
    pipeline.mark_done("label", p, cfg)
    assert pipeline.is_current("label", p, cfg)

    for _ in range(3):
        pipeline.load_context(p, overrides={"guest": "Alex Vale"})
        assert pipeline.is_current("label", p, cfg), \
            "reloading identical context invalidated a completed stage"

    # a genuine change must still invalidate
    pipeline.load_context(p, overrides={"guest": "Someone Else"})
    assert not pipeline.is_current("label", p, cfg)
    assert ctxmod.load(build).guest == "Someone Else"


def test_force_clears_state(tmp_path):
    state.record(tmp_path, "label", inputs=[], outputs=[], config_hash="x")
    assert state.state_path(tmp_path, "label").exists()
    state.clear(tmp_path, "label")
    assert not state.state_path(tmp_path, "label").exists()


def test_transcribe_prompts_for_an_omitted_audio_path(monkeypatch):
    from types import SimpleNamespace
    from cutdown import cli

    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr("builtins.input", lambda _: "~/interview.wav")
    assert cli._audio_path(None) == pathlib.Path("~/interview.wav").expanduser()


def test_transcribe_requires_an_audio_path_when_not_interactive(monkeypatch):
    from types import SimpleNamespace
    from cutdown import cli

    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(CutdownError, match="audio path required"):
        cli._audio_path(None)


def _fake_ffmpeg(path, ok: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + ("echo 'ffmpeg version 7'\nexit 0\n" if ok else
                    "echo 'dyld: Library not loaded: libx265.216.dylib' >&2\nexit 134\n"))
    path.chmod(0o755)
    return path


def test_ffmpeg_resolver_skips_a_binary_that_aborts_in_dyld(tmp_path, monkeypatch):
    from cutdown import media

    broken = _fake_ffmpeg(tmp_path / "brew" / "ffmpeg", ok=False)
    good = _fake_ffmpeg(tmp_path / "keg" / "ffmpeg", ok=True)
    monkeypatch.setenv("PATH", str(broken.parent))
    monkeypatch.delenv("CUTDOWN_FFMPEG", raising=False)
    monkeypatch.setattr(media, "KNOWN", [str(good)])
    media.find_ffmpeg.cache_clear()
    try:
        assert media.find_ffmpeg() == str(good)
        env = media.tool_env()
        assert env["PATH"].split(os.pathsep)[0] == str(good.parent)
    finally:
        media.find_ffmpeg.cache_clear()


def test_ffmpeg_resolver_falls_back_to_the_bundled_binary(tmp_path, monkeypatch):
    from cutdown import media

    broken = _fake_ffmpeg(tmp_path / "brew" / "ffmpeg", ok=False)
    bundled = _fake_ffmpeg(tmp_path / "wheel" / "ffmpeg-macos-aarch64-v7.1", ok=True)
    monkeypatch.setenv("PATH", str(broken.parent))
    monkeypatch.delenv("CUTDOWN_FFMPEG", raising=False)
    monkeypatch.setattr(media, "KNOWN", [])
    monkeypatch.setattr(media, "_bundled", lambda: str(bundled))
    monkeypatch.setattr(media, "SHIM_DIR", tmp_path / "shim")
    media.find_ffmpeg.cache_clear()
    try:
        assert media.find_ffmpeg() == str(bundled)
        env = media.tool_env()
        first = pathlib.Path(env["PATH"].split(os.pathsep)[0])
        assert first == tmp_path / "shim"
        assert os.readlink(first / "ffmpeg") == str(bundled)
    finally:
        media.find_ffmpeg.cache_clear()


def test_ffmpeg_resolver_names_every_failure(tmp_path, monkeypatch):
    from cutdown import media

    broken = _fake_ffmpeg(tmp_path / "brew" / "ffmpeg", ok=False)
    monkeypatch.setenv("PATH", str(broken.parent))
    monkeypatch.delenv("CUTDOWN_FFMPEG", raising=False)
    monkeypatch.setattr(media, "KNOWN", [])
    monkeypatch.setattr(media, "_bundled", lambda: None)
    media.find_ffmpeg.cache_clear()
    try:
        with pytest.raises(CutdownError, match="libx265"):
            media.find_ffmpeg()
    finally:
        media.find_ffmpeg.cache_clear()


def test_render_decodes_a_compressed_master_to_pcm(tmp_path):
    """A FLAC master is decoded once to a WAV the byte cutter can slice."""
    import subprocess
    from cutdown import media
    from cutdown.render import readable_master

    exe = media.find_ffmpeg()
    flac = tmp_path / "master.flac"
    subprocess.run([exe, "-v", "error", "-f", "lavfi", "-i",
                    "sine=frequency=440:sample_rate=48000:duration=1",
                    "-ac", "2", "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
                    str(flac)], check=True)
    path, info = readable_master(flac, tmp_path / "build")
    assert path == tmp_path / "build" / "master.wav"
    assert (info.sample_rate, info.channels, info.bits_per_sample) == (48000, 2, 24)
    assert info.total_frames == 48000


def test_transcribe_recovers_a_completed_json_after_an_interrupted_exit(tmp_path):
    """A finished WhisperX artifact is enough to resume; state is optional."""
    from cutdown.transcribe import run_transcribe

    audio = tmp_path / "interview.wav"
    audio.write_bytes(b"audio")
    out = tmp_path / "transcript"
    out.mkdir()
    expected = out / "interview.json"
    expected.write_text('{"word_segments": []}')

    assert run_transcribe(audio, out, {"diarize": True}) == expected


def test_assemble_restarts_through_the_fallback_when_primary_fails(tmp_path, monkeypatch):
    """The fallback must restart assembly, not mix two models in one edit."""
    from cutdown import assemble

    class FakeProvider:
        def __init__(self, name):
            self.name = name

        def describe(self):
            return self.name

    calls = []

    def fake_run(*args, **kwargs):
        calls.append((kwargs["provider"].name, kwargs["model"], kwargs["run_dir"]))
        if len(calls) == 1:
            raise CutdownError("Primary unavailable")
        return {"provider": f"{kwargs['provider'].name}/{kwargs['model']}"}

    monkeypatch.setattr(assemble, "get_provider",
                        lambda config, stage: (FakeProvider("primary"), "main-model"))
    monkeypatch.setattr(assemble, "get_named_provider",
                        lambda config, name: FakeProvider(name))
    monkeypatch.setattr(assemble, "_run_assemble", fake_run)

    cfg = Config.load()
    cfg.overlay({"stages": {"assemble": {"fallback_provider": "openrouter",
                                         "fallback_model": "backup-model"}}})
    fb_name = cfg.get("stages.assemble.fallback_provider")
    fallback = cfg.get("stages.assemble.fallback_model")
    result = assemble.run_assemble(tmp_path, cfg, None, None,
                                   run_dir=tmp_path / "runs")
    assert result["provider"] == f"{fb_name}/{fallback}"
    assert calls == [
        ("primary", "main-model", tmp_path / "runs"),
        (fb_name, fallback, tmp_path / "runs" / f"fallback-{fb_name}"),
    ]


# --- 3. dry-run makes no network calls ---------------------------------------

def test_dry_run_writes_prompts_and_sends_nothing(build, tmp_path, monkeypatch):
    import urllib.request

    def boom(*a, **k):
        raise AssertionError("--dry-run made a network call")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)

    from cutdown.context import EpisodeContext
    from cutdown.label import run_label

    run_dir = tmp_path / "runs"
    run_label(build, Config.load(), EpisodeContext(guest="Alex Vale"),
              run_dir=run_dir, dry_run=True)
    user = (run_dir / "label.w01.user.txt").read_text()
    system = (run_dir / "label.w01.system.txt").read_text()
    assert "S0001" in user and "S0006" in user
    assert "admin" in system
    assert (run_dir / "label.w01.schema.json").exists()


# --- 5. exclusivity ----------------------------------------------------------

def test_exclusivity_is_asserted_not_assumed():
    doc = {"segments": [{"id": "seg01", "order": ["S0001", "S0002"]},
                        {"id": "seg02", "order": ["S0002"]}]}
    with pytest.raises(CutdownError, match="both seg01 and seg02"):
        check_exclusivity(doc)


def test_exclusivity_passes_when_disjoint():
    check_exclusivity({"segments": [{"id": "a", "order": ["S1"]},
                                    {"id": "b", "order": ["S2"]}]})


# --- 6. no admin unit reaches a clip -----------------------------------------

def test_admin_units_are_refused(tmp_path):
    labels = tmp_path / "labels.jsonl"
    labels.write_text(
        '{"id": "S0001", "role": "admin", "topics": [], "guest_led": false}\n'
        '{"id": "S0002", "role": "payoff", "topics": [], "guest_led": true}\n')
    with pytest.raises(CutdownError, match="admin-tagged"):
        check_no_admin({"segments": [{"id": "seg01", "order": ["S0001"]}]}, labels)
    check_no_admin({"segments": [{"id": "seg01", "order": ["S0002"]}]}, labels)


def test_unlabelled_units_are_refused(tmp_path):
    labels = tmp_path / "labels.jsonl"
    labels.write_text('{"id": "S0003", "role": "unlabelled", "topics": [], '
                      '"guest_led": false}\n')
    with pytest.raises(CutdownError, match="never got a label"):
        check_no_admin({"segments": [{"id": "seg01", "order": ["S0003"]}]}, labels)


# --- 7 & 8. render: BWF stamps and sample-exact durations --------------------

@pytest.fixture(scope="session")
def rendered(build, tmp_path_factory, fixture_wav):
    tl = read_json(build / "timeline.json")
    ids = ["S0002", "S0004", "S0006"]
    (build / "segments.json").write_text(json.dumps({
        "guest": "Alex Vale", "episode": "interview",
        "segments": [{
            "id": "seg01",
            "anchor": {"artist": "The Lanterns", "title": "Paper Moon",
                       "kind": "guest_pick"},
            "target_sec": [30, 600], "ids": ids, "order": ids,
            "rejected": [{"id": "S0001", "why": "belongs with the own-work segment"}],
            "reason": "fixture",
        }],
    }))
    out = tmp_path_factory.mktemp("out")
    summary = run_render(build, out, fixture_wav, skip_admin_check=True, chunks=False)
    return out, summary, tl, ids


def test_clip_durations_match_declared_within_one_sample(rendered):
    out, summary, tl, ids = rendered
    assert summary["max_sample_delta"] <= 1
    import csv
    rows = list(csv.DictReader(open(out / "manifest.csv")))
    for r in rows:
        declared = sum(tl[u]["dur_cut"] for u in r["unit_ids"].split())
        assert abs(int(r["frames"]) - round(declared * summary["sample_rate"])) <= 1


def test_every_clip_carries_its_master_position(rendered):
    out, summary, tl, ids = rendered
    import csv
    for r in csv.DictReader(open(out / "manifest.csv")):
        got = bwf.verify(out / r["file"], expected_time_reference=int(r["time_reference"]))
        assert got["time_reference"] == int(r["in_samples"])
        assert got["time_reference"] / summary["sample_rate"] == \
            pytest.approx(float(r["in_sec"]), abs=1 / summary["sample_rate"])


def test_clip_audio_is_a_byte_copy_of_the_master(rendered, fixture_wav):
    out, summary, tl, ids = rendered
    master = WavInfo(fixture_wav)
    import csv
    for r in csv.DictReader(open(out / "manifest.csv")):
        clip = WavInfo(out / r["file"])
        assert clip.sample_rate == master.sample_rate
        assert clip.bits_per_sample == master.bits_per_sample
        assert clip.channels == master.channels
        assert clip.read_frames(0, clip.total_frames) == \
            master.read_frames(int(r["in_samples"]), int(r["frames"]))


def test_stringout_is_clips_plus_silence(rendered):
    out, summary, tl, ids = rendered
    stringout = next(s["stringout"] for s in summary["segments"] if s["id"] == "seg01")
    assert stringout == "1-final/seg01_paper-moon.wav", \
        "the stringout name should carry the anchor title"
    so = WavInfo(out / stringout)
    import csv
    rows = list(csv.DictReader(open(out / "manifest.csv")))
    gap = round(0.5 * summary["sample_rate"])
    expect = sum(int(r["frames"]) for r in rows) + gap * (len(rows) - 1)
    assert so.total_frames == expect


def test_clip_names_carry_their_context():
    from cutdown.render import _name, slugify

    assert slugify("The Art of Storytellin' (Pt. 1)") == "the-art-of-storytellin-pt-1"
    assert slugify("Beyoncé — Déjà Vu") == "beyonce-deja-vu"
    assert slugify("") == ""
    assert "/" not in slugify("AC/DC") and slugify("AC/DC") == "acdc"

    tokens = {"seg": "seg01", "title": "paper-moon", "artist": "the-lanterns",
              "kind": "guest_pick", "episode": "interview", "guest": "alex-vale"}
    assert _name("{seg}_{clip:02d}_{title}_{ids}", clip=1, ids="S0073",
                 first_id="S0073", last_id="S0073", **tokens) == \
        "seg01_01_paper-moon_S0073"
    assert _name("{seg}_{clip:02d}_{artist}_{title}_{ids}", clip=12,
                 ids="S0081-S0084", first_id="S0081", last_id="S0084", **tokens) == \
        "seg01_12_the-lanterns_paper-moon_S0081-S0084"

    # an empty token collapses rather than leaving a gap
    tokens["title"] = ""
    assert _name("{seg}_{clip:02d}_{title}_{ids}", clip=3, ids="S0001",
                 first_id="S0001", last_id="S0001", **tokens) == "seg01_03_S0001"


def test_a_bad_filename_template_names_the_available_tokens():
    from cutdown.render import _name
    with pytest.raises(CutdownError, match="Available tokens"):
        _name("{nonsense}", seg="seg01", clip=1, ids="S1")


def test_render_paths_cannot_replace_source_folders(tmp_path):
    from cutdown.render import _dirs, _name

    source = tmp_path / "audio" / "master.wav"
    with pytest.raises(CutdownError, match="overlaps"):
        _dirs(tmp_path, {"final": "audio"}, source)
    with pytest.raises(CutdownError, match="single path components"):
        _dirs(tmp_path, {"final": "../output"}, source)
    with pytest.raises(CutdownError, match="single path components"):
        _name("{seg}", seg="../segment")
    (tmp_path / "1-final").symlink_to(tmp_path / "audio", target_is_directory=True)
    with pytest.raises(CutdownError, match="symlink"):
        _dirs(tmp_path, None, source)


def test_a_trim_inside_a_run_splits_the_run():
    """Regression: a trim on a unit that is neither first nor last in a
    contiguous run was counted in the manifest but never applied to the audio,
    because the clip spans first.cut_in -> last.cut_out. Left a 0.83s
    discrepancy between the clip and what it claimed to contain.
    """
    from cutdown.render import _contiguous_runs

    timeline = {
        "S1": {"cut_in": 0.0, "cut_out": 10.0, "dur_cut": 10.0},
        "S2": {"cut_in": 10.0, "cut_out": 20.0, "dur_cut": 10.0},
        "S3": {"cut_in": 20.0, "cut_out": 30.0, "dur_cut": 10.0},
    }
    order = ["S1", "S2", "S3"]

    # untrimmed: one clip
    assert _contiguous_runs(order, lambda u: timeline[u]) == [["S1", "S2", "S3"]]

    # S1 trimmed at the tail: the run must split so the trim lands on an edge
    eff = dict(timeline)
    eff["S1"] = {"cut_in": 0.0, "cut_out": 9.2, "dur_cut": 9.2}
    assert _contiguous_runs(order, lambda u: eff[u]) == [["S1"], ["S2", "S3"]]

    # S2 trimmed at the head: split before it
    eff2 = dict(timeline)
    eff2["S2"] = {"cut_in": 10.8, "cut_out": 20.0, "dur_cut": 9.2}
    assert _contiguous_runs(order, lambda u: eff2[u]) == [["S1"], ["S2", "S3"]]


def test_render_refuses_a_clip_that_disagrees_with_its_units(build, tmp_path,
                                                             monkeypatch, fixture_wav):
    """The drift guard must be fatal, not cosmetic."""
    from cutdown import render as rmod

    ids = ["S0002", "S0004"]
    (tmp_path / "segments.json").write_text(json.dumps({
        "segments": [{"id": "seg01", "anchor": {}, "target_sec": [0, 999],
                      "ids": ids, "order": ids}]}))
    for f in ("timeline.json", "units.jsonl", "unit_words.json"):
        (tmp_path / f).write_bytes((build / f).read_bytes())

    # Force a run to merge two units that are not adjacent in the master.
    def invalid_plan(order, kept, timeline):
        return [{"in": timeline[order[0]]["cut_in"],
                 "out": timeline[order[-1]]["cut_out"],
                 "units": list(order), "join": "start"}]
    monkeypatch.setattr(rmod.spans, "clips", invalid_plan)
    with pytest.raises(CutdownError, match="samples unaccounted for"):
        rmod.run_render(tmp_path, tmp_path / "out", fixture_wav,
                        skip_admin_check=True)


def test_rejected_units_are_recorded(rendered):
    out, *_ = rendered
    import csv
    rows = list(csv.DictReader(open(out / "rejected.csv")))
    assert any(r["unit_id"] == "S0001" and "own-work" in r["why"] for r in rows)
    assert all(r["text"] for r in rows), "rejected rows should carry the text"


# --- cuts land in silence ----------------------------------------------------

def test_cut_points_sit_in_measured_gaps(build):
    tl = read_json(build / "timeline.json")
    units = read_jsonl(build / "units.jsonl")
    for u in units:
        t = tl[u["id"]]
        assert t["cut_in"] <= u["start"], f"{u['id']} cuts into its first word"
        assert t["cut_out"] >= u["end"], f"{u['id']} cuts into its last word"
    # and never overlap each other
    for a, b in zip(units, units[1:]):
        assert tl[a["id"]]["cut_out"] <= tl[b["id"]]["cut_in"] + 1e-6


# --- provider guard ----------------------------------------------------------

def test_unlisted_provider_is_refused_before_sending():
    cfg = Config.load()
    cfg.data["allowed_providers"] = ["openrouter"]
    with pytest.raises(CutdownError, match="not in allowed_providers"):
        cfg.check_allowed("zengo")


def test_schema_validation_names_the_offending_path():
    from cutdown.schema import ValidationError, validate
    from cutdown.label import SCHEMA
    with pytest.raises(ValidationError) as e:
        validate({"labels": [{"id": "S1", "topics": [], "role": "nope",
                              "guest_led": False}]}, SCHEMA)
    assert "labels[0].role" in str(e.value)


def test_json_is_extracted_from_a_fenced_reply():
    from cutdown.providers import extract_json
    assert extract_json('sure!\n```json\n{"a": 1}\n```\nhope that helps') == {"a": 1}
    assert extract_json('{"a": 2}') == {"a": 2}


# --- notes mode --------------------------------------------------------------
#
# The second mode: clips already exported from a finished edit, read back as a
# document someone can share. Its load-bearing rules are that the transcript is
# verbatim, and that every timestamp on the page was measured here rather than
# written by a model.

def _script(lines):
    """Lines of (speaker, start, text) -> WhisperX-shaped word segments."""
    words = []
    for spk, start, text in lines:
        t = start
        for w in text.split():
            words.append({"word": w, "start": round(t, 3), "end": round(t + 0.28, 3),
                          "speaker": spk})
            t += 0.32
    return words


CLIP_LINES = [
    ("SPEAKER_01", 0.5, "So tell me about the record that you picked out for us."),
    ("SPEAKER_00", 4.0, "We were in the studio at four o'clock in the morning, right."),
    ("SPEAKER_00", 12.0, "And Kenny would not let me leave until I had done twelve takes."),
    ("SPEAKER_01", 20.0, "Twelve?"),
]


@pytest.fixture
def clip(tmp_path):
    """A read clip: a WhisperX JSON and a WAV carrying a BWF stamp."""
    from cutdown.notes import read_clip

    wj = tmp_path / "seg01.json"
    wj.write_text(json.dumps({"word_segments": _script(CLIP_LINES)}))
    wav = tmp_path / "seg01.wav"
    _write_silence(wav, seconds=25, master_sec=584.928)
    return read_clip(wav, wj, gap=1.6, max_sec=40.0)


def _write_silence(path, *, seconds=10, rate=48000, master_sec=None):
    import struct
    from cutdown import bwf
    from cutdown.wav import write_wav

    class Fmt:
        fmt_raw = struct.pack("<HHIIHH", 1, 1, rate, rate * 2, 2, 16)

    extra = []
    if master_sec is not None:
        extra = [(b"bext", bwf.build_bext(time_reference=int(master_sec * rate)))]
    write_wav(path, Fmt(), b"\x00\x00" * rate * seconds, extra_chunks=extra)
    return path


def test_notes_paragraphs_break_on_speaker_and_pause(clip):
    ids = [p["id"] for p in clip["paragraphs"]]
    assert ids == [f"P{i:02d}" for i in range(1, len(ids) + 1)]
    # a speaker change always breaks; the two SPEAKER_00 lines break on the pause
    assert [p["speaker"] for p in clip["paragraphs"]] == ["A", "B", "B", "A"]
    assert clip["paragraphs"][0]["text"].startswith("So tell me")
    assert clip["duration"] == 25.0          # from the WAV, not the transcript
    assert clip["master_sec"] == pytest.approx(584.928, abs=0.001)


def test_notes_transcript_is_verbatim(clip):
    """Nothing is rewritten: every word of the source is in the paragraphs."""
    said = " ".join(w["word"] for w in _script(CLIP_LINES))
    assert " ".join(p["text"] for p in clip["paragraphs"]) == said


def test_notes_drops_a_quote_that_is_not_in_the_transcript(clip):
    from cutdown.notes import resolve_summary

    real = "Kenny would not let me leave"
    out = resolve_summary({
        "headline": "h", "summary": "s",
        "anecdotes": [{"title": "t", "what": "w", "ids": ["P02", "P03"]}],
        "quotes": [{"id": "P03", "text": real},
                   {"id": "P03", "text": "I wept openly for several hours"}],
    }, clip)
    assert [q["text"] for q in out["quotes"]] == [real]
    assert out["dropped"]["quotes"] == 1
    # and it is timed from the words it is made of, not from its paragraph:
    # P03 opens "And Kenny would not…", so the quote starts one word in.
    q = out["quotes"][0]
    p3 = next(p for p in clip["paragraphs"] if p["id"] == "P03")
    assert p3["start"] < q["start"] < q["end"] < p3["end"]


def test_notes_finds_a_quote_filed_under_the_wrong_paragraph(clip):
    from cutdown.notes import resolve_summary
    out = resolve_summary({
        "headline": "h", "summary": "s", "anecdotes": [],
        "quotes": [{"id": "P01", "text": "twelve takes"}],
    }, clip)
    assert out["quotes"] and out["quotes"][0]["id"] == "P03"
    assert out["dropped"]["quotes"] == 0


def test_notes_timestamps_come_from_the_audio_not_the_model(clip):
    from cutdown.notes import resolve_summary
    out = resolve_summary({
        "headline": "h", "summary": "s",
        "anecdotes": [{"title": "t", "what": "w", "ids": ["P02", "P03", "P99"]},
                      {"title": "invented", "what": "w", "ids": ["P77"]}],
    }, clip)
    assert len(out["anecdotes"]) == 1, "an anecdote with no real IDs must be dropped"
    a = out["anecdotes"][0]
    paras = {p["id"]: p for p in clip["paragraphs"]}
    assert a["ids"] == ["P02", "P03"]
    assert a["start"] == paras["P02"]["start"]
    assert a["end"] == paras["P03"]["end"]
    assert out["dropped"] == {"anecdotes": 1, "ids": 2, "quotes": 0}


def test_notes_names_speakers_from_the_reading_then_from_the_volume(clip):
    from cutdown.notes import name_speakers, resolve_summary

    # No summary: the one who says the most is the one being interviewed.
    assert name_speakers(clip, "Alex Vale", "Morgan Reed") == \
        {"A": "Morgan Reed", "B": "Alex Vale"}
    clip["summary"] = resolve_summary(
        {"headline": "h", "summary": "s", "anecdotes": [],
         "speakers": {"A": "host", "B": "guest"}}, clip)
    assert name_speakers(clip, "Alex Vale", "Morgan Reed")["B"] == "Alex Vale"


def test_notes_orders_clips_by_their_bwf_stamps():
    from cutdown.notes import order_clips
    clips = [{"stem": "a-late", "master_sec": 900.0},
             {"stem": "b-early", "master_sec": 100.0}]
    assert [c["stem"] for c in order_clips(clips, "time")] == ["b-early", "a-late"]
    assert [c["stem"] for c in order_clips(clips, "name")] == ["a-late", "b-early"]
    # one clip without a stamp and the whole ordering falls back to filenames
    clips[1] = {"stem": "b-early", "master_sec": None}
    assert [c["stem"] for c in order_clips(clips, "time")] == ["a-late", "b-early"]


def test_notes_refuses_two_clips_with_the_same_name(tmp_path):
    from cutdown.notes import find_clips
    for d in ("one", "two"):
        (tmp_path / d).mkdir()
        _write_silence(tmp_path / d / "seg01.wav", seconds=1)
    found = find_clips([tmp_path / "one"])
    assert [p.name for p in found] == ["seg01.wav"]
    with pytest.raises(CutdownError, match="share the filename"):
        find_clips([tmp_path / "one", tmp_path / "two"])


def test_notes_fillers_go_only_when_asked(tmp_path):
    from cutdown.notes import read_clip
    wj = tmp_path / "f.json"
    wj.write_text(json.dumps({"word_segments": _script(
        [("SPEAKER_00", 0.0, "Um, I mean, uh, it was fine.")])}))
    wav = _write_silence(tmp_path / "f.wav", seconds=5)
    assert "Um," in read_clip(wav, wj, gap=1.6, max_sec=40.0)["paragraphs"][0]["text"]
    tidy = read_clip(wav, wj, gap=1.6, max_sec=40.0, drop_fillers=True)
    assert tidy["paragraphs"][0]["text"] == "I mean, it was fine."


def test_notes_document_carries_measured_timestamps(clip):
    from cutdown.notes import name_speakers, resolve_summary
    from cutdown.notesdoc import render_clip_text, render_markdown, render_text

    clip["summary"] = resolve_summary({
        "headline": "Twelve takes at four in the morning", "summary": "A session.",
        "anecdotes": [{"title": "The twelve takes", "what": "It went long.",
                       "ids": ["P02", "P03"], "strength": "strong"}],
        "quotes": [{"id": "P03", "text": "twelve takes"}],
    }, clip)
    clip.update(n=1, cid="C01", names=name_speakers(clip, "Alex Vale", "Morgan Reed"))
    doc = {"context": {"guest": "Alex Vale", "show": "Record Room"},
           "transcriber": "whisperx large-v3", "provider": "zengo/gpt-5.6-luna",
           "clips": [clip], "digest": None,
           "totals": {"clips": 1, "sec": clip["duration"], "words": clip["words"],
                      "anecdotes": 1}}

    for text in (render_markdown(doc), render_text(doc), render_clip_text(clip, doc)):
        assert "Twelve takes at four in the morning" in text
        assert "[0:04" in text, "the anecdote's measured start is missing"
        assert "Alex Vale:" in text and "Morgan Reed:" in text
        assert "twelve takes" in text
    md = render_markdown(doc)
    assert "master 9:45" in md, "the clip's place in the session should be shown"
    assert "verbatim" in md, "the document must say what is generated and what is not"


def test_notes_clock_grows_only_when_it_has_to():
    from cutdown.util import clock
    assert clock(0) == "0:00"
    assert clock(64.4) == "1:04"
    assert clock(584.928) == "9:45"
    assert clock(3600) == "1:00:00"
    assert clock(4951) == "1:22:31"
