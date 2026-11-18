"""Sections, presets, topics, the output layout and the API wire formats.

No fixture needed: everything here runs against small synthetic inputs.
Run:  .venv/bin/python -m pytest tests -q
"""
from __future__ import annotations

import json
import pathlib
import sys
import wave

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cutdown import pipeline, presets, sections, topics  # noqa: E402
from cutdown.config import Config  # noqa: E402
from cutdown.context import EpisodeContext  # noqa: E402
from cutdown.providers.api import ApiProvider, _LEARNED  # noqa: E402
from cutdown.util import CutdownError, read_json, write_json, write_jsonl  # noqa: E402

RATE = 8000


def _units(spans: list[tuple[float, float]], role="GUEST") -> tuple[list, dict]:
    units, timeline = [], {}
    for n, (a, b) in enumerate(spans, 1):
        uid = f"S{n:04d}"
        gap = round(a - spans[n - 2][1], 3) if n > 1 else None
        units.append({"id": uid, "speaker": "A", "role": role, "text": f"unit {n}",
                      "start": a, "end": b, "dur": b - a, "gap_before": gap,
                      "words": 3})
        timeline[uid] = {"start": a, "end": b, "cut_in": a, "cut_out": b,
                         "dur_cut": round(b - a, 3)}
    return units, timeline


# --- sections ---------------------------------------------------------------

def test_sections_break_at_long_silences():
    units, tl = _units([(0, 10), (10.5, 20), (80, 90), (90.4, 100), (200, 210)])
    secs = sections.detect_sections(units, tl, gap_sec=20, max_sec=1000)
    assert [s["ids"] for s in secs] == [["S0001", "S0002"], ["S0003", "S0004"],
                                        ["S0005"]]
    assert [s["break"] for s in secs] == ["start", "silence", "silence"]
    assert secs[1]["gap_before"] == 60.0
    assert (secs[1]["cut_in"], secs[1]["cut_out"]) == (80, 100)


def test_long_sections_split_at_their_widest_pause():
    spans, t = [], 0.0
    for n in range(40):
        gap = 4.0 if n == 20 else 0.5
        spans.append((t + gap, t + gap + 10))
        t = spans[-1][1]
    units, tl = _units(spans)
    secs = sections.detect_sections(units, tl, gap_sec=20, max_sec=300)
    assert len(secs) >= 2
    assert secs[1]["break"] == "length"
    assert secs[1]["first"] == "S0021", "split should land on the 4s pause"
    assert all(s["end"] - s["start"] <= 300 or len(s["ids"]) == 1 for s in secs)


def test_section_headers_carry_no_positions():
    units, tl = _units([(0, 10), (95.25, 100)])
    secs = sections.detect_sections(units, tl, gap_sec=20)
    text = sections.index(units, secs)
    assert "--- C02" in text and "silence" in text
    assert "95.25" not in text and "85.25" not in text


# --- context and presets ----------------------------------------------------

def test_detail_and_instructions_both_reach_the_prompt():
    ctx = EpisodeContext(instructions="Songs are played in the room.",
                         detail="Four or five songs; keep the extra chat.")
    block = ctx.block()
    assert "Songs are played in the room." in block
    assert "Four or five songs" in block
    assert block.index("Songs are played") < block.index("Four or five")


def test_preset_round_trip_and_apply(tmp_path, monkeypatch):
    monkeypatch.setenv("CUTDOWN_PRESETS", str(tmp_path))
    presets.save("Radio Show", {"show": "Record Room", "names": ["Morgan"],
                                "instructions": "Records play in the room.",
                                "extras": True, "targets": {"guest_pick": [60, 200]},
                                "config": {"transcribe": {"max_speakers": 3}}})
    assert presets.names() == ["radio-show"]
    over = presets.apply("radio show", "four or five songs today")
    assert over["preset"] == "radio-show"
    assert over["instructions"] == "Records play in the room."
    assert over["detail"] == "four or five songs today"
    assert over["extras"] is True and over["config"]["transcribe"]["max_speakers"] == 3


def test_preset_rejects_unknown_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("CUTDOWN_PRESETS", str(tmp_path))
    (tmp_path / "bad.yaml").write_text("show: x\nshwo: y\n")
    with pytest.raises(CutdownError, match="unknown key"):
        presets.load("bad")


