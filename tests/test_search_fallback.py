import sys
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "vendor"))

"""Search discovery remains bounded, truthful and distinct from source reading."""
from pathlib import Path
import tempfile
import time
import json
import unittest
from unittest.mock import patch
from urllib.parse import quote, parse_qs, urlsplit

from backend.config import Config
from backend.sandbox import ToolError
from backend.search_results import _YahooOrganic, yahoo_search_results
from backend.tools import ToolGateway


def organic(url='https://example.edu/paper', title='Actual paper &amp; evidence'):
    return ('<div class="dd algo relsrch Sr"><div class="compTitle options-toggle">'
            '<a href="'+url+'"><div>Navigation text not the title</div><h3><span>'+title+'</span></h3></a>'
            '</div><div class="compText aAbs"><p>Search excerpt, not the source body.</p></div></div>')


class SearchExtractionTests(unittest.TestCase):
    def test_fragmented_long_title_stops_retaining_fragments_at_label_bound(self):
        parser = _YahooOrganic()
        parser.feed('<div class="algo relsrch"><div class="compTitle"><a href="https://example.edu/paper"><h3>')
        parser.feed('<span>X</span>' * 20000)
        self.assertEqual(parser.active['titleChars'], 1200)
        self.assertEqual(len(parser.active['title']), 1200)
        parser.feed('</h3></a></div></div>')
        self.assertEqual(parser.rows, [{'url': 'https://example.edu/paper', 'title': 'X' * 1200}])

    def test_pathological_page_depth_fails_closed_without_an_unbounded_stack(self):
        page='<div>'*1000+organic()+'</div>'*1000
        self.assertEqual(yahoo_search_results(page,'https://search.yahoo.com/search'),[])

    def test_explicit_ad_or_ai_ancestors_cannot_masquerade_as_organic(self):
        page='<div class="ad">'+organic('https://ad.example/')+'</div>'
        page+='<div class="genaipaa">'+organic('https://ai.example/')+'</div>'+organic()
        self.assertEqual(yahoo_search_results(page,'https://search.yahoo.com/search'),
                         [{'title':'Actual paper & evidence','url':'https://example.edu/paper'}])

    def test_title_must_be_inside_its_corresponding_anchor_not_a_sibling(self):
        wrong='<div class="algo relsrch"><div class="compTitle"><a href="https://wrong.example/">Icon</a><h3>Unrelated heading</h3></div></div>'
        valid='<div class="algo relsrch"><div class="compTitle"><a href="https://icon.example/">Icon</a><a href="https://right.example/"><h3>Real title</h3></a></div></div>'
        self.assertEqual(yahoo_search_results(wrong+valid,'https://search.yahoo.com/search'),
                         [{'title':'Real title','url':'https://right.example/'}])

    def test_only_complete_organic_title_links_not_ai_answers_adverts_or_navigation(self):
        page='<a href="https://nav.example/">Navigation</a>'
        page+='<div class="genaipaa"><h3><a href="https://ai.example/">AI generated answer</a></h3></div>'
        page+='<div class="ad"><div class="compTitle"><a href="https://ad.example/"><h3>Advert</h3></a></div></div>'
        page+=organic()+organic('https://example.edu/paper#another','Duplicate')
        page+='<div class="relsrch"><div class="compTitle"><a href="https://broken.example/"><h3>Incomplete'
        self.assertEqual(yahoo_search_results(page,'https://search.yahoo.com/search'),
                         [{'title':'Actual paper & evidence','url':'https://example.edu/paper'}])

    def test_wrapped_targets_decode_once_and_unsafe_targets_are_not_returned(self):
        wrapped='https://r.search.yahoo.com/path/RU='+quote('https://example.edu/Case?Q=A',safe='')+'/RK=2/RS=token'
        bad=['javascript:alert(1)','https://name:secret@example.edu/','http://127.0.0.1/',
             'http://169.254.169.254/','http://localhost/','https://machine.local/',
             'https://search.yahoo.com/scout/chat','//search.yahoo.com/search', 'https://example.edu/\\bad']
        page=organic(wrapped,'资料')+''.join(organic(url)for url in bad)
        self.assertEqual(yahoo_search_results(page,'https://search.yahoo.com/search'),
                         [{'title':'资料','url':'https://example.edu/Case?Q=A'}])

    def test_bounded_titles_results_and_hidden_script_text(self):
        page=''.join(organic('https://example.edu/'+str(i),'A'*1500+'<script>NOT A TITLE</script>')for i in range(20))
        results=yahoo_search_results(page,'https://search.yahoo.com/search',3)
        self.assertEqual(len(results),3);self.assertTrue(all(len(r['title'])==300 for r in results))
        self.assertTrue(all('NOT A TITLE' not in r['title']for r in results))
        self.assertEqual(yahoo_search_results(organic(title='   '),'https://search.yahoo.com/search'),[])


class SerpSearchTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.config=Config(Path(self.directory.name), {'SERPAPI_API_KEY':'test-serp-secret'})
        self.gateway=ToolGateway(self.config)

    def call(self, **kwargs):
        return self.gateway.execute('research','search-test','web.search',
                                    {'query':'大学 AI & evidence?', 'max_results':2}, **kwargs)

    def test_missing_key_fails_without_any_network_fallback(self):
        self.gateway._serpapi_api_key=''
        with patch.object(self.gateway,'_serp_response') as request:
            with self.assertRaises(ToolError) as caught:self.call()
        self.assertEqual(caught.exception.code,'configuration');request.assert_not_called()

    def test_organic_results_are_bounded_safe_deduplicated_and_unfetched(self):
        rows=[{'title':'Paper','link':'https://example.edu/paper','snippet':'unverified'},
              {'title':'Duplicate','link':'https://example.edu/paper#fragment'},
              {'title':'Bad','link':'http://127.0.0.1/'},
              {'title':'Credentials','link':'https://user:secret@example.edu/'},
              {'title':'Leak','link':'https://example.edu/test-serp-secret'},
              {'title':'Other','link':'https://example.org/other'},
              {'title':'Extra','link':'https://example.net/extra'}]
        with patch.object(self.gateway,'_serp_response',return_value={'organic_results':rows}) as request:
            result=self.call()
        request.assert_called_once();self.assertEqual(result['provider'],'SerpAPI')
        self.assertEqual([r['url'] for r in result['results']],['https://example.edu/paper','https://example.org/other'])
        self.assertTrue(result['resultsAreUnfetched']);self.assertNotIn('snippet',result['results'][0])
        self.assertNotIn('sources',result)
        self.assertNotIn('test-serp-secret',str(result));self.assertNotIn('api_key',result['sourceUrl'])

    def test_google_redirects_keep_titles_without_unfetchable_urls(self):
        goto='https://www.google.com/goto?url='+'A'*400
        rows=[{'title':'Redirected event','link':goto},
              {'title':'Direct event','link':'https://example.edu/event'}]
        with patch.object(self.gateway,'_serp_response',return_value={'organic_results':rows}):
            result=self.gateway.execute('research','search-test','web.search',{'query':'HKUST event','max_results':5})
        self.assertEqual(result['results'],[{'title':'Direct event','url':'https://example.edu/event'}])
        self.assertEqual(result['unresolvedRedirectTitles'],['Redirected event'])
        self.assertNotIn(goto,str(result))

    def test_real_empty_results_are_not_provider_failure(self):
        with patch.object(self.gateway,'_serp_response',return_value={'organic_results':[]}):result=self.call()
        self.assertTrue(result['ok']);self.assertEqual(result['results'],[])

    def test_api_error_is_not_a_success_or_fallback(self):
        with patch.object(self.gateway,'_serp_response',return_value={'error':True}) as request:
            with self.assertRaises(ToolError) as caught:self.call()
        self.assertEqual(caught.exception.code,'search_unavailable');request.assert_called_once()

    def test_malformed_results_are_not_success(self):
        with patch.object(self.gateway,'_serp_response',return_value={'organic_results':{}}):
            with self.assertRaises(ToolError):self.call()

    def test_total_deadline_is_not_reset(self):
        with patch.object(self.gateway,'_serp_response') as request:
            with self.assertRaises(ToolError) as caught:self.call(deadline=time.monotonic()-1)
        self.assertEqual(caught.exception.code,'deadline');request.assert_not_called()

    def test_secret_is_scrubbed_from_nested_public_state(self):
        value={'url':'https://serpapi.com/?api_key=test-serp-secret','items':['test-serp-secret']}
        result=self.config.scrub(value)
        self.assertNotIn('test-serp-secret',str(result))
        self.assertEqual(result['items'],['[REDACTED]'])

    def test_worker_receives_secret_only_in_environment(self):
        from backend.tools import _SERP_SEARCH_WORKER
        payload={'payload':{'organic_results':[{'title':'Paper','link':'https://example.edu/'}]}}
        completed={'timedOut':False,'exitCode':0,'outputLimitExceeded':False,'stdout':json.dumps(payload)}
        with patch('backend.tools.run_process',return_value=completed) as process:
            self.call()
        args,kwargs=process.call_args
        self.assertNotIn('test-serp-secret',str(args));self.assertEqual(kwargs['env']['SERPAPI_API_KEY'],'test-serp-secret')
        self.assertIn('_reject_redirects=True',_SERP_SEARCH_WORKER)
        with patch.dict('os.environ', {'SERPAPI_API_KEY':'test-serp-secret'}):
            self.assertNotIn('SERPAPI_API_KEY',self.gateway.sandbox._environment())

    def test_invalid_json_and_failed_transport_do_not_expose_raw_output(self):
        for stdout,exit_code in [('test-serp-secret',0),('',1)]:
            completed={'timedOut':False,'exitCode':exit_code,'outputLimitExceeded':False,'stdout':stdout}
            with patch('backend.tools.run_process',return_value=completed):
                with self.assertRaises(ToolError) as caught:self.call()
            self.assertNotIn('test-serp-secret',str(caught.exception))


if __name__=='__main__':unittest.main()
