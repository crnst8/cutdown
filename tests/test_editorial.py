"""Regression tests for complete, guest-led final cuts."""
import struct
import wave
from types import SimpleNamespace

import pytest

from cutdown import assemble, check, pipeline, prompts
from cutdown.config import Config
from cutdown.render import _trim_point, apply_trims, run_render
from cutdown.util import write_json, write_jsonl
from cutdown.wav import WavInfo


@pytest.mark.parametrize('words,side', [
    ([[0, .2, 'first'], [.5, .7, 'tracks'], [1, 1.2, 'off'], [1.4, 1.6, 'it.']], 'tail'),
    ([[0, .2, 'Yeah,'], [.8, 1, 'I'], [1.1, 1.3, 'think']], 'head'),
    ([[0, .2, 'um'], [.25, .5, 'music']], 'head'),  # too little space
    ([[0, .2, 'um'], [.5, .7, 'uh']], 'head'),  # no speech left
    ([[0, .2], [.5, .7], [1, 1.2]], 'tail'),  # old timings without words
    ([[0, .2, 'love'], [.5, .7, 'um'], [1, 1.2, 'music']], 'tail'),
])
def test_trim_never_guesses_from_a_nearby_pause(words, side):
    assert _trim_point(words, side, .15, 2.5) is None


def test_only_the_exact_filler_boundary_is_trimmed():
    words = [[0, .2, 'Um,'], [.4, .5, 'uh'], [.8, 1, 'I'], [2, 2.2, 'love'],
             [2.4, 2.6, 'music.'], [3, 3.2, 'um.']]
    assert _trim_point(words, 'head', .15, 2.5) == .65
    assert _trim_point(words, 'tail', .15, 2.5) == 2.8


def test_order_is_a_unique_permutation():
    ids, order, *_ = assemble._sanitize(
        {'ids': ['a', 'b', 'a'], 'order': ['b', 'b', 'missing']}, {'a', 'b'})
    assert ids == ['a', 'b']
    assert order == ['b', 'a']


def make_build(path):
    units = [
        {'id': 'S1', 'role': 'GUEST', 'text': 'Um, I love music.'},
        {'id': 'S2', 'role': 'HOST', 'text': 'Why?'},
        {'id': 'S3', 'role': 'GUEST', 'text': 'It brings people together.'},
    ]
    timeline = {f'S{n+1}': {'cut_in': n * 4, 'cut_out': (n+1) * 4,
                            'dur_cut': 4} for n in range(3)}
    words = {'S1': [[0, .2, 'Um,'], [.6, .8, 'I'], [1, 1.2, 'love'], [2, 3, 'music.']]}
    write_jsonl(path / 'units.jsonl', units)
    write_json(path / 'timeline.json', timeline)
    write_json(path / 'unit_words.json', words)
    write_jsonl(path / 'labels.jsonl', [{'id': u['id'], 'role': 'anecdote'} for u in units])
    return units, timeline, words


def test_check_reads_trimmed_text_and_marks_actual_joins(tmp_path):
    units, timeline, words = make_build(tmp_path)
    doc = {'segments': [{'id': 'seg01', 'order': ['S1', 'S3'],
                         'trim': {'S1': {'head': True}}}]}
    rendered = check._render_segments(doc, {u['id']: u for u in units}, timeline, words)
    assert 'Um,' not in rendered
    assert '[S1] GUEST: I love music.' in rendered
    assert rendered.count('[EDIT JOIN]') == 1
    doc['segments'][0]['order'] = ['S1', 'S2', 'S3']
    assert '[EDIT JOIN]' not in check._render_segments(
        doc, {u['id']: u for u in units}, timeline, words)


