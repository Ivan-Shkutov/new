import pytest
from pulse_app import import_knowledge as importer


@pytest.mark.parametrize('maximum,expected',[(1,1),(10,2)])
def test_official_crawl_stays_in_scope_and_reports_missing_content(monkeypatch,maximum,expected):
    root='https://workspace.vk.ru/docs/on-premises/vk-teams'
    seen=[];redirects=[]
    class Response:
        def __init__(self,url):self.headers={'Content-Type':'application/pdf' if url.endswith('.pdf') else 'text/html'}
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,size):
            return ('<nav>navigation</nav><script>do not index</script><main><p>'+('Official VK Teams documentation. '*10)+'</p></main><a href="'+root+'/child">child</a><a href="'+root+'/manual.pdf">PDF</a><a href="https://attacker.example/steal">outside</a>').encode()
    class Opener:
        def open(self,request,**kwargs):
            seen.append(request.full_url);return Response(request.full_url)
    def build(handler):redirects.append(handler);return Opener()
    monkeypatch.setattr(importer.urllib.request,'build_opener',build)
    monkeypatch.setattr(importer.time,'sleep',lambda _:None)
    docs,report=importer.crawl(root,'vk-teams-on-premises','unconfirmed',maximum=maximum)
    assert len(docs)==expected and all('navigation' not in d['body'] and 'do not index' not in d['body'] for d in docs)
    assert not report['complete_discovery']
    if maximum==1:assert report['limit_reached']
    else:assert len(report['skipped'])==1
    assert all(url.startswith(root) for url in seen)
    with pytest.raises(ValueError,match='outside'):
        redirects[0].redirect_request(None,None,302,'',{},'https://attacker.example/')
