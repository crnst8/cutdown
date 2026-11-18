from cutdown.config import Config
from cutdown.providers import Provider, request_json, stage_options
from cutdown.providers.api import ApiProvider
import pytest

@pytest.mark.parametrize("wire", ["chat","messages","responses"])
def test_body_stage_overrides_provider(wire):
    p = ApiProvider("test", {"extra_body":{"provider":{"data_collection":"deny"},"reasoning":{"effort":"high"}}})
    body = p._body(wire,"system","user","model",extra_body={"reasoning":{"effort":"low"}})
    assert body['reasoning']['effort']=='low'
    assert body['provider']['data_collection']=='deny'

def test_stage_defaults():
    cfg=Config.load()
    assert stage_options(cfg,'label') == {'extra_body':{'reasoning':{'effort':'low'}}}
    assert stage_options(cfg,'discover') == {'extra_body':None}

def test_request_forwards_options(tmp_path):
    seen=[]
    class Fake(Provider):
        def send(self, **kw):
            seen.append(kw)
            return '{}', {'usage':{'cost':.001,'prompt_tokens':10,'completion_tokens':2}}
    body={'reasoning':{'effort':'low'}}
    request_json(Fake('fake',{}),model='model',system='',user='',schema={'type':'object'},run_dir=tmp_path,tag='test',extra_body=body)
    assert seen[0]['extra_body']==body


def test_empty_stage_body_disables_defaults(tmp_path):
    path=tmp_path/'cutdown.toml'
    path.write_text('[stages]\nlabel = {provider="openrouter", model="openai/gpt-6-luna", extra_body={}}\n')
    assert stage_options(Config.load(path),'label') == {'extra_body':{}}
    cfg=Config.load();cfg.overlay({'stages':{'check':{'extra_body':{}}}})
    assert stage_options(cfg,'check') == {'extra_body':{}}


def test_provider_urls_require_tls_outside_loopback():
    from urllib.request import Request
    from cutdown.providers.api import open_api
    from cutdown.util import CutdownError

    with pytest.raises(CutdownError, match="require HTTPS"):
        open_api(Request("http://example.invalid/v1/models"), timeout=1)