def test_check_includes_topics(tmp_path, monkeypatch):
    make_build(tmp_path)
    write_json(tmp_path / 'segments.json', {'provider': 'openrouter/example-model', 'segments': []})
    write_json(tmp_path / 'topics.json', {'topics': [
        {'id': 'topic01', 'ids': ['S3'], 'anchor': {'kind': 'topic', 'title': 'Connection'}}]})
    captured = {}
    def request(provider, **kw):
        captured.update(kw)
        return SimpleNamespace(data={'segments': [{'id': 'topic01', 'verdict': 'Complete'}],
                                     'warnings': []})
    monkeypatch.setattr(check, 'request_json', request)
    check.run_check(tmp_path, Config.load(), SimpleNamespace(block=lambda: ''),
                    run_dir=tmp_path / 'runs')
    assert 'topic: Connection' in captured['user']
    assert 'It brings people together.' in captured['user']


@pytest.mark.parametrize('grace,requests,flag', [(2, 1, 'completion_extension'),
                                                (0, 3, 'over_target')])
def test_completion_allowance_avoids_cutting_for_a_small_overrun(
        tmp_path, monkeypatch, grace, requests, flag):
    make_build(tmp_path)
    cfg = Config.load()
    cfg.overlay({'assemble': {'completion_grace_sec': grace}, 'fillers': {'remove': False}})
    calls = []
    def request(provider, **kw):
        calls.append(kw)
        return SimpleNamespace(data={'ids': ['S1', 'S2', 'S3'],
                                     'order': ['S1', 'S2', 'S3'], 'reason': 'Complete thought.'})
    monkeypatch.setattr(assemble, 'request_json', request)
    ctx = SimpleNamespace(block=lambda: '', guest='', show='', episode='')
    brief = SimpleNamespace(segments=[{'id': 'seg01', 'target_sec': [5, 10]}],
                            guest='', show='', episode='')
    doc = assemble.run_assemble(tmp_path, cfg, ctx, brief, run_dir=tmp_path / 'runs')
    assert len(calls) == requests
    assert flag in doc['segments'][0]['flags']
    assert doc['segments'][0]['measured_sec'] == 12
    assert '(4.0s measured)' in calls[0]['user']


def test_render_preserves_meaningful_words_and_only_removes_safe_filler(tmp_path):
    units, timeline, words = make_build(tmp_path)
    doc = {'segments': [{'id': 'seg01', 'ids': ['S1'], 'order': ['S1'],
                         'trim': {'S1': {'head': True, 'tail': True}},
                         'target_sec': [0, 3], 'completion_grace_sec': 2}]}
    write_json(tmp_path / 'segments.json', doc)
    write_json(tmp_path / 'topics.json', {'topics': [
        {'id': 'topic01', 'ids': ['S3'], 'anchor': {'kind': 'topic', 'title': 'Connection'}}]})
    write_json(tmp_path / 'check.json', {'segments': [], 'warnings': [
        {'segment': 'topic01', 'id': 'S3', 'kind': 'ending', 'severity': 'high',
         'note': 'Test topic warning'}]})
    master = tmp_path / 'master.wav'
    samples = b''.join(struct.pack('<h', n % 1000) for n in range(12000))
    with wave.open(str(master), 'wb') as wav:
        wav.setparams((1, 2, 1000, 0, 'NONE', 'not compressed'))
        wav.writeframes(samples)
    run_render(tmp_path, tmp_path / 'out', master, chunks=False)
    final = tmp_path / 'out' / '1-final' / 'seg01.wav'
    info = WavInfo(final)
    # Filler removed at .4s; every remaining original sample through 4s survives.
    assert info.read_frames(0, 3600) == samples[800:8000]
    assert 'over_target' not in (tmp_path / 'out' / 'manifest.csv').read_text()
    assert assemble.measure(['S1'], timeline, words, doc['segments'][0]['trim']) == 3.6
    assert apply_trims(doc, timeline, words)['S1']['cut_out'] == 4
    from cutdown.report import write_report
    report = write_report(tmp_path, tmp_path / 'out',
                          SimpleNamespace(show='', guest='', episode='')).read_text()
    assert 'Test topic warning' in report
    assert 'Um,' not in report
    assert 'Needs a human' not in report  # a completion extension is informational


