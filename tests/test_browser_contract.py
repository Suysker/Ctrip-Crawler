"""Task-bound request capture and stable browser identity contracts."""
import copy
import asyncio
import importlib.util
import os
import sys
import types
from unittest.mock import patch
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def load_browser():
    temporary = tempfile.TemporaryDirectory()
    path = Path(temporary.name) / 'browser_contract.py'
    header = (ROOT / 'source_sections/imports.py.inc').read_text(encoding='utf-8') + '\n'
    path.write_text(header + (ROOT / 'source_sections/processing.py.inc').read_text(encoding='utf-8') + '\n'
                    + (ROOT / 'source_sections/browser.py.inc').read_text(encoding='utf-8'), encoding='utf-8')
    spec=importlib.util.spec_from_file_location('browser_contract',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return temporary,module


def task():
    return {'origin':{'code':'SHA','name':'上海'},'destination':{'code':'HKG','name':'香港'},
            'departure_date':'2027-01-10','index':0,'key':'task','run_id':'run'}


def body():
    return {'flightSegments':[{'departureCityCode':'SHA','arrivalCityCode':'HKG',
                              'departureDate':'2027-01-10'}],
            'adultCount':1,'childCount':0,'infantCount':0,'cabin':'Y_S_C_F','flightWay':'S',
            'transactionID':'query-1'}


class BrowserContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary,cls.module=load_browser()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_city_display_names_do_not_define_query_identity(self):
        candidate=body()
        candidate['flightSegments'][0].update(departureCityName='上海市',arrivalCityName='中国香港')
        self.assertTrue(self.module.search_request_matches(candidate,task()))

    def test_different_route_or_date_is_never_current_result(self):
        for key,value in [('arrivalCityCode','SNN'),('departureCityCode','PEK'),('departureDate','2027-01-11')]:
            candidate=body();candidate['flightSegments'][0][key]=value
            self.assertFalse(self.module.search_request_matches(candidate,task()))

    def test_multiple_segments_are_not_a_single_direction_query(self):
        candidate=body();candidate['flightSegments'].append(copy.deepcopy(candidate['flightSegments'][0]))
        self.assertFalse(self.module.search_request_matches(candidate,task()))

    def test_different_passenger_conditions_are_rejected(self):
        candidate=body();candidate['adultCount']=2
        self.assertFalse(self.module.search_request_matches(candidate,task()))

    def test_missing_identity_is_not_permissively_coerced(self):
        candidate=body();del candidate['flightSegments'][0]['arrivalCityCode']
        self.assertFalse(self.module.search_request_matches(candidate,task()))
        self.assertFalse(self.module.search_request_matches(None,task()))

    def test_launch_replay_does_not_leak_parent_secrets_or_change_identity(self):
        identity={'schema':1,'platform':self.module.platform.system(),
                  'browser_build':self.module.CAMOUFOX_BUILD,
                  'launch':{'env':{'CAMOU_CONFIG_1':'{"audio:seed":123}'},
                            'args':[],'firefox_user_prefs':{},'executable_path':'browser'}}
        original=copy.deepcopy(identity)
        options=self.module.identity_launch_options(identity,{'headless':True})
        self.assertEqual(identity,original)
        self.assertEqual(options['env']['CAMOU_CONFIG_1'],identity['launch']['env']['CAMOU_CONFIG_1'])
        self.assertTrue(options['headless'])
        options['env']['CAMOU_CONFIG_1']='modified'
        self.assertEqual(identity,original)

    def test_identity_generation_resolves_every_asset_against_the_pinned_binary(self):
        captured={}
        def launch_options(**kwargs):
            captured.update(kwargs)
            return {'env':{},'args':[],'firefox_user_prefs':{}}
        modules={
            'camoufox.fingerprints':types.SimpleNamespace(get_random_preset=lambda **kw:{'navigator':{'userAgent':'fixed'}}),
            'camoufox.addons':types.SimpleNamespace(DefaultAddons=()),
            'camoufox.multiversion':types.SimpleNamespace(find_installed_version=lambda spec:Path('/pinned-browser')),
            'camoufox.pkgman':types.SimpleNamespace(launch_path=lambda path:str(path/'camoufox')),
            'camoufox.utils':types.SimpleNamespace(launch_options=launch_options),
        }
        with patch.dict(sys.modules,modules):
            self.module.generate_identity({'headless':True})
        self.assertEqual(captured.get('executable_path'),str(Path('/pinned-browser')/'camoufox'))

    def test_cross_platform_identity_is_explicitly_rejected(self):
        identity={'schema':1,'platform':'different','browser_build':self.module.CAMOUFOX_BUILD,'launch':{}}
        with self.assertRaises(self.module.CrawlerError) as error:
            self.module.identity_launch_options(identity,{})
        self.assertEqual(error.exception.code,'identity_incompatible')

    def test_partial_search_is_not_success_and_pull_must_follow_its_query(self):
        chain=self.module.SearchChain(task())
        partial={'status':0,'data':{'context':{'searchId':'search-1','finished':False},'flightItineraryList':[]}}
        complete={'status':0,'data':{'context':{'searchId':'search-2','finished':True},'flightItineraryList':[]}}
        self.assertIsNone(chain.accept(self.module.SEARCH_API_PATH,'POST',body(),200,partial))
        self.assertIsNone(chain.accept('/international/search/api/search/pull/other','POST',body(),200,complete))
        self.assertIs(chain.accept('/international/search/api/search/pull/search-1','POST',body(),200,complete),complete)

    def test_missing_completion_marker_is_protocol_error(self):
        chain=self.module.SearchChain(task())
        payload={'status':0,'data':{'context':{'searchId':'search-1'},'flightItineraryList':[]}}
        with self.assertRaises(self.module.CrawlerError) as error:
            chain.accept(self.module.SEARCH_API_PATH,'POST',body(),200,payload)
        self.assertEqual(error.exception.code,'protocol_changed')

    def test_completion_only_pull_finishes_the_latest_snapshot_without_merging_itineraries(self):
        chain=self.module.SearchChain(task())
        first={'status':0,'data':{'context':{'searchId':'search-1','finished':False},
                                'flightItineraryList':[{'itineraryId':'obsolete'}]}}
        latest={'status':0,'data':{'context':{'searchId':'search-2','finished':False},
                                 'flightItineraryList':[{'itineraryId':'latest'}]}}
        notice={'status':0,'data':{'context':{'searchId':'search-3','finished':True,'flag':0}}}
        originals=copy.deepcopy([first,latest,notice])
        chain.accept(self.module.SEARCH_API_PATH,'POST',body(),200,first)
        self.assertIsNone(chain.accept('/international/search/api/search/pull/search-1','POST',body(),200,latest))
        result=chain.accept('/international/search/api/search/pull/search-2','POST',body(),200,notice)
        self.assertEqual(result['data']['flightItineraryList'],[{'itineraryId':'latest'}])
        self.assertEqual(result['data']['context'],notice['data']['context'])
        self.assertEqual([first,latest,notice],originals)

    def test_empty_final_pull_completes_but_does_not_erase_an_earlier_nonempty_snapshot(self):
        chain=self.module.SearchChain(task())
        partial={'status':0,'data':{'context':{'searchId':'search-1','finished':False},
                                  'flightItineraryList':[{'itineraryId':'previous'}]}}
        empty={'status':0,'data':{'context':{'searchId':'search-2','finished':True},'flightItineraryList':[]}}
        chain.accept(self.module.SEARCH_API_PATH,'POST',body(),200,partial)
        self.assertEqual(chain.accept('/international/search/api/search/pull/search-1','POST',body(),200,empty)
                         ['data']['flightItineraryList'],[{'itineraryId':'previous'}])

    def test_empty_unfinished_pull_does_not_erase_the_snapshot_before_completion(self):
        chain=self.module.SearchChain(task())
        partial={'status':0,'data':{'context':{'searchId':'search-1','finished':False},
                                  'flightItineraryList':[{'itineraryId':'previous'}]}}
        empty={'status':0,'data':{'context':{'searchId':'search-2','finished':False},'flightItineraryList':[]}}
        notice={'status':0,'data':{'context':{'searchId':'search-3','finished':True}}}
        chain.accept(self.module.SEARCH_API_PATH,'POST',body(),200,partial)
        self.assertIsNone(chain.accept('/international/search/api/search/pull/search-1','POST',body(),200,empty))
        result=chain.accept('/international/search/api/search/pull/search-2','POST',body(),200,notice)
        self.assertEqual(result['data']['flightItineraryList'],[{'itineraryId':'previous'}])

    def test_explicit_empty_finished_initial_response_is_a_valid_empty_result(self):
        chain=self.module.SearchChain(task())
        empty={'status':0,'data':{'context':{'searchId':'search-1','finished':True},'flightItineraryList':[]}}
        self.assertIs(chain.accept(self.module.SEARCH_API_PATH,'POST',body(),200,empty),empty)

    def test_null_final_list_is_invalid_and_never_reuses_an_earlier_snapshot(self):
        chain=self.module.SearchChain(task())
        partial={'status':0,'data':{'context':{'searchId':'search-1','finished':False},'flightItineraryList':[]}}
        invalid={'status':0,'data':{'context':{'searchId':'search-2','finished':True},'flightItineraryList':None}}
        chain.accept(self.module.SEARCH_API_PATH,'POST',body(),200,partial)
        with self.assertRaises(self.module.CrawlerError) as error:
            chain.accept('/international/search/api/search/pull/search-1','POST',body(),200,invalid)
        self.assertEqual(error.exception.code,'protocol_changed')

    def test_initial_pending_notice_waits_for_a_later_valid_snapshot(self):
        chain=self.module.SearchChain(task())
        pending={'status':0,'data':{'context':{'searchId':'search-1','finished':False}}}
        complete={'status':0,'data':{'context':{'searchId':'search-2','finished':True},
                                    'flightItineraryList':[{'itineraryId':'current'}]}}
        self.assertIsNone(chain.accept(self.module.SEARCH_API_PATH,'POST',body(),200,pending))
        self.assertIsNone(chain.itineraries)
        self.assertIs(chain.accept('/international/search/api/search/pull/search-1','POST',body(),200,complete),complete)

    def test_intermediate_pending_notice_retains_the_known_snapshot_until_completion(self):
        for snapshot in ([],[{'itineraryId':'current'}]):
            with self.subTest(snapshot=snapshot):
                chain=self.module.SearchChain(task())
                first={'status':0,'data':{'context':{'searchId':'search-1','finished':False},'flightItineraryList':snapshot}}
                pending={'status':0,'data':{'context':{'searchId':'search-2','finished':False}}}
                complete={'status':0,'data':{'context':{'searchId':'search-3','finished':True}}}
                chain.accept(self.module.SEARCH_API_PATH,'POST',body(),200,first)
                self.assertIsNone(chain.accept('/international/search/api/search/pull/search-1','POST',body(),200,pending))
                result=chain.accept('/international/search/api/search/pull/search-2','POST',body(),200,complete)
                self.assertEqual(result['data']['flightItineraryList'],snapshot)

    def test_unrelated_pending_notice_cannot_advance_the_current_chain(self):
        chain=self.module.SearchChain(task())
        first={'status':0,'data':{'context':{'searchId':'search-1','finished':False}}}
        unrelated={'status':0,'data':{'context':{'searchId':'unrelated','finished':False}}}
        complete={'status':0,'data':{'context':{'searchId':'search-2','finished':True},'flightItineraryList':[]}}
        chain.accept(self.module.SEARCH_API_PATH,'POST',body(),200,first)
        wrong=body();wrong['transactionID']='other'
        self.assertIsNone(chain.accept('/international/search/api/search/pull/search-1','POST',wrong,200,unrelated))
        self.assertIsNone(chain.accept('/international/search/api/search/pull/other','POST',body(),200,unrelated))
        self.assertIs(chain.accept('/international/search/api/search/pull/search-1','POST',body(),200,complete),complete)

    def test_finished_notice_without_any_known_snapshot_is_still_invalid(self):
        for initially_pending in (False,True):
            with self.subTest(initially_pending=initially_pending):
                chain=self.module.SearchChain(task())
                path=self.module.SEARCH_API_PATH
                if initially_pending:
                    pending={'status':0,'data':{'context':{'searchId':'search-1','finished':False}}}
                    chain.accept(path,'POST',body(),200,pending)
                    path='/international/search/api/search/pull/search-1'
                missing={'status':0,'data':{'context':{'searchId':'search-2','finished':True}}}
                with self.assertRaises(self.module.CrawlerError) as error:
                    chain.accept(path,'POST',body(),200,missing)
                self.assertEqual(error.exception.code,'protocol_changed')

    def test_completion_notice_confirms_an_already_explicit_empty_snapshot(self):
        chain=self.module.SearchChain(task())
        partial={'status':0,'data':{'context':{'searchId':'search-1','finished':False},'flightItineraryList':[]}}
        notice={'status':0,'data':{'context':{'searchId':'search-2','finished':True}}}
        chain.accept(self.module.SEARCH_API_PATH,'POST',body(),200,partial)
        result=chain.accept('/international/search/api/search/pull/search-1','POST',body(),200,notice)
        self.assertEqual(result['data']['flightItineraryList'],[])
        self.assertIs(result['data']['context']['finished'],True)

    def test_unrelated_completion_notice_does_not_finish_or_replace_current_snapshot(self):
        chain=self.module.SearchChain(task())
        partial={'status':0,'data':{'context':{'searchId':'search-1','finished':False},
                                  'flightItineraryList':[{'itineraryId':'current'}]}}
        notice={'status':0,'data':{'context':{'searchId':'search-2','finished':True}}}
        chain.accept(self.module.SEARCH_API_PATH,'POST',body(),200,partial)
        wrong=body();wrong['transactionID']='other'
        self.assertIsNone(chain.accept('/international/search/api/search/pull/search-1','POST',wrong,200,notice))
        self.assertIsNone(chain.accept('/international/search/api/search/pull/unrelated','POST',body(),200,notice))
        result=chain.accept('/international/search/api/search/pull/search-1','POST',body(),200,notice)
        self.assertEqual(result['data']['flightItineraryList'],[{'itineraryId':'current'}])

    def test_authentication_requirement_is_not_empty_success(self):
        chain=self.module.SearchChain(task())
        payload={'status':0,'data':{'needUserLogin':True,'context':{'finished':True,'searchId':'query'}}}
        with self.assertRaises(self.module.CrawlerError) as error:
            chain.accept(self.module.SEARCH_API_PATH,'POST',body(),200,payload)
        self.assertEqual(error.exception.code,'needs_auth')

    def test_pull_from_different_transaction_cannot_finish_current_task(self):
        chain=self.module.SearchChain(task())
        partial={'status':0,'data':{'context':{'searchId':'search-1','finished':False},'flightItineraryList':[]}}
        complete={'status':0,'data':{'context':{'searchId':'search-2','finished':True},'flightItineraryList':[]}}
        chain.accept(self.module.SEARCH_API_PATH,'POST',body(),200,partial)
        wrong=body();wrong['transactionID']='query-2'
        self.assertIsNone(chain.accept('/international/search/api/search/pull/search-1','POST',wrong,200,complete))

    def test_city_alias_uses_server_city_identity_without_local_alias_table(self):
        payload={'Status':0,'Key':'香港','Data':[{'Type':5,'Code':'HKG','CityId':58,
                 'Name':'中国香港','canSelect':True}, {'Type':3,'Code':'HKG','Name':'香港国际机场','canSelect':True}]}
        self.assertEqual(self.module.resolve_city_payload(payload,'香港'),{'code':'HKG','name':'中国香港'})

    def test_ambiguous_city_suggestion_does_not_choose_first(self):
        payload={'Status':0,'Key':'Springfield','Data':[
            {'Type':5,'Code':'AAA','CityId':1,'Name':'Springfield one','canSelect':True},
            {'Type':5,'Code':'BBB','CityId':2,'Name':'Springfield two','canSelect':True}]}
        with self.assertRaises(self.module.CrawlerError) as error:
            self.module.resolve_city_payload(payload,'Springfield')
        self.assertEqual(error.exception.code,'city_ambiguous')

    def test_login_requires_member_identity_not_just_an_unchanged_url(self):
        self.assertFalse(self.module.member_authenticated({'ResponseStatus':{'Ack':'Success'},'uid':''}))
        self.assertFalse(self.module.member_authenticated({'uid':'member'}))
        self.assertTrue(self.module.member_authenticated({'ResponseStatus':{'Ack':'Success','Errors':[]},
                                                          'result':{'resultCode':0},'uid':None}))

    def test_verified_member_response_accepts_the_sites_nullable_uid(self):
        # Minimal public fixture derived from a normally verified member response.
        verified={'ResponseStatus':{'Ack':'Success','Errors':[]},'uid':None,'userName':'',
                  'result':{'resultCode':0,'resultMsg':''}}
        self.assertTrue(self.module.member_authenticated(verified))
        anonymous={'ResponseStatus':{'Ack':'Failure','Errors':[{'ErrorCode':'NotAuthenticated'}]},
                   'uid':None,'userName':'','result':None}
        self.assertFalse(self.module.member_authenticated(anonymous))

    def test_member_transport_success_requires_explicit_empty_errors_and_business_success(self):
        for status in [{'Ack':'Success'}, {'Ack':'Success','Errors':None},
                       {'Ack':'Success','Errors':[{'ErrorCode':'Failure'}]}]:
            self.assertFalse(self.module.member_authenticated({'ResponseStatus':status,'result':{'resultCode':0}}))
        self.assertFalse(self.module.member_authenticated({'ResponseStatus':{'Ack':'Success','Errors':[]},
                                                           'result':{'resultCode':1}}))

    def test_member_confirmation_only_uses_the_site_member_api(self):
        path='/restapi/soa2/15201/getMemberSummaryInfo'
        self.assertTrue(self.module.is_member_summary_request('https://m.ctrip.com'+path,'POST'))
        self.assertFalse(self.module.is_member_summary_request('https://example.invalid'+path,'POST'))
        self.assertFalse(self.module.is_member_summary_request('https://m.ctrip.com'+path,'GET'))

    def test_comfort_identity_requires_result_flight_and_current_route_and_date(self):
        aliases=self.module.comfort_target_aliases([
            {'航班号':'MU1234','实际承运航班号':'FM4567'},
            {'flightNo':'MU4321','operateFlightNo':'FM4567'}])
        request={'departureCityCode':'SHA','arrivalCityCode':'HKG','departureDate':'2027-01-10',
                 'flightNoList':['MU1234'],'cabinList':['Y']}
        self.assertEqual(self.module.comfort_request_identity(request,task(),aliases)[0],'FM4567')
        for key,value in [('arrivalCityCode','TYO'),('departureDate','2027-01-11'),('flightNoList',['UNKNOWN'])]:
            wrong=dict(request,**{key:value})
            self.assertIsNone(self.module.comfort_request_identity(wrong,task(),aliases))


@unittest.skipUnless(os.getenv('CTRIP_BROWSER_TESTS') == '1', 'Requires the explicitly installed Camoufox runtime')
class LoginDOMContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_authentication_preserves_unconfirmed_candidate_before_timeout(self):
        temporary,module=load_browser()
        checkpoints=[]
        session=module.BrowserSession({'headless':True,'action_timeout':1},
            module.generate_identity({'headless':True}),{},lambda kind,value:checkpoints.append((kind,value)))
        try:
            await session.start()
            session.challenge_page=await session.context.new_page()
            await session.challenge_page.set_content('<div id="sc_mobilevalidate">Local verification fixture</div>')
            session.challenge_handle='local'
            session.challenge_expires_at=module.time.monotonic()+5
            session.config.update(headless=False,challenge_policy='wait_then_switch')
            with self.assertRaises(module.CrawlerError) as error:
                await session.authenticate({'username':'local'},0.7,challenge_handle='local')
            self.assertEqual(error.exception.code,'auth_timeout')
            self.assertTrue(checkpoints)
            self.assertTrue(all(kind=='auth_checkpoint' and value['confirmed'] is False for kind,value in checkpoints))
        finally:
            await session.close()
            temporary.cleanup()


    async def test_styled_agreement_uses_visible_label_without_forced_input_click(self):
        temporary,module=load_browser()
        session=module.BrowserSession({'headless':True},module.generate_identity({'headless':True}),{},None)
        try:
            await session.start()
            page=await session.context.new_page()
            await page.set_content('''<input type="checkbox" id="checkboxAgreementInput" style="display:none">
                <label for="checkboxAgreementInput">Accept the local test agreement</label>''')
            await module.accept_login_agreement(page,1000)
            self.assertTrue(await page.locator('#checkboxAgreementInput').is_checked())
            await module.accept_login_agreement(page,1000)
            self.assertTrue(await page.locator('#checkboxAgreementInput').is_checked())
        finally:
            await session.close()
            temporary.cleanup()

    async def test_existing_member_session_is_confirmed_before_login_form_is_used(self):
        temporary,module=load_browser()
        session=module.BrowserSession({'headless':True,'action_timeout':1},
                                      module.generate_identity({'headless':True}),{},None)
        try:
            await session.start()
            await session.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body='Local contract fixture'))
            await session.context.route('https://flights.ctrip.com/online/channel/domestic',
                lambda route: route.fulfill(content_type='text/html',body='''<script>
                    fetch('https://m.ctrip.com/restapi/soa2/15201/getMemberSummaryInfo',{method:'POST'});
                </script>'''))
            await session.context.route('**/getMemberSummaryInfo',lambda route:route.fulfill(
                content_type='application/json',headers={'Access-Control-Allow-Origin':'*'},body='{"ResponseStatus":{"Ack":"Success","Errors":[]},"uid":null,"result":{"resultCode":0}}'))
            for account in ({'username':'unused','password':'unused'}, {'username':'unused'}):
                result=await session.authenticate(account,10)
                self.assertTrue(result['authenticated'])
        finally:
            await session.close()
            temporary.cleanup()

    async def test_invalid_member_session_without_password_remains_needs_auth(self):
        temporary,module=load_browser()
        session=module.BrowserSession({'headless':True,'action_timeout':1},
                                      module.generate_identity({'headless':True}),{},None)
        try:
            await session.start()
            await session.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body='Local fixture'))
            await session.context.route('https://flights.ctrip.com/online/channel/domestic',
                lambda route:route.fulfill(content_type='text/html',body="""<script>
                    fetch('https://m.ctrip.com/restapi/soa2/15201/getMemberSummaryInfo',{method:'POST'});
                </script>"""))
            await session.context.route('**/getMemberSummaryInfo',lambda route:route.fulfill(
                content_type='application/json',headers={'Access-Control-Allow-Origin':'*'},
                body='{"ResponseStatus":{"Ack":"Failure","Errors":[{}]},"result":null}'))
            with self.assertRaises(module.CrawlerError) as error:
                await session.authenticate({'username':'unused'},10)
            self.assertEqual(error.exception.code,'needs_auth')
        finally:
            await session.close();temporary.cleanup()

    async def test_member_rate_limit_preserves_server_wait_before_login_form(self):
        temporary,module=load_browser()
        session=module.BrowserSession({'headless':True,'action_timeout':1},
                                      module.generate_identity({'headless':True}),{},None)
        login_visits=[]
        try:
            await session.start()
            await session.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body='Local fixture'))
            await session.context.route('https://flights.ctrip.com/online/channel/domestic',
                lambda route:route.fulfill(content_type='text/html',body="""<script>
                    fetch('https://m.ctrip.com/restapi/soa2/15201/getMemberSummaryInfo',{method:'POST'});
                </script>"""))
            await session.context.route('**/getMemberSummaryInfo',lambda route:route.fulfill(
                status=429,content_type='application/json',headers={'Access-Control-Allow-Origin':'*','Retry-After':'3600'},body='{}'))
            async def login(route):
                login_visits.append(route.request.url)
                await route.fulfill(content_type='text/html',body='Local login fixture')
            await session.context.route('https://passport.ctrip.com/user/login?*',login)
            with self.assertRaises(module.CrawlerError) as error:
                await session.authenticate({'username':'unused','password':'unused'},10)
            self.assertEqual(error.exception.code,'rate_limited')
            self.assertEqual(error.exception.retry_after,3600)
            self.assertEqual(login_visits,[])
        finally:
            await session.close();temporary.cleanup()


