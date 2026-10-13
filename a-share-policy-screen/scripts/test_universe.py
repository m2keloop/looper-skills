"""Exchange fallback must remain complete, current and source-attributed."""
import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from fetch_data import official_universe
from run_daily import collect, is_main_board


class Frame:
    empty = False
    columns = ['A股代码','A股简称','A股上市日期','板块','所属行业']
    def fillna(self, value):
        return self
    def to_dict(self, orient):
        return [{'A股代码':'000001','A股简称':'Test SZ','A股上市日期':'1991-04-03',
                 '板块':'主板','所属行业':'J 金融业'}]


def row(code, removed='-'):
    return {'A_STOCK_CODE':code,'LIST_DATE':'20200101','DELIST_DATE':removed,
            'SEC_NAME_CN':'Test SH','CSRC_CODE_DESC':'制造业'}


def adapter(main=None, total=None, fail_sz=False):
    main = main if main is not None else [row('600001'),row('600002','20200102')]
    def http(url, **kwargs):
        if 'query.sse' in url:
            rs = main if kwargs['params']['STOCK_TYPE']=='1' else [row('688001')]
            count = len(rs) if total is None else total
            return SimpleNamespace(json=lambda: {'result':copy.deepcopy(rs),'pageHelp':{'total':count}})
        if fail_sz:
            raise ConnectionError('synthetic unavailable exchange')
        return SimpleNamespace(content=b'PKsynthetic')
    pd = SimpleNamespace(read_excel=lambda *a,**k:Frame(),Timestamp=lambda s:datetime.fromisoformat(s))
    return SimpleNamespace(_v39_http=http,pd=pd)


class UniverseTests(unittest.TestCase):
    def test_main_board_filter_before_collection(self):
        self.assertTrue(all(is_main_board(s) for s in ['sh600001','sh601001','sh603001','sh605001','sz000001','sz001001','sz002001','sz003001']))
        self.assertFalse(any(is_main_board(s) for s in ['sh688001','sh689001','sz300001','sz301001']))

    def test_resume_only_main_checkpoints_without_repulling_bars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); run=root/'run';run.mkdir()
            members=[{'symbol':s,'asset_status':'ACTIVE'} for s in ['sh600001','sh688001','sz300001']]
            (run/'universe.json').write_text(json.dumps(members))
            entries=[{'stock':m,'revision':m['symbol']} for m in members]
            class Cache:
                path=root/'_cache/market.sqlite'
                def snapshot(self, identity):
                    return {'scope':'current_active_sh_sz','market_days':['2026-10-08']},entries
                def checkpoints(self, key):
                    return {e['stock']['symbol']:e for e in entries}
                def save_snapshot(self, header, chosen):
                    self.header,self.chosen=header,chosen
                    return 'saved'
            cache=Cache()
            client=SimpleNamespace(interval=.25,metrics=lambda:{'http_requests':0})
            sd=SimpleNamespace(V39_CLIENT=client)
            with patch.dict('sys.modules',{'data_adapter':sd}), patch('run_daily.batch_quotes',return_value={}) as quotes:
                _,stats=collect(cache,'prior','2026-10-08',run,main_board_only=True)
            self.assertEqual(quotes.call_args.args[1],['sh600001'])
            self.assertEqual([e['stock']['symbol'] for e in cache.chosen],['sh600001'])
            self.assertEqual(cache.header['scope'],'subset')
            self.assertEqual(cache.header['universe_filter'],'MAIN')
            self.assertEqual(stats['cache_hits'],1)
            self.assertEqual(stats['pool'],1)
            self.assertEqual(stats['universe_before_filter'],3)
            self.assertEqual(stats['http_requests'],0)

    def test_complete_pool_excludes_retired_and_records_real_source(self):
        stocks = official_universe(adapter(),'2026-10-08')
        self.assertEqual([s['symbol'] for s in stocks],['sh600001','sh688001','sz000001'])
        self.assertTrue(all(s['asset_status']=='ACTIVE' and s['universe_as_of'] for s in stocks))
        self.assertTrue(all('official' in s['universe_source'] for s in stocks))

    def test_one_exchange_unavailable_fails_whole_pool(self):
        with self.assertRaises(ConnectionError):
            official_universe(adapter(fail_sz=True),'2026-10-08')

    def test_duplicate_members_cannot_hide_partial_pool(self):
        with self.assertRaisesRegex(ValueError,'duplicate'):
            official_universe(adapter([row('600001'),row('600001')]),'2026-10-08')

    def test_pagination_count_mismatch_fails(self):
        with self.assertRaisesRegex(ValueError,'count_mismatch'):
            official_universe(adapter(total=1),'2026-10-08')


if __name__=='__main__':
    unittest.main()
