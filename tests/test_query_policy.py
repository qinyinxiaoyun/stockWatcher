"""Offline alias coverage checks through the real client with fake transport."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from stock_watch.news import EastmoneyNewsClient, NewsTarget, SHANGHAI
from stock_watch.quality import classify_article
from stock_watch.query_policy import is_alias_window, select_queries

NOW = datetime(2026,10,23,2,tzinfo=timezone.utc)
STOCK = NewsTarget('synthetic-query-stock','合成企业','合成企业',
                   queries=('合成企业','历史别名','123456.SH'),focus=('订单','经营风险'))


def row(key,title):
    return {'id':key,'title':title,'content':'公司公告确认重大经营风险。',
            'url':f'https://example.invalid/{key}','date':NOW.isoformat()}


class Transport:
    def __init__(self,results):
        self.results = results
        self.calls = []
    def __call__(self,url,timeout):
        query = json.loads(parse_qs(urlsplit(url).query)['param'][0])['keyword']
        self.calls.append(query)
        return json.dumps({'code':0,'result':{'cmsArticleWebOld':self.results.get(query,[])}})


@pytest.mark.parametrize('minute',[0,15,30,45])
def test_alias_windows_always_keep_all_queries(minute):
    selected = select_queries(STOCK,NOW.replace(minute=minute,second=17))
    assert selected.queries == STOCK.queries
    assert is_alias_window(NOW.replace(minute=minute))


@pytest.mark.parametrize('minute',[5,10,20,25,35,40,50,55])
def test_other_worker_rounds_select_primary_without_mutating_configuration(minute):
    selected = select_queries(STOCK,NOW.replace(minute=minute))
    assert selected.queries == (STOCK.query,)
    assert selected.key == STOCK.key and selected.focus == STOCK.focus
    assert STOCK.queries == ('合成企业','历史别名','123456.SH')


def test_nonempty_primary_still_refreshes_alias_only_major_fact_every_15_minutes():
    transport = Transport({'合成企业':[row('primary','合成企业公告新订单')],
                           '历史别名':[row('alias-risk','历史别名公告重大事故导致停产')]})
    client = EastmoneyNewsClient(transport=transport,max_attempts=1)
    initial = client.fetch_target(select_queries(STOCK,NOW.replace(minute=5)))
    assert {item.raw_id for item in initial} == {'primary'}
    assert transport.calls == ['合成企业']
    # Primary success is deliberately not a reason to skip the periodic pass.
    later = client.fetch_target(select_queries(STOCK,NOW.replace(minute=15)))
    assert transport.calls == ['合成企业','合成企业','历史别名','123456.SH']
    assert {item.raw_id for item in later} == {'primary','alias-risk'}
    risk = next(item for item in later if item.raw_id == 'alias-risk')
    assert classify_article(risk).urgent_hint


def test_blank_primary_preserves_alias_fallback_on_non_alias_round():
    target = replace(STOCK,query='  ',queries=('历史别名','123456.SH'))
    transport = Transport({'历史别名':[row('fallback','历史别名公告重大事故导致停产')]})
    result = EastmoneyNewsClient(transport=transport,max_attempts=1).fetch_target(
        select_queries(target,NOW.replace(minute=5)))
    assert transport.calls == ['历史别名','123456.SH']
    assert [item.raw_id for item in result] == ['fallback']
    assert target.query == '  ' and target.queries == ('历史别名','123456.SH')


def test_theme_keywords_retain_periodic_secondary_keyword_coverage():
    target = NewsTarget('synthetic-query-theme','半导体经营','芯片产能',
                        queries=('芯片产能','先进封装','HBM供需'),kind='theme',focus=('供需',))
    transport = Transport({'芯片产能':[row('theme-primary','芯片产能新项目公告')],
                           'HBM供需':[row('theme-alias','HBM供需公告出口禁令')]})
    client = EastmoneyNewsClient(transport=transport,max_attempts=1)
    first = client.fetch_target(select_queries(target,NOW.replace(minute=10)))
    assert {item.raw_id for item in first} == {'theme-primary'}
    second = client.fetch_target(select_queries(target,NOW.replace(minute=15)))
    assert {item.raw_id for item in second} == {'theme-primary','theme-alias'}
    assert transport.calls == ['芯片产能','芯片产能','先进封装','HBM供需']
    assert target.queries == ('芯片产能','先进封装','HBM供需')
    assert target.kind == 'theme' and target.focus == ('供需',)


def test_naive_local_and_aware_utc_times_follow_same_window():
    local = datetime(2026,10,23,10,15)
    assert is_alias_window(local)
    assert is_alias_window(local.replace(tzinfo=SHANGHAI).astimezone(timezone.utc))
    assert select_queries(STOCK,local).queries == STOCK.queries


def test_empty_queries_still_select_configured_primary():
    target = replace(STOCK,queries=())
    assert select_queries(target,NOW.replace(minute=5)).queries == ('合成企业',)
    assert select_queries(target,NOW.replace(minute=15)).queries == ()


@pytest.mark.parametrize('primary_failure', [False, True])
def test_scan_falls_back_to_all_aliases_on_empty_or_failed_primary(primary_failure):
    from types import SimpleNamespace
    from stock_watch.subscription_scan import _fetch_target

    class Client(EastmoneyNewsClient):
        def __init__(self):
            self.calls = []

        def fetch_target(self, target, *, now):
            self.calls.append(target.queries)
            if len(self.calls) == 1:
                if primary_failure:
                    raise TimeoutError('synthetic unavailable source')
                return []
            return ['alias result']

    client = Client()
    assert _fetch_target(SimpleNamespace(fetcher=client), STOCK, NOW.replace(minute=5)) == ['alias result']
    assert client.calls == [(STOCK.query,), STOCK.queries]
    assert STOCK.queries == ('合成企业','历史别名','123456.SH')