@unittest.skipUnless(os.getenv('CTRIP_BROWSER_TESTS') == '1', 'Requires the explicitly installed Camoufox runtime')
class LookupDOMContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_city_rate_limit_preserves_server_retry_after(self):
        temporary,module=load_browser()
        session=module.BrowserSession({'headless':True},module.generate_identity({'headless':True}),{},None)
        try:
            await session.start()
            await session.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body='Local fixture'))
            await session.context.route('**/poi/search?*',lambda route:route.fulfill(
                status=429,content_type='application/json',headers={'Retry-After':'3600'},body='{}'))
            with self.assertRaises(module.CrawlerError) as error:
                await session.resolve_cities(['上海'])
            self.assertEqual(error.exception.code,'rate_limited')
            self.assertEqual(error.exception.retry_after,3600)
        finally:
            await session.close();temporary.cleanup()


class CollectBoundaryContractTests(unittest.IsolatedAsyncioTestCase):
    async def collect_responses(self, responses, deadline_seconds=30, config=None, capture_comfort=False):
        temporary,module=load_browser()
        self.read_bodies=[]
        self.detail_ui_calls=[]
        read_bodies=self.read_bodies
        detail_ui_calls=self.detail_ui_calls
        class Request:
            def __init__(self, payload, method, url):
                self.post_data_json=payload;self.method=method;self.url=url
        class Response:
            def __init__(self, path, payload, status, response_payload, method='POST'):
                self.url='https://flights.ctrip.com'+path;self.request=Request(payload,method,self.url)
                self.status=status;self.headers={};self.payload=response_payload
            async def body(self):
                read_bodies.append(self.url)
                return json.dumps(self.payload).encode()
        class Page:
            def __init__(self):self.callbacks={}
            def on(self, kind, callback):self.callbacks[kind]=callback
            def remove_listener(self,*args):pass
            async def goto(self,*args,**kwargs):
                for args in responses:
                    response=Response(*args)
                    self.callbacks['request'](response.request)
                    self.callbacks['response'](response)
            def locator(self,*args):
                detail_ui_calls.append(args)
                raise AssertionError('This fixture must not trigger detail UI')
            async def close(self):pass
        class Context:
            async def new_page(self):return Page()
        session=module.BrowserSession(config or {}, {}, {}, None);session.context=Context()
        if capture_comfort:
            async def collect_comfort(page, task, rows, queue, deadline, errors, traffic):
                self.comfort_window=deadline-module.time.monotonic()
                return module.apply_comfort(rows,[])
            session._collect_comfort=collect_comfort
        try:return await session.collect(task(),deadline_seconds)
        finally:temporary.cleanup()

    async def test_unrelated_failed_initial_response_cannot_interrupt_current_query(self):
        wrong=body();wrong['flightSegments'][0]['departureDate']='2027-01-11'
        complete={'status':0,'data':{'context':{'searchId':'done','finished':True},'flightItineraryList':[]}}
        result=await self.collect_responses([
            ('/international/search/api/search/batchSearch',wrong,503,None),
            ('/international/search/api/search/batchSearch',body(),200,complete)])
        self.assertEqual(result['status'],'empty')

    async def test_only_pending_notices_reach_the_attempt_timeout_without_publishing(self):
        first={'status':0,'data':{'context':{'searchId':'search-1','finished':False}}}
        next_notice={'status':0,'data':{'context':{'searchId':'search-2','finished':False}}}
        with self.assertRaises(Exception) as error:
            await self.collect_responses([
                ('/international/search/api/search/batchSearch',body(),200,first),
                ('/international/search/api/search/pull/search-1',body(),200,next_notice)],deadline_seconds=10.1)
        self.assertEqual(getattr(error.exception,'code',None),'response_timeout')
        self.assertEqual(len(self.read_bodies),2)

    async def test_comfort_budget_changes_the_window_without_extending_the_attempt(self):
        from test_processing import itinerary
        complete={'status':0,'data':{'context':{'searchId':'done','finished':True},'flightItineraryList':[itinerary()]}}
        for configured,attempt,expected in [(0.5,90,0.5),(25,90,25),(1000,12,2)]:
            with self.subTest(configured=configured,attempt=attempt):
                await self.collect_responses([
                    ('/international/search/api/search/batchSearch',body(),200,complete)],deadline_seconds=attempt,
                    config={'comft_flight':True,'comfort_timeout':configured},capture_comfort=True)
                self.assertAlmostEqual(self.comfort_window,expected,delta=0.1)

    async def test_comfort_budget_defaults_to_fifteen_for_an_existing_config(self):
        from test_processing import itinerary
        complete={'status':0,'data':{'context':{'searchId':'done','finished':True},'flightItineraryList':[itinerary()]}}
        await self.collect_responses([
            ('/international/search/api/search/batchSearch',body(),200,complete)],deadline_seconds=90,
            config={'comft_flight':True},capture_comfort=True)
        self.assertAlmostEqual(self.comfort_window,15,delta=0.1)

    async def test_zero_comfort_budget_delivers_quotes_without_triggering_details(self):
        from test_processing import itinerary
        complete={'status':0,'data':{'context':{'searchId':'done','finished':True},'flightItineraryList':[itinerary()]}}
        result=await self.collect_responses([
            ('/international/search/api/search/batchSearch',body(),200,complete)],deadline_seconds=90,
            config={'comft_flight':True,'comfort_timeout':0})
        self.assertEqual(result['status'],'succeeded')
        self.assertEqual(result['comfort_status'],'unavailable')
        self.assertEqual(self.detail_ui_calls,[])

    async def test_unrelated_pull_path_method_and_transaction_are_filtered_before_status(self):
        partial={'status':0,'data':{'context':{'searchId':'current','finished':False},'flightItineraryList':[]}}
        complete={'status':0,'data':{'context':{'searchId':'done','finished':True},'flightItineraryList':[]}}
        wrong=body();wrong['transactionID']='old'
        result=await self.collect_responses([
            ('/international/search/api/search/batchSearch',body(),200,partial),
            ('/international/search/api/search/pull/old',body(),503,None),
            ('/international/search/api/search/pull/current',body(),503,None,'GET'),
            ('/international/search/api/search/pull/current',wrong,429,None),
            ('/international/search/api/search/pull/current',body(),200,complete)])
        self.assertEqual(result['status'],'empty')


