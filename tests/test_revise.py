from types import SimpleNamespace
import pytest
from cutdown import revise, check, pipeline
from cutdown.config import Config
from cutdown.util import write_json, write_jsonl, read_json, CutdownError

@pytest.fixture
def edit(tmp_path, monkeypatch):
    units = [{'id':f'S{n:02}', 'role':'GUEST', 'text':f'Words from unit {n}.'} for n in range(1,13)]
    write_jsonl(tmp_path/'units.jsonl', units)
    write_jsonl(tmp_path/'labels.jsonl', [{"id":u['id'],"role":"admin" if u['id']=='S12' else 'anecdote'} for u in units])
    write_json(tmp_path/'timeline.json', {u['id']:{'cut_in':i*4,'cut_out':(i+1)*4,'dur_cut':4} for i,u in enumerate(units)})
    write_json(tmp_path/'segments.json', {'segments':[{'id':'seg01','ids':['S02','S03','S06'],'order':['S02','S03','S06'],'target_sec':[8,20],'measured_sec':12}]})
    write_json(tmp_path/'topics.json', {'topics':[{'id':'topic01','ids':['S08','S09','S10','S11'],'order':['S08','S09','S10','S11']}], 'unplaced':[]})
    write_json(tmp_path/'check.json', {'segments':[], 'warnings':[{'segment':'seg01','id':'S06','kind':'join','severity':'high','note':'Missing context'}]})
    cfg = Config.load(); cfg.overlay({'topics':{'min_sec':8}})
    ctx = SimpleNamespace(block=lambda:'')
    calls=[]
    def response(provider, **kw):
        calls.append(kw)
        return SimpleNamespace(data={'ids':['S02','S03','S04'],'order':['S02','S03','S04'],'reason':'Complete'})
    monkeypatch.setattr(revise,'request_json',response)
    monkeypatch.setattr(check,'request_json',lambda provider,**kw:SimpleNamespace(data={'segments':[{'id':'seg01','verdict':'Complete'}],'warnings':[]}))
    return tmp_path,cfg,ctx,calls

def run(edit):
    b,c,ctx,_=edit
    return revise.run_revise(b,c,ctx,None,run_dir=b/'runs')

def test_accept_candidates_and_backup(edit):
    b,c,ctx,calls=edit
    result=run(edit)
    assert result['groups']==['seg01']
    seg=read_json(b/'segments.json')['segments'][0]
    assert seg['revision']['added']==['S04'] and seg['revision']['removed']==['S06']
    assert seg['rejected'][-1]['id']=='S06'
    assert 'S12' not in calls[0]['user'] and 'in:topic01' in calls[0]['user']
    assert (b/'pre-revise/segments.json').exists()
    with pytest.raises(CutdownError,match='already revised'): run(edit)

@pytest.mark.parametrize('ids',[[],['S01','S02','S03','S04','S05','S06','S07','S08','S09']])
def test_reject(edit,monkeypatch,ids):
    b,c,*_=edit
    c.overlay({'assemble':{'completion_grace_sec':0}})
    monkeypatch.setattr(revise,'request_json',lambda *a,**kw:SimpleNamespace(data={'ids':ids,'order':ids,'reason':''}))
    assert run(edit)['groups']==[]
    seg=read_json(b/'segments.json')['segments'][0]
    assert seg['ids']==['S02','S03','S06'] and 'revision_rejected' in seg['flags']

def test_unchanged_does_not_check(edit,monkeypatch):
    monkeypatch.setattr(revise,'request_json',lambda *a,**kw:SimpleNamespace(data={'ids':['S02','S03','S06'],'order':['S02','S03','S06'],'reason':''}))
    monkeypatch.setattr(check,'request_json',lambda *a,**kw:pytest.fail('rechecked unchanged'))
    assert run(edit)['groups']==[]

def test_rollback(edit,monkeypatch):
    b,*_=edit
    before={p:p.read_bytes() for p in b.glob('*.json')}
    def fail(*a,**kw): raise CutdownError('failed')
    monkeypatch.setattr(check,'request_json',fail)
    with pytest.raises(CutdownError): run(edit)
    assert all(p.read_bytes()==v for p,v in before.items())

def test_topic_transfer(edit,monkeypatch):
    b,*_=edit
    monkeypatch.setattr(revise,'request_json',lambda *a,**kw:SimpleNamespace(data={'ids':['S02','S03','S08'],'order':['S02','S03','S08'],'reason':''}))
    run(edit)
    assert 'S08' not in read_json(b/'topics.json')['topics'][0]['ids']

def test_gate_decline_and_no_warnings(edit):
    b,c,ctx,_=edit
    job=pipeline.Job(pipeline.Paths(b,b.parent),c,b/'brief.yaml')
    called=[]
    assert pipeline.maybe_revise(job,lambda *a:called.append(a))=='declined'
    assert pipeline.maybe_revise(job,lambda *a:pytest.fail('asked twice'))=='none'
    assert len(called)==1
    write_json(b/'check.json',{'warnings':[]})
    assert pipeline.maybe_revise(job,lambda *a:pytest.fail('asked without warnings'))=='none'