def test_reapplying_a_preset_keeps_the_days_detail(tmp_path, monkeypatch):
    monkeypatch.setenv("CUTDOWN_PRESETS", str(tmp_path))
    presets.save("radio", {"instructions": "standing"})
    p = pipeline.Paths(build=tmp_path / "build", outdir=tmp_path)
    pipeline.load_context(p, overrides=presets.apply("radio", "today only"))
    ctx = pipeline.load_context(p, overrides=presets.apply("radio"))
    assert ctx.detail == "today only" and ctx.instructions == "standing"


def test_context_config_overlay_changes_the_stage_hash(tmp_path):
    cfg = Config.load()
    before = cfg.stage_hash("ingest")
    p = pipeline.Paths(build=tmp_path / "build", outdir=tmp_path)
    pipeline.load_context(p, overrides={"config": {"ingest": {"section_gap": 30}}},
                          config=cfg)
    assert cfg.get("ingest.section_gap") == 30
    assert cfg.stage_hash("ingest") != before


def test_cli_preset_save_from_flags(tmp_path, monkeypatch):
    from cutdown import cli
    monkeypatch.setenv("CUTDOWN_PRESETS", str(tmp_path))
    assert cli.main(["preset", "save", "radio", "--show", "Record Room", "--name", "Morgan",
                     "--detail", "records play in the room", "--extras", "keep"]) == 0
    d = presets.load("radio")
    assert d["show"] == "Record Room" and d["names"] == ["Morgan"]
    assert d["instructions"] == "records play in the room"
    assert d["extras"] is True


# --- topics -----------------------------------------------------------------

def test_topics_follow_the_brief_unless_context_overrides():
    class B:
        data = {"extras": {"keep": True}}
    assert topics.wanted(EpisodeContext(), B())
    assert not topics.wanted(EpisodeContext(extras=False), B())
    assert not topics.wanted(EpisodeContext(), None)


def test_topic_sanitize_is_exclusive_ordered_and_measured():
    units, tl = _units([(0, 10), (11, 20), (21, 30), (31, 40), (41, 42)])
    labels = {"S0002": {"role": "admin"}, "S0001": {"role": "aside"},
              "S0003": {"role": "anecdote"}, "S0004": {"role": "aside"},
              "S0005": {"role": "aside"}}
    segs = {"segments": [{"id": "seg01", "ids": ["S0001"]}]}
    pool = topics.leftover(units, labels, segs)
    assert pool == ["S0003", "S0004", "S0005"], "used and admin units stay out"
    raw = {"topics": [{"title": "B", "ids": ["S0004", "S0003", "S0001"]},
                      {"title": "A", "ids": ["S0003", "S0005"]}]}
    out, dropped = topics.sanitize(raw, pool, tl, min_sec=0)
    assert dropped == ["S0001"]
    assert out[0]["ids"] == ["S0003", "S0004"], "recorded order, not model order"
    assert out[1]["ids"] == ["S0005"], "a unit goes to one topic only"
    assert out[0]["id"] == "topic01" and out[0]["measured_sec"] == 18.0
    short, _ = topics.sanitize(raw, pool, tl, min_sec=5)
    assert [t["title"] for t in short] == ["B"]


# --- render layout ----------------------------------------------------------

def _write_master(path: pathlib.Path, seconds: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"\x01\x00" * RATE * seconds)