@unittest.skipUnless(os.getenv('CTRIP_BROWSER_TESTS') == '1', 'Requires the explicitly installed Camoufox runtime')
class NavigationDOMContractTests(unittest.IsolatedAsyncioTestCase):
    async def local_session(self, *, refusal=None):
        from test_processing import itinerary
        temporary,module=load_browser()
        session=module.BrowserSession({'headless':True,'navigation_timeout':1},
                                      module.generate_identity({'headless':True}),{},None)
        await session.start()
        release=asyncio.Event();before_close=[]
        complete={'status':0,'data':{'context':{'searchId':'done','finished':True},'flightItineraryList':[itinerary()]}}
        html='''<!doctype html><html data-fixture-dcl="no"><script>
            document.addEventListener('DOMContentLoaded',()=>document.documentElement.dataset.fixtureDcl='yes');
            fetch('/international/search/api/search/batchSearch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(QUERY)});
            </script><script defer src="/blocked-defer.js"></script><body>Local fixture</body></html>'''.replace('QUERY',json.dumps(body()))
        await session.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body='Local fixture'))
        async def navigation(route):
            if refusal=='abort':await route.abort('failed')
            else:await route.fulfill(status=refusal or 200,content_type='text/html',body=html if not refusal else 'Denied')
        await session.context.route('https://flights.ctrip.com/online/list/**',navigation)
        await session.context.route('**/batchSearch',lambda route:route.fulfill(content_type='application/json',body=json.dumps(complete)))
        async def deferred(route):
            await release.wait()
            await route.fulfill(content_type='application/javascript',body='')
        await session.context.route('**/blocked-defer.js',deferred)
        new_page=session.context.new_page
        async def instrumented_page():
            page=await new_page();close=page.close
            async def close_fixture():
                before_close.append(await page.locator('html').get_attribute('data-fixture-dcl'))
                release.set()
                await close()
            page.close=close_fixture
            return page
        session.context.new_page=instrumented_page
        return temporary,module,session,release,before_close

    async def test_valid_search_can_complete_while_domcontentloaded_is_blocked(self):
        temporary,module,session,release,before_close=await self.local_session()
        try:
            result=await session.collect(task(),12)
            self.assertEqual(result['status'],'succeeded')
            self.assertEqual(len(result['rows']),1)
            self.assertEqual(before_close,['no'])
        finally:
            release.set();await session.close();temporary.cleanup()

    async def test_real_navigation_abort_is_not_suppressed(self):
        from playwright.async_api import Error as BrowserError
        temporary,module,session,release,before_close=await self.local_session(refusal='abort')
        try:
            with self.assertRaises(BrowserError) as error:
                await session.collect(task(),12)
            self.assertEqual(error.exception.diagnostic['network']['response_kind'],'navigation')
            self.assertTrue(error.exception.diagnostic['network']['request_failed'])
        finally:
            release.set();await session.close();temporary.cleanup()

    async def test_http_navigation_refusal_is_reported_without_waiting_for_search(self):
        temporary,module,session,release,before_close=await self.local_session(refusal=403)
        try:
            with self.assertRaises(module.CrawlerError) as error:
                await session.collect(task(),12)
            self.assertEqual(error.exception.code,'site_blocked')
            self.assertEqual(error.exception.diagnostic['network']['http_status'],403)
        finally:
            release.set();await session.close();temporary.cleanup()