def test_prompt_override_invalidates_only_affected_stage(tmp_path):
    cfg = Config.load()
    original = cfg.stage_hash('assemble')
    ingest = cfg.stage_hash('ingest')
    (tmp_path / 'assemble.system.md').write_text('New editorial guidance')
    assert cfg.stage_hash('assemble', tmp_path) != original
    assert cfg.stage_hash('ingest', tmp_path) == ingest
    assert cfg.stage_hash('check', tmp_path) == cfg.stage_hash('check')


def test_check_cache_tracks_topics_and_words(tmp_path):
    p = pipeline.Paths(tmp_path, tmp_path / 'out')
    cfg = Config.load()
    write_json(tmp_path / 'check.json', {})
    pipeline.mark_done('check', p, cfg)
    assert pipeline.is_current('check', p, cfg)
    write_json(tmp_path / 'topics.json', {'topics': []})
    assert not pipeline.is_current('check', p, cfg)


def test_assemble_templates_still_render():
    assert prompts.render('assemble.retry', segment_id='seg01', measured=12,
                          target_lo=5, target_hi=10, delta=2, direction='long',
                          selection='S1', advice='Keep the conclusion')


def test_render_internal_filler_joins_tightly(tmp_path):
    import csv
    make_build(tmp_path)
    write_json(tmp_path / 'unit_words.json', {'S1': [[0,.2,'I'],[.5,.8,'um'],[1.1,1.3,'love'],[1.4,2,'music']]})
    write_json(tmp_path / 'segments.json', {'segments':[{'id':'seg01','ids':['S1']}]})
    master = tmp_path / 'master.wav'
    with wave.open(str(master), 'wb') as wav:
        wav.setparams((1, 2, 1000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\x01\x00' * 12000)
    out = tmp_path / 'out'
    run_render(tmp_path, out, master, chunks=False)
    rows = list(csv.DictReader((out / 'manifest.csv').open()))
    assert len(rows) == 2 and rows[1]['join'] == 'tight'
    assert WavInfo(out / '1-final/seg01.wav').total_frames == sum(int(r['frames']) for r in rows)


def test_render_phrase_hole_joins_tightly(tmp_path):
    import csv
    make_build(tmp_path)
    write_json(tmp_path / 'segments.json', {'segments':[{'id':'seg01','ids':['S1'],'holes':{'S1':[[1.5,1.8]]}}]})
    master = tmp_path / 'master.wav'
    samples = b''.join(struct.pack('<h', n % 1000) for n in range(12000))
    with wave.open(str(master), 'wb') as wav:
        wav.setparams((1,2,1000,0,'NONE','not compressed')); wav.writeframes(samples)
    out=tmp_path/'out'
    run_render(tmp_path,out,master,chunks=False,fillers={'remove':False})
    rows=list(csv.DictReader((out/'manifest.csv').open()))
    assert len(rows)==2 and rows[1]['join']=='tight'
    info=WavInfo(out/'1-final/seg01.wav')
    assert info.read_frames(0,info.total_frames) == samples[:3000]+samples[3600:8000]


def test_render_rejects_plan_covering_unselected_audio(tmp_path,monkeypatch):
    from cutdown import render
    make_build(tmp_path)
    write_json(tmp_path/'segments.json',{'segments':[{'id':'seg01','ids':['S1','S3']}]})
    monkeypatch.setattr(render.spans,'clips',lambda *a:[{'in':0,'out':12,'units':['S1','S3'],'join':'start'}])
    master=tmp_path/'master.wav'
    with wave.open(str(master),'wb') as wav:
        wav.setparams((1,2,1000,0,'NONE','not compressed'));wav.writeframes(b'\x01\x00'*12000)
    from cutdown.util import CutdownError
    with pytest.raises(CutdownError,match='samples unaccounted for'):
        run_render(tmp_path,tmp_path/'out',master,chunks=False,fillers={'remove':False})