def test_render_writes_final_clips_and_chunks(tmp_path):
    from cutdown.render import run_render
    build = tmp_path / "build"
    units, tl = _units([(1, 5), (5, 9), (40, 45), (46, 50), (52, 55)])
    write_jsonl(build / "units.jsonl", units)
    write_json(build / "timeline.json", tl)
    write_jsonl(build / "labels.jsonl", [
        {"id": u["id"], "topics": ["seg01"] if n < 2 else ["general"],
         "role": "admin" if n == 4 else "anecdote", "guest_led": True}
        for n, u in enumerate(units)])
    write_json(build / "segments.json", {"episode": "t", "segments": [
        {"id": "seg01", "anchor": {"artist": "A", "title": "Paper Moon",
                                   "kind": "guest_pick"},
         "target_sec": [1, 100], "ids": ["S0001", "S0002"],
         "order": ["S0001", "S0002"]}]})
    write_json(build / "topics.json", {"enabled": True, "topics": [
        {"id": "topic01", "title": "Touring", "ids": ["S0003", "S0004"],
         "order": ["S0003", "S0004"], "anchor": {"title": "Touring", "kind": "topic"}}]})
    write_json(build / "sections.json",
               sections.detect_sections(units, tl, gap_sec=20))
    master = tmp_path / "master.wav"
    _write_master(master, 60)

    r = run_render(build, tmp_path, master)
    assert (tmp_path / "1-final" / "seg01_paper-moon.wav").exists()
    assert (tmp_path / "1-final" / "topic01_touring.wav").exists()
    seg_clips = sorted((tmp_path / "2-clips" / "seg01_paper-moon").iterdir())
    assert len(seg_clips) == 1, "adjacent units render as one clip"
    assert (tmp_path / "2-clips" / "topic01_touring").is_dir()
    chunks = sorted(p.name for p in (tmp_path / "3-chunks").iterdir())
    assert chunks[0].startswith("C01_00m01s_paper-moon")
    assert chunks[1].startswith("C02_00m40s_touring")
    assert len(r["chunks"]) == 2 and r["topics"][0]["final"].startswith("1-final/")
    rows = (tmp_path / "manifest.csv").read_text().splitlines()
    assert rows[0].startswith("kind,") and any(x.startswith("chunk,") for x in rows)


def test_render_refuses_a_unit_in_both_a_segment_and_a_topic(tmp_path):
    from cutdown.render import run_render
    build = tmp_path / "build"
    units, tl = _units([(1, 5), (6, 9)])
    write_jsonl(build / "units.jsonl", units)
    write_json(build / "timeline.json", tl)
    write_json(build / "segments.json", {"segments": [
        {"id": "seg01", "ids": ["S0001"], "order": ["S0001"]}]})
    write_json(build / "topics.json", {"topics": [
        {"id": "topic01", "ids": ["S0001"], "order": ["S0001"]}]})
    master = tmp_path / "m.wav"
    _write_master(master, 10)
    with pytest.raises(CutdownError, match="appears in both"):
        run_render(build, tmp_path, master, skip_admin_check=True)


# --- pipeline ---------------------------------------------------------------

def test_discover_never_overwrites_an_existing_brief(tmp_path):
    brief = tmp_path / "brief.yaml"
    brief.write_text("segments:\n- id: seg01\n  target_sec: [60, 90]\n")
    job = pipeline.Job(paths=pipeline.Paths(build=tmp_path / "build", outdir=tmp_path),
                       config=Config.load(), brief_path=brief)
    assert pipeline.run_stage(job, "discover") == "skipped"
    assert "seg01" in brief.read_text()


def test_topics_off_writes_an_empty_file_without_a_model(tmp_path):
    build = tmp_path / "build"
    build.mkdir()
    doc = topics.run_topics(build, Config.load(), EpisodeContext(extras=False), None,
                            run_dir=tmp_path / "runs")
    assert doc == {"enabled": False, "topics": []}
    assert read_json(build / "topics.json")["enabled"] is False


# --- API wire formats ---------------------------------------------------------

def test_auto_wire_prefers_the_model_family_then_falls_through():
    p = ApiProvider("zengo", Config.load().provider("zengo"))
    assert p.wires_for("gpt-6-luna")[0] == "responses"
    assert p.wires_for("qwen3.8-max")[0] == "messages"
    assert p.wires_for("kimi-k3") == ["chat", "messages", "responses"]
    _LEARNED[(p.cfg["base_url"], "kimi-k3")] = "messages"
    try:
        assert p.wires_for("kimi-k3")[0] == "messages"
    finally:
        _LEARNED.pop((p.cfg["base_url"], "kimi-k3"))


def test_every_request_carries_a_session_header():
    p = ApiProvider("zengo", {**Config.load().provider("zengo"), "api_key_env": "X"})
    h = p._headers()
    assert h["x-opencode-session"].startswith("cutdown-")
    assert h["x-opencode-session"] == p._headers()["x-opencode-session"]