def test_regressed(edit,monkeypatch):
    b,*_=edit
    monkeypatch.setattr(check,'request_json',lambda *a,**kw:SimpleNamespace(data={'segments':[], 'warnings':[{'segment':'seg01','severity':'high','note':str(i)} for i in range(2)]}))
    run(edit)
    assert 'revision_regressed' in read_json(b/'segments.json')['segments'][0]['flags']


def test_phrase_cuts(edit,monkeypatch):
    b,c,ctx,calls=edit
    from cutdown import spans
    words=[[4,4.2,"And"],[4.3,4.5,"yeah,"],[5,5.2,"I"],[5.3,5.5,"love"],[5.6,5.8,"all"],[6,6.2,"music."]]
    write_json(b/'unit_words.json',{'S02':words})
    captured=[]
    def reply(*a,**kw):
        return SimpleNamespace(data={'ids':['S02','S03','S06'],'order':['S02','S03','S06'],'reason':'Complete', 'cuts':[{'id':'S02','text':'And yeah,'},{'id':'S02','text':'not present'}]})
    def recheck(*a,**kw):
        captured.append(kw['user'])
        return SimpleNamespace(data={'segments':[], 'warnings':[]})
    monkeypatch.setattr(revise,'request_json',reply)
    monkeypatch.setattr(check,'request_json',recheck)
    run(edit)
    seg=read_json(b/'segments.json')['segments'][0]
    assert len(seg['holes']['S02'])==1
    assert len(seg['revision']['cuts'])==len(seg['revision']['cuts_refused'])==1
    assert seg['measured_sec']==12-.775
    assert 'And yeah' not in captured[0]


def test_partial_check_preserves_unchanged(edit,monkeypatch):
    b,c,ctx,_=edit
    chk=read_json(b/'check.json')
    chk['warnings'].append({'segment':'topic01','severity':'low','note':'Keep warning'})
    chk['segments'].append({'id':'topic01','verdict':'Keep verdict'})
    write_json(b/'check.json',chk)
    captured=[]
    def reply(*a,**kw):
        captured.append(kw['user'])
        return SimpleNamespace(data={'segments':[{'id':'seg01','verdict':'New'}],'warnings':[]})
    monkeypatch.setattr(check,'request_json',reply)
    check.run_check(b,c,ctx,run_dir=b/'runs',only={'seg01'})
    chk=read_json(b/'check.json')
    assert any(w['note']=='Keep warning' for w in chk['warnings'])
    assert any(s['verdict']=='Keep verdict' for s in chk['segments'])
    assert 'topic01' not in captured[0]


def test_dropped_topic_removes_warnings(edit,monkeypatch):
    b,c,*_=edit
    c.overlay({'topics':{'min_sec':16}})
    chk=read_json(b/'check.json');chk['warnings'].append({'segment':'topic01','severity':'low','note':'Old'})
    write_json(b/'check.json',chk)
    monkeypatch.setattr(revise,'request_json',lambda *a,**kw:SimpleNamespace(data={'ids':['S02','S03','S08'],'order':['S02','S03','S08'],'reason':''}))
    run(edit)
    assert read_json(b/'topics.json')['topics']==[]
    assert not any(w.get('segment')=='topic01' for w in read_json(b/'check.json')['warnings'])


def test_success_records_current_state(edit,monkeypatch):
    b,c,ctx,_=edit
    job=pipeline.Job(pipeline.Paths(b,b.parent),c,b/'brief.yaml')
    monkeypatch.setattr(job,'context',lambda *a:ctx)
    assert pipeline.maybe_revise(job,lambda *a:True)=='revised'
    for stage in ('assemble','topics','check'):
        assert pipeline.is_current(stage,job.paths,c,**job.io())
    assert pipeline.maybe_revise(job,lambda *a:pytest.fail('asked twice'))=='none'


def test_failure_without_topics_restores_absence(edit,monkeypatch):
    b,*_=edit
    (b/'topics.json').unlink()
    def fail(*a,**kw): raise CutdownError('failed')
    monkeypatch.setattr(check,'request_json',fail)
    with pytest.raises(CutdownError):run(edit)
    assert not (b/'topics.json').exists()


def test_cli_no_revise_does_not_ask(tmp_path,monkeypatch):
    from cutdown import cli
    b=tmp_path/'build';b.mkdir()
    write_json(b/'check.json',{'warnings':[{'segment':'seg01','note':'test'}]})
    source=tmp_path/'source.json';source.write_text('{}')
    def fail(*a):pytest.fail('asked on --no-revise')
    monkeypatch.setattr('builtins.input',fail)
    monkeypatch.setattr(pipeline,'run_stage',lambda *a,**kw:'skipped')
    assert cli.main(['run','--json',str(source),'-o',str(tmp_path),'--no-revise','--until','check'])==0