@unittest.skipUnless(os.getenv('CTRIP_BROWSER_TESTS') == '1', 'Requires the explicitly installed Camoufox runtime')
class ComfortDOMContractTests(unittest.IsolatedAsyncioTestCase):
    async def local_session(self, *, failed_detail=False, early_detail=False, lazy=False, rate_limited=False,
                            detail_delay=0, missing_bridge=False, failed_request=False):
        from test_processing import itinerary,ProcessingContractTests
        temporary,module=load_browser()
        session=module.BrowserSession({'headless':True,'comft_flight':True,'action_timeout':1,'comfort_timeout':3},
                                      module.generate_identity({'headless':True}),{},None)
        await session.start()
        requests=[]
        complete={'status':0,'msg':'success','data':{'context':{'searchId':'done','finished':True},
                                                    'flightItineraryList':[itinerary()]}}
        if lazy:
            second=copy.deepcopy(itinerary(identity='two'))
            second['flightSegments'][0]['flightList'][0].update(flightNo='MU9999',operateFlightNo='FM9999')
            complete['data']['flightItineraryList'].append(second)
        comfort_request={'departureCityCode':'SHA','arrivalCityCode':'HKG','departureDate':'2027-01-10',
                         'flightNoList':['FM4567'],'cabinList':['Y']}
        send="fetch('/international/search/api/flight/comfort/getFlightComfort',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(req)})"
        html='''<div class="flight-item"><span class="plane-No" data-number="UNKNOWN">UNKNOWN<span>aircraft</span></span></div>
            <div class="flight-item"><span class="plane-No" data-number="FM4567">MU1234<span>aircraft</span></span></div><script>'''
        html+='const req='+json.dumps(comfort_request)+';'
        html+="document.querySelectorAll('.plane-No').forEach(el=>el.addEventListener('mouseenter',()=>{const req="+json.dumps(comfort_request)+";req.flightNoList=[el.dataset.number];return "+send+";}));"
        if lazy:
            html=html.replace('<div class="flight-item">', '<div class="flight-item" style="height:30px">')
            html='<div id="flight-list" style="height:160px;overflow:auto">'+html.replace('<script>', '<div id="spacer" style="height:1000px"></div></div><script>')
            html+="""document.querySelector('#flight-list').addEventListener('scroll',()=>{if(document.querySelector('[data-number=FM9999]'))return;
                const card=document.createElement('div');card.className='flight-item';card.style.height='30px';card.innerHTML='<span class="plane-No" data-number="FM9999">MU9999<span>aircraft</span></span>';
                document.querySelector('#spacer').before(card);card.querySelector('.plane-No').addEventListener('mouseenter',()=>{const req="""+json.dumps(comfort_request)+";req.flightNoList=['FM9999'];return "+send+";});});"
        html+='(async()=>{'+('await '+send+';' if early_detail else '')
        html+="await fetch('/international/search/api/search/batchSearch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify("+json.dumps(body())+")});})();</script>"
        await session.context.route('**/*',lambda route:route.fulfill(content_type='text/html',body='Local fixture'))
        await session.context.route('https://flights.ctrip.com/online/list/**',lambda route:route.fulfill(content_type='text/html',body=html))
        await session.context.route('**/batchSearch',lambda route:route.fulfill(content_type='application/json',body=json.dumps(complete)))
        async def detail(route):
            requests.append(route.request.post_data_json)
            if failed_request:
                await route.abort('failed')
                return
            if detail_delay:
                await asyncio.sleep(detail_delay)
            payload=ProcessingContractTests().comfort_response()['payload']
            if missing_bridge:
                del payload['data']['punctualityInfo']['departureBridge']
            await route.fulfill(status=429 if rate_limited else (503 if failed_detail else 200),
                headers={'Retry-After':'3600'} if rate_limited else {},content_type='application/json',
                body=json.dumps({'status':1} if failed_detail else payload))
        await session.context.route('**/getFlightComfort',detail)
        return temporary,module,session,requests

    async def test_hover_triggers_only_result_flights_and_supports_both_column_schemas(self):
        temporary,module,session,requests=await self.local_session()
        try:
            for rename in [True,False]:
                session.config['rename_col']=rename
                result=await session.collect(task(),30)
                self.assertEqual(result['comfort_status'],'complete')
                self.assertEqual(result['status'],'succeeded')
                summary=result['comfort_summary']
                self.assertEqual((summary['result_flights'],summary['requested_flights'],summary['responded_flights']), (1,1,1))
                self.assertEqual((summary['requests_sent'],summary['responses_received'],summary['covered_flights']), (1,1,1))
            self.assertEqual([r['flightNoList'] for r in requests],[['FM4567'],['FM4567']])
        finally:
            await session.close();temporary.cleanup()

    async def test_optional_error_before_core_completion_does_not_fail_core(self):
        temporary,module,session,requests=await self.local_session(failed_detail=True,early_detail=True)
        try:
            result=await session.collect(task(),30)
            self.assertEqual(result['status'],'succeeded')
            self.assertEqual(result['comfort_status'],'unavailable')
            self.assertIn('network_error',result['comfort_reason'])
        finally:
            await session.close();temporary.cleanup()

    async def test_scroll_continues_past_initial_cards_in_their_normal_container(self):
        temporary,module,session,requests=await self.local_session(lazy=True)
        try:
            result=await session.collect(task(),30)
            self.assertEqual(result['comfort_status'],'complete')
            self.assertEqual([r['flightNoList'] for r in requests],[['FM4567'],['FM9999']])
        finally:
            await session.close();temporary.cleanup()

    async def test_detail_rate_limit_preserves_server_wait_and_stops_further_hover(self):
        temporary,module,session,requests=await self.local_session(lazy=True,rate_limited=True)
        try:
            with self.assertRaises(module.CrawlerError) as error:
                await session.collect(task(),30)
            self.assertEqual(error.exception.code,'rate_limited')
            self.assertEqual(error.exception.retry_after,3600)
            self.assertEqual(len(requests),1)
        finally:
            await session.close();temporary.cleanup()

    async def test_late_inflight_response_is_consumed_before_the_comfort_deadline(self):
        temporary,module,session,requests=await self.local_session(detail_delay=1.8)
        try:
            result=await session.collect(task(),30)
            self.assertEqual(result['comfort_status'],'complete')
            self.assertEqual(result['comfort_summary']['covered_rows'],1)
            self.assertEqual(len(requests),1)
        finally:
            await session.close();temporary.cleanup()

    async def test_all_responses_with_missing_source_fields_finish_without_waiting_full_budget(self):
        temporary,module,session,requests=await self.local_session(missing_bridge=True)
        session.config['comfort_timeout']=10
        try:
            result=await asyncio.wait_for(session.collect(task(),30),4)
            self.assertEqual(result['comfort_status'],'partial')
            self.assertEqual(result['comfort_reason'],'source_missing')
            self.assertEqual(result['comfort_summary']['missing_fields'],{'departure_bridge_rate':1})
            self.assertEqual(result['comfort_summary']['responded_flights'],1)
        finally:
            await session.close();temporary.cleanup()

    async def test_failed_native_request_is_counted_without_waiting_for_a_response(self):
        temporary,module,session,requests=await self.local_session(failed_request=True)
        try:
            result=await asyncio.wait_for(session.collect(task(),30),4)
            self.assertEqual(result['comfort_status'],'unavailable')
            summary=result['comfort_summary']
            self.assertEqual((summary['requests_sent'],summary['responses_received'],summary['covered_flights']), (1,0,0))
            self.assertIn('network_error',result['comfort_reason'])
        finally:
            await session.close();temporary.cleanup()

    async def test_a_failed_hover_does_not_mark_the_flight_as_already_triggered(self):
        from playwright.async_api import TimeoutError as BrowserTimeout
        temporary,module,session,requests=await self.local_session()
        new_page=session.context.new_page
        failures=[]
        async def page_with_transient_hover():
            page=await new_page();locator=page.locator
            def wrapped_locator(selector):
                group=locator(selector)
                if selector=='.flight-item .plane-No':
                    nth=group.nth
                    def wrapped_nth(index):
                        item=nth(index);hover=item.hover
                        async def hover_with_one_failure(**kwargs):
                            if index==1 and not failures:
                                failures.append(True)
                                raise BrowserTimeout('Local transient hover failure')
                            return await hover(**kwargs)
                        item.hover=hover_with_one_failure
                        return item
                    group.nth=wrapped_nth
                return group
            page.locator=wrapped_locator
            return page
        session.context.new_page=page_with_transient_hover
        try:
            result=await session.collect(task(),30)
            self.assertEqual(result['comfort_status'],'complete')
            self.assertEqual(len(requests),1)
            self.assertEqual(len(failures),1)
        finally:
            await session.close();temporary.cleanup()

if __name__=='__main__':
    unittest.main()