def test_openrouter_sends_routing_rules_and_no_session_header():
    p = ApiProvider("openrouter", {**Config.load().provider("openrouter"),
                                   "api_key_env": "X"})
    assert p.wires_for("openai/gpt-6-luna") == ["chat"]
    h = p._headers()
    assert "x-opencode-session" not in h and h["X-Title"] == "cutdown"
    assert "anthropic-version" not in h
    assert p._headers("messages")["anthropic-version"]
    body = p._body("chat", "sys", "hi", "openai/gpt-6-luna")
    # Unpublished transcript: only hosts that neither store nor train on it.
    assert body["provider"]["data_collection"] == "deny"
    assert body["messages"][1] == {"role": "user", "content": "hi"}


def test_rate_limits_wait_out_the_window():
    from cutdown.providers.api import _retry_wait
    assert _retry_wait(429, "7", 1) == 7
    assert _retry_wait(429, None, 1) >= 15
    assert _retry_wait(503, None, 1) == 2


@pytest.mark.parametrize("wire,payload", [
    ("chat", {"choices": [{"message": {"content": "{\"a\":1}"},
                           "finish_reason": "stop"}]}),
    ("messages", {"content": [{"type": "thinking", "thinking": "hm"},
                              {"type": "text", "text": "{\"a\":1}"}],
                  "stop_reason": "end_turn"}),
    ("responses", {"status": "completed", "output": [
        {"type": "reasoning", "content": []},
        {"type": "message", "content": [{"type": "output_text",
                                         "text": "{\"a\":1}"}]}]}),
])
def test_each_wire_format_parses_to_the_reply_text(wire, payload):
    text, meta = ApiProvider._content(wire, json.dumps(payload), {})
    assert text == "{\"a\":1}"
    assert meta["finish_reason"]


def test_body_shapes():
    p = ApiProvider("zengo", Config.load().provider("zengo"))
    r = p._body("responses", "sys", "hi", "gpt-6-luna")
    assert r["instructions"] == "sys" and "temperature" not in r
    m = p._body("messages", "sys", "hi", "qwen3.8-max")
    assert m["system"] == "sys" and m["messages"][0]["role"] == "user"
    c = p._body("chat", "sys", "hi", "kimi-k3")
    assert c["messages"][0] == {"role": "system", "content": "sys"}


# --- studio -------------------------------------------------------------------

def test_studio_opens_on_the_context_screen_and_saves_it(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    pytest.importorskip("textual")
    from cutdown import context as ctxmod
    from cutdown.studio import ContextScreen, Studio
    from textual.widgets import TextArea

    monkeypatch.setenv("CUTDOWN_PRESETS", str(tmp_path / "presets"))
    presets.save("radio", {"show": "Record Room", "instructions": "standing"})
    args = SimpleNamespace(outdir=tmp_path / "ep", brief=None, config=None,
                           audio=None, json_in=None, master=None, prompts=None,
                           preset=None, detail=None, guest=None, show=None,
                           episode=None, song=[], name=[], context=None)

    async def go():
        app = Studio(args)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, ContextScreen)
            scr = app.screen
            from textual.widgets import Input, Static
            assert scr.query_one("#ctx-host", Input) is not None
            assert scr.query_one("#ctx-preview-pane").display
            scr.query_one("#ctx-guest", Input).value = "Test Guest"
            await pilot.pause()
            assert "Guest: Test Guest" in str(scr.query_one("#ctx-preview", Static).render())
            scr.query_one("#ctx-preset").value = "radio"
            await pilot.pause()
            scr.query_one("#ctx-detail", TextArea).text = "four or five songs"
            scr.query_one("#ctx-apply").scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click("#ctx-apply")
            await pilot.pause()
            assert not isinstance(app.screen, ContextScreen)
            await pilot.press("q")

    asyncio.run(go())
    ctx = ctxmod.load(tmp_path / "ep" / "build")
    assert ctx.preset == "radio" and ctx.show == "Record Room"
    assert ctx.instructions == "standing" and ctx.detail == "four or five songs"


# --- one command ------------------------------------------------------------

@pytest.mark.parametrize("typed", [
    "/tmp/My Show/ep 1.wav",
    "/tmp/My\\ Show/ep\\ 1.wav ",
    "'/tmp/My Show/ep 1.wav'",
    '"/tmp/My Show/ep 1.wav"',
])
def test_dragged_and_typed_paths_resolve(typed):
    from cutdown.cli import clean_path
    assert clean_path(typed) == pathlib.Path("/tmp/My Show/ep 1.wav")


def test_bare_cutdown_with_a_file_starts_the_whole_job(tmp_path, monkeypatch):
    from cutdown import cli
    audio = tmp_path / "Today Show.wav"
    audio.write_bytes(b"")
    seen = {}
    monkeypatch.setattr(cli, "cmd_studio", lambda a: seen.update(vars(a)) or 0)
    assert cli.main([str(audio), "--preset", "radio"]) == 0
    assert seen["preset"] == "radio"
    assert seen["audio"] == audio and seen["autorun"] is True
    assert seen["outdir"] == pathlib.Path.cwd() / "ep" / "today-show"


def test_last_preset_is_remembered(tmp_path, monkeypatch):
    monkeypatch.setenv("CUTDOWN_PRESETS", str(tmp_path))
    presets.save("radio", {"show": "x"})
    assert presets.last() == ""
    presets.remember("radio")
    assert presets.last() == "radio"


def test_one_command_studio_preselects_last_preset_and_starts(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    pytest.importorskip("textual")
    from textual.widgets import Button, Select
    from cutdown.studio import ContextScreen, Studio

    monkeypatch.setenv("CUTDOWN_PRESETS", str(tmp_path / "presets"))
    presets.save("radio", {"show": "Record Room", "instructions": "standing"})
    presets.remember("radio")
    ran = []
    monkeypatch.setattr(Studio, "action_run_all", lambda self: ran.append(True))
    args = SimpleNamespace(outdir=tmp_path / "ep", brief=None, config=None,
                           audio=None, json_in=None, master=None, prompts=None,
                           preset=None, detail=None, guest=None, show=None,
                           episode=None, song=[], name=[], context=None, autorun=True)

    async def go():
        app = Studio(args)
        async with app.run_test(size=(140, 45)) as pilot:
            await pilot.pause()
            scr = app.screen
            assert isinstance(scr, ContextScreen)
            assert scr.query_one("#ctx-preset", Select).value == "radio"
            assert str(scr.query_one("#ctx-apply", Button).label) == "Start"
            scr.query_one("#ctx-apply").scroll_visible(animate=False)
            await pilot.pause()
            await pilot.click("#ctx-apply")
            await pilot.pause()
            app.exit()

    asyncio.run(go())
    assert ran == [True], "Start runs the whole pipeline"


# --- split-channel recordings -------------------------------------------------

def _stereo24(path: pathlib.Path, left: list[int], right: list[int]) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(3)
        w.setframerate(RATE)
        w.writeframes(b"".join(l.to_bytes(3, "little", signed=True)
                               + r.to_bytes(3, "little", signed=True)
                               for l, r in zip(left, right)))


def test_split_is_a_byte_exact_deinterleave(tmp_path):
    from cutdown import channels
    from cutdown.wav import WavInfo
    left = [i * 3 - 5000 for i in range(3000)]
    right = [-(i * 7) for i in range(3000)]
    src = tmp_path / "talk.wav"
    _stereo24(src, left, right)
    lp, rp = channels.split(src, tmp_path / "ch")
    for path, want in ((lp, left), (rp, right)):
        info = WavInfo(path)
        assert (info.channels, info.bits_per_sample, info.sample_rate) == (1, 24, RATE)
        raw = info.read_frames(0, info.total_frames)
        got = [int.from_bytes(raw[i:i + 3], "little", signed=True)
               for i in range(0, len(raw), 3)]
        assert got == want


def test_split_refuses_a_mono_file(tmp_path):
    from cutdown import channels
    src = tmp_path / "mono.wav"
    _write_master(src, 1)
    with pytest.raises(CutdownError, match="stereo"):
        channels.split(src, tmp_path / "ch")


def test_merge_keeps_bleed_only_on_the_louder_channel(tmp_path):
    from cutdown import channels
    # Left is loud for the first second, right is loud for the second.
    loud, quiet = 400000, 2000
    left = [loud] * RATE + [quiet] * RATE
    right = [quiet] * RATE + [loud] * RATE
    src = tmp_path / "talk.wav"
    _stereo24(src, left, right)
    mono = channels.split(src, tmp_path / "ch")
    lj, rj = tmp_path / "l.json", tmp_path / "r.json"
    write_json(lj, {"word_segments": [
        {"word": "hello", "start": 0.1, "end": 0.5},
        {"word": "there", "start": 1.2, "end": 1.6}]})     # bleed of the right mic
    write_json(rj, {"word_segments": [
        {"word": "hello", "start": 0.12, "end": 0.5},      # bleed of the left mic
        {"word": "There.", "start": 1.21, "end": 1.58},
        {"word": "yes", "start": 0.6, "end": 0.8}]})       # real overlap, kept
    out = tmp_path / "merged.json"
    stats = channels.merge(lj, rj, mono, out)
    doc = read_json(out)
    got = [(w["word"], w["speaker"]) for w in doc["word_segments"]]
    assert got == [("hello", "SPEAKER_L"), ("yes", "SPEAKER_R"),
                   ("There.", "SPEAKER_R")]
    assert stats["bleed_removed"] == 2 and doc["cutdown_channels"] == "split"


def test_split_transcribe_runs_each_channel_undiarized_and_merges(tmp_path, monkeypatch):
    from cutdown import transcribe
    src = tmp_path / "talk.wav"
    _stereo24(src, [1000] * RATE, [-1000] * RATE)
    calls = []

    def fake(audio, outdir, cfg, **kw):
        calls.append((audio.name, cfg["diarize"]))
        out = outdir / f"{audio.stem}.json"
        write_json(out, {"word_segments": [
            {"word": audio.stem[-1], "start": 0.1, "end": 0.2}]})
        return out
    monkeypatch.setattr(transcribe, "_whisperx", fake)
    out = transcribe.run_transcribe(src, tmp_path / "t", {"channels": "host-left"})
    assert calls == [("talk.L.wav", False), ("talk.R.wav", False)]
    assert [w["speaker"] for w in read_json(out)["word_segments"]] == \
        ["SPEAKER_L", "SPEAKER_R"]
    # Recovered as split; a mixed request does not reuse it.
    calls.clear()
    transcribe.run_transcribe(src, tmp_path / "t", {"channels": "guest-left"})
    assert calls == []
    monkeypatch.setattr(transcribe, "hf_token", lambda cfg: "x")
    monkeypatch.setattr(transcribe, "_whisperx",
                        lambda audio, outdir, cfg, **kw: calls.append("mixed") or
                        outdir / f"{audio.stem}.json")
    transcribe.run_transcribe(src, tmp_path / "t", {"channels": "mixed"})
    assert calls == ["mixed"]


def test_ingest_takes_the_guest_from_the_channel(tmp_path):
    from cutdown.ingest import run_ingest
    wj = tmp_path / "w.json"
    # The host says more; without the channel layout they would be taken as guest.
    words = [{"word": f"h{i}", "start": i * 0.3, "end": i * 0.3 + 0.2,
              "speaker": "SPEAKER_L"} for i in range(30)]
    words += [{"word": "g", "start": 20.0, "end": 20.2, "speaker": "SPEAKER_R"}]
    write_json(wj, {"word_segments": words})
    run_ingest(wj, tmp_path / "b", guest_speaker="SPEAKER_R")
    roles = {json.loads(l)["speaker"]: json.loads(l)["role"]
             for l in (tmp_path / "b" / "units.jsonl").read_text().splitlines()}
    assert roles == {"SPEAKER_L": "HOST", "SPEAKER_R": "GUEST"}


def test_channel_layout_changes_transcribe_and_ingest_hashes(tmp_path):
    cfg = Config.load()
    p = pipeline.Paths(build=tmp_path / "build", outdir=tmp_path)
    pipeline.load_context(p, config=cfg)
    t, i = cfg.stage_hash("transcribe"), cfg.stage_hash("ingest")
    pipeline.load_context(p, overrides={"channels": "host-left"}, config=cfg)
    assert cfg.stage_hash("transcribe") != t and cfg.stage_hash("ingest") != i
    with pytest.raises(CutdownError, match="channels must be"):
        pipeline.load_context(p, overrides={"channels": "left"}, config=cfg)


def test_host_and_speaker_context(tmp_path):
    from cutdown.context import EpisodeContext, load, save
    from dataclasses import asdict
    from cutdown.util import write_json
    c = EpisodeContext(guest="A", host="B")
    assert "Host: B" in c.block() and "diarization" in c.block()
    c.channels = "host-left"
    assert "reliable" in c.block()
    d = asdict(c); d.pop("host")
    write_json(tmp_path / "context.json", d)
    before = (tmp_path / "context.json").stat().st_mtime_ns
    save(tmp_path, load(tmp_path))
    assert (tmp_path / "context.json").stat().st_mtime_ns == before


def test_question_rate_selection():
    from cutdown.ingest import guest_by_questions
    def unit(s,n,q):
        return [{"speaker":s,"word":"why?" if i<q else "word"} for i in range(n)]
    assert guest_by_questions([unit("A",400,3),unit("B",300,12)]) == "A"
    assert guest_by_questions([unit("A",400,3),unit("B",300,4)]) is None
    assert guest_by_questions([unit("A",400,3),unit("B",300,12),unit("C",30,0)]) is None


def test_evidence_only_available():
    from cutdown.assemble import _segment_note
    assert "S1" in _segment_note({"evidence":["S1","S2"]}, {"S1"})
    assert "S2" not in _segment_note({"evidence":["S1","S2"]}, {"S1"})


def test_context_screen_replaces_and_splits_lines():
    from cutdown.studio import merged_context, split_lines
    from cutdown.context import EpisodeContext
    c = merged_context(EpisodeContext(names=["a","b"], guest="G",instructions="Keep",extras=True), {"names":["a"],"guest":"","extras":None})
    assert c.names==["a"] and c.guest=="" and c.instructions=="Keep" and c.extras is None
    assert split_lines("Morgan (host, left)\nOther") == ["Morgan (host, left)","Other"]


def test_confirm_modal(tmp_path):
    import asyncio
    from textual.app import App
    from cutdown.studio import ConfirmScreen
    async def go():
        app = App()
        answers=[]
        async with app.run_test() as pilot:
            app.push_screen(ConfirmScreen("Revise?", "3 warnings in 2 of 5 pieces"), answers.append)
            await pilot.pause(); await pilot.press("y"); await pilot.pause()
            app.push_screen(ConfirmScreen("Revise?"), answers.append)
            await pilot.pause(); await pilot.press("enter"); await pilot.pause()
        assert answers == [True, False]
    asyncio.run(go())


def test_ingest_split_roles(tmp_path):
    from cutdown.ingest import run_ingest
    from cutdown.util import write_json,read_json
    words=[]
    for i in range(20):
        words.append({'start':i*2,'end':i*2+.5,'word':'Why?' if i<8 else 'word', 'speaker':'A'})
        words.append({'start':i*2+1,'end':i*2+1.5,'word':'Answer.', 'speaker':'B'})
    write_json(tmp_path/'source.json',{'word_segments':words})
    stats=run_ingest(tmp_path/'source.json',tmp_path/'build',guest_speaker='A',repair=False)
    assert stats['roles']=='swapped'
    assert read_json(tmp_path/'build/roles.json')['mode']=='split'


def test_swap_decline_and_accept(tmp_path,monkeypatch):
    from cutdown import pipeline,context
    from cutdown.config import Config
    from cutdown.util import write_json
    b=tmp_path/'build';b.mkdir()
    context.save(b,context.EpisodeContext(channels='host-left'))
    write_json(b/'roles.json',{'mode':'split','verdict':'swapped','guest':{'rate':2},'host':{'rate':0}})
    job=pipeline.Job(pipeline.Paths(b,tmp_path),Config.load(),tmp_path/'brief.yaml',overrides={'channels':'host-left'})
    assert not pipeline.maybe_swap(job,lambda *a:False)
    assert not pipeline.maybe_swap(job,lambda *a:pytest.fail('asked twice'))
    context.save(b,context.EpisodeContext(channels='guest-left'))
    called=[]
    monkeypatch.setattr(pipeline,'run_stage',lambda *a,**kw:called.append(kw))
    assert pipeline.maybe_swap(job,lambda *a:True)
    assert context.load(b).channels=='host-left' and job.overrides['channels']=='host-left'
    assert called==[{'force':True}]


def test_host_preset_roundtrip(tmp_path,monkeypatch):
    monkeypatch.setenv("CUTDOWN_PRESETS",str(tmp_path))
    presets.save("host-test",{"host":"Morgan"})
    assert presets.apply("host-test","")["host"]=="Morgan"
