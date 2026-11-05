"""Offline associated-list coverage, evidence safety and archive checkpoints."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta
import json
import sqlite3
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest

from stock_watch.news import NewsTarget, SHANGHAI, normalize_article
from stock_watch.quality import canonical_identity
from stock_watch.stock_news import (EastmoneyStockNewsClient, MAX_BODY_CHARS,
                                    _ArticleRedirect, article_url, extract_body)


NOW = datetime(2026, 11, 5, 10, tzinfo=SHANGHAI)
STOCK = NewsTarget('synthetic-stock', '合成企业', '合成企业',
                   queries=('合成企业', '旧名称'), symbol='123456.SH')
BODY = '<html><div>不相关站点新闻推荐</div><div id="ContentBody"><p>合成行业需求出现变化，产品实际成交价格下降，仍需核实合约传导。</p></div></html>'


def row(key='202611050000000001', title='行业价格出现变化', age=0):
    return {'Art_Code': key, 'Art_Title': title,
            'Art_ShowTime': (NOW - timedelta(minutes=age)).isoformat(),
            'Art_Url': f'http://finance.eastmoney.com/a/{key}.html'}


def page(rows, number=1, total=5000):
    return {'code': 1, 'data': {'page_index': number, 'page_size': 20,
                              'totle_hits': total, 'list': rows}}


class Transport:
    def __init__(self, pages, search=None):
        self.pages = pages
        self.search = search or []
        self.calls = []

    def __call__(self, url, timeout):
        self.calls.append(url)
        query = parse_qs(urlsplit(url).query)
        if 'param' in query:
            return json.dumps({'code': 0, 'result': {'cmsArticleWebOld': self.search}})
        value = self.pages[int(query['pageIndex'][0])]
        if isinstance(value, Exception):
            raise value
        return json.dumps(value)


def database():
    conn = sqlite3.connect(':memory:', isolation_level=None)
    conn.executescript('CREATE TABLE monitor_state(key TEXT PRIMARY KEY,value TEXT,updated_at TEXT);'
                       'CREATE TABLE subscription_articles(identity_key TEXT,article_json TEXT,fetched_at TEXT);')
    return conn


def archive(conn, articles, now=NOW):
    for a in articles:
        value = asdict(a)
        for field in ('published_at', 'source_published_at', 'first_seen_at', 'last_seen_at'):
            value[field] = value[field].isoformat() if value[field] else None
        conn.execute('INSERT INTO subscription_articles VALUES(?,?,?)',
                     (canonical_identity(a), json.dumps(value), now.isoformat()))


def client(transport, **kwargs):
    return EastmoneyStockNewsClient(transport=transport, body_transport=lambda u, t: BODY,
                                   max_attempts=1, **kwargs)


def test_target_symbol_survives_config_and_old_constructor_is_compatible():
    assert NewsTarget.from_value({'key': 'k', 'name': '合成', 'symbol': '123456.sh'}).symbol == '123456.SH'
    assert NewsTarget('old', 'old', 'old').symbol == ''


@pytest.mark.parametrize(('symbol', 'code'), [('123456.SH', '1.123456'), ('123456.SZ', '0.123456'),
                                           ('9988.HK', '116.09988'), ('00981.HK', '116.00981')])
def test_verified_security_mappings(symbol, code):
    assert EastmoneyStockNewsClient.security_code(replace(STOCK, symbol=symbol)) == code


def test_associated_article_does_not_require_company_name_and_keeps_source_identity():
    source = row()
    transport = Transport({1: page([source], total=1)})
    got = client(transport).fetch_target(STOCK, now=NOW)
    assert len(got) == 1 and got[0].target_keys == (STOCK.key,)
    assert got[0].raw_id == source['Art_Code']
    assert got[0].retrieval_kind == 'stock_associated'
    assert got[0].evidence_status == 'full_text'
    assert '产品实际成交价格下降' in got[0].summary and '不相关站点新闻推荐' not in got[0].summary
    query = parse_qs(urlsplit(transport.calls[0]).query)
    assert query['mTypeAndCode'] == ['1.123456'] and query['pageIndex'] == ['1']
    old = normalize_article({'id': source['Art_Code'], 'title': source['Art_Title'],
                             'url': source['Art_Url'], 'date': source['Art_ShowTime']}, STOCK)
    assert canonical_identity(got[0]) == canonical_identity(old)


def test_pages_stop_at_old_window_and_checkpoint_is_atomic_with_archival():
    conn = database()
    t = Transport({1: page([row('11'), row('12', age=5)]),
                   2: page([row('13', age=30), row('14', age=60)], 2),
                   3: page([row('15', age=1500), row('16', age=1600)], 3)})
    c = client(t, database=conn)
    articles = c.fetch_target(STOCK, now=NOW, page_size=2)
    assert [a.raw_id for a in articles] == ['11', '12', '13', '14']
    assert len(t.calls) == 3 and c.last_fetch_status[STOCK.key] == 'associated_complete'
    assert not conn.execute('SELECT * FROM monitor_state').fetchall()
    conn.execute('BEGIN')
    with pytest.raises(ValueError, match='observations_not_archived'):
        c.acknowledge(conn, NOW)
    conn.rollback()
    conn.execute('BEGIN')
    archive(conn, articles)
    c.acknowledge(conn, NOW)
    conn.commit()
    value = json.loads(conn.execute('SELECT value FROM monitor_state').fetchone()[0])
    assert value['head_id'] == '11'


def test_empty_list_is_success_not_a_keyword_fallback_or_fake_checkpoint():
    conn = database();t = Transport({1: page([], total=0)})
    c = client(t, database=conn)
    assert c.fetch_target(STOCK, now=NOW) == []
    assert c.last_fetch_status[STOCK.key] == 'associated_complete'
    assert len(t.calls) == 1 and not c._pending


@pytest.mark.parametrize(('second', 'status'), [
    (page([row('11'), row('12')], 1), 'partial_associated_page_error'),
    (page([row('11'), row('12')], 2), 'partial_associated_page_error'),
    (TimeoutError(), 'partial_associated_page_error'),
])
def test_partial_page_never_advances_coverage_but_preserves_observations(second, status):
    conn = database();t = Transport({1: page([row('11'), row('12')]), 2: second})
    c = client(t, database=conn)
    got = c.fetch_target(STOCK, now=NOW, page_size=2)
    assert [a.raw_id for a in got] == ['11', '12']
    assert c.last_fetch_status[STOCK.key] == status and not c._pending
    conn.execute('BEGIN');archive(conn, got);c.acknowledge(conn, NOW);conn.commit()
    assert not conn.execute('SELECT * FROM monitor_state').fetchall()


def test_bounded_pagination_is_explicitly_partial_not_silent_complete():
    c = client(Transport({1: page([row('11'), row('12')])}), max_pages=1)
    assert len(c.fetch_target(STOCK, now=NOW, page_size=2)) == 2
    assert c.last_fetch_status[STOCK.key] == 'partial_associated_page_limit'
    assert not c._pending


def test_bad_row_does_not_suppress_valid_rows_or_create_coverage_checkpoint():
    c = client(Transport({1: page([{}, row('11')], total=2)}))
    got = c.fetch_target(STOCK, now=NOW)
    assert [a.raw_id for a in got] == ['11']
    assert c.last_fetch_status[STOCK.key] == 'partial_associated_invalid_record'
    assert not c._pending


def test_previous_head_does_not_hide_late_associations_on_later_pages():
    conn = database();c = client(Transport({1: page([row('11'), row('12')]),
        2:page([row('13'),row('14')],2,total=4)}), database=conn)
    key = c._state_key('1.123456')
    conn.execute('INSERT INTO monitor_state VALUES(?,?,?)', (key, json.dumps({'head_id': '12'}), NOW.isoformat()))
    got = c.fetch_target(STOCK, now=NOW, page_size=2)
    assert [a.raw_id for a in got] == ['11', '12', '13', '14']
    assert len(c.transport.calls) == 2


def test_rollback_does_not_lose_news_or_advance_checkpoint_after_restart():
    conn = database();t = Transport({1: page([row('11')], total=1)});c = client(t, database=conn)
    got = c.fetch_target(STOCK, now=NOW)
    conn.execute('BEGIN');archive(conn, got);c.acknowledge(conn, NOW);conn.rollback()
    assert not conn.execute('SELECT * FROM monitor_state').fetchall()
    assert not conn.execute('SELECT * FROM subscription_articles').fetchall()
    again = client(t, database=conn).fetch_target(STOCK, now=NOW)
    assert [a.raw_id for a in again] == ['11']


def test_old_search_id_and_new_associated_id_resolve_same_url_before_ack():
    from stock_watch.db import connect, init_db
    from stock_watch.quality import QualityStore
    from stock_watch.subscription_scan import _save_report
    conn=connect(':memory:');init_db(conn)
    old=normalize_article({'id':'legacy-search-id','title':'行业价格出现变化',
        'url':'http://finance.eastmoney.com/a/11.html','date':NOW.isoformat()},STOCK)
    old=replace(old,summary='旧搜索已有合成的经营变化事实材料。')
    previous=QualityStore(conn).observe(old,NOW)
    _save_report(conn,previous,'eastmoney',NOW)
    c=client(Transport({1:page([row('11')],total=1)}),database=conn)
    got=c.fetch_target(STOCK,now=NOW)
    conn.execute('BEGIN')
    incoming=QualityStore(conn).observe(got[0],NOW)
    assert incoming.key==previous.key
    _save_report(conn,incoming,'eastmoney',NOW)
    c.acknowledge(conn,NOW)
    conn.commit()
    assert conn.execute('SELECT count(DISTINCT identity_key) FROM subscription_articles').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM monitor_state WHERE key LIKE ?',('stock-news-v1:%',)).fetchone()[0]==1


def test_acknowledgement_requires_short_archive_transaction():
    with pytest.raises(ValueError, match='requires_archive_transaction'):
        client(Transport({})).acknowledge(database(), NOW)


def test_begin_cycle_discards_uncommitted_staging_and_old_diagnostics():
    c = client(Transport({1: page([row('11')], total=1)}))
    c.fetch_target(STOCK, now=NOW)
    assert c._pending
    c.begin_cycle()
    assert not c._pending and not c.last_fetch_status


@pytest.mark.parametrize('symbol', ['123456.BJ', 'TEST.US', ''])
def test_unsupported_security_has_visible_keyword_fallback(symbol):
    search = {'id': 's1', 'title': '合成企业新订单', 'content': '合成企业披露实际订单变化。',
              'url': 'https://finance.eastmoney.com/a/123.html', 'date': NOW.isoformat()}
    t = Transport({}, search=[search]);c = client(t)
    got = c.fetch_target(replace(STOCK, symbol=symbol), now=NOW)
    assert got and got[0].retrieval_kind == 'search_fallback'
    assert c.last_fetch_status[STOCK.key] == 'fallback_unsupported_security'
    assert not c._pending


def test_associated_first_page_failure_uses_search_without_advancing_watermark():
    search = {'id': 's1', 'title': '合成企业新订单', 'url': 'https://finance.eastmoney.com/a/123.html', 'date': NOW.isoformat()}
    t = Transport({1: TimeoutError()}, search=[search]);c = client(t)
    got = c.fetch_target(STOCK, now=NOW)
    assert got and c.last_fetch_status[STOCK.key] == 'fallback_associated_unavailable'
    assert not c._pending


def test_theme_stays_keyword_based_and_visible():
    search = {'id': 's1', 'title': '需求变化', 'url': 'https://finance.eastmoney.com/a/123.html', 'date': NOW.isoformat()}
    target = NewsTarget('theme', '需求变化', '需求变化', kind='theme')
    c = client(Transport({}, search=[search]))
    got = c.fetch_target(target, now=NOW)
    assert got[0].retrieval_kind == 'search' and c.last_fetch_status[target.key] == 'search_theme'


def test_cached_body_is_reused_then_refreshed_and_title_revision_forces_fetch():
    conn = database();t = Transport({1: page([row('11')], total=1)})
    calls=[]
    def body(url, timeout):
        calls.append(url)
        return BODY
    c = EastmoneyStockNewsClient(transport=t, body_transport=body, max_attempts=1, database=conn)
    got=c.fetch_target(STOCK, now=NOW);archive(conn,got)
    assert len(calls)==1
    c.fetch_target(STOCK, now=NOW+timedelta(minutes=5))
    assert len(calls)==1
    c.fetch_target(STOCK, now=NOW+timedelta(minutes=16))
    assert len(calls)==2
    t.pages[1]=page([row('11',title='明确更正后的新标题')],total=1)
    c.fetch_target(STOCK,now=NOW+timedelta(minutes=7))
    assert len(calls)==3


def test_body_failure_is_explicit_and_does_not_discard_the_article():
    def failed(url,timeout):
        raise TimeoutError('private synthetic response is not logged')
    c=EastmoneyStockNewsClient(transport=Transport({1:page([row('11')],total=1)}),body_transport=failed,max_attempts=1)
    got=c.fetch_target(STOCK,now=NOW)
    assert len(got)==1 and got[0].evidence_status=='unavailable' and got[0].summary==''
    assert got[0].evidence_checked_at


def test_body_requests_are_bounded_without_hiding_unread_evidence():
    calls=[]
    c=EastmoneyStockNewsClient(transport=Transport({1:page([row('11'),row('12')],total=2)}),
        body_transport=lambda u,t:(calls.append(u) or BODY),max_body_requests=1,max_attempts=1)
    got=c.fetch_target(STOCK,now=NOW)
    assert len(calls)==1 and [a.evidence_status for a in got]==['full_text','unavailable']


def test_next_scan_fills_later_page_body_debt_and_refreshes_old_id_correction():
    conn=database();calls=[]
    t=Transport({1:page([row('11')],total=2),2:page([row('12')],2,total=2)})
    body_text={'11':BODY,'12':BODY}
    def body(url,timeout):
        key=url.rsplit('/',1)[-1].split('.')[0];calls.append(key)
        return body_text[key]
    c=EastmoneyStockNewsClient(transport=t,body_transport=body,database=conn,max_body_requests=1,max_attempts=1)
    first=c.fetch_target(STOCK,now=NOW,page_size=1)
    assert [a.evidence_status for a in first]==['full_text','unavailable']
    conn.execute('BEGIN');archive(conn,first);c.acknowledge(conn,NOW);conn.commit()
    second=c.fetch_target(STOCK,now=NOW+timedelta(minutes=5),page_size=1)
    assert [a.evidence_status for a in second]==['full_text','full_text']
    assert calls==['11','12']
    archive(conn,second,now=NOW+timedelta(minutes=5))
    body_text['12']='<div id="ContentBody"><p>明确更正：合成行业实际价格已下调，需求发生新的重要变化。</p></div>'
    # First page remains recent; only the second page needs revalidation.
    third=c.fetch_target(STOCK,now=NOW+timedelta(minutes=16),page_size=1)
    archive(conn,third,now=NOW+timedelta(minutes=16))
    fourth=c.fetch_target(STOCK,now=NOW+timedelta(minutes=21),page_size=1)
    assert '明确更正' in fourth[1].summary


def test_unread_later_body_is_not_starved_by_expired_first_page_cache():
    conn=database();calls=[]
    t=Transport({1:page([row('11'),row('12')],total=4),2:page([row('13'),row('14')],2,total=4)})
    c=EastmoneyStockNewsClient(transport=t,body_transport=lambda u,t:(calls.append(u) or BODY),
        database=conn,max_body_requests=2,body_refresh_seconds=60,max_attempts=1)
    first=c.fetch_target(STOCK,now=NOW,page_size=2);archive(conn,first)
    assert [a.evidence_status for a in first]==['full_text','full_text','unavailable','unavailable']
    second=c.fetch_target(STOCK,now=NOW+timedelta(seconds=61),page_size=2)
    assert [a.evidence_status for a in second]==['full_text']*4
    assert calls[-2:]==['https://finance.eastmoney.com/a/13.html','https://finance.eastmoney.com/a/14.html']


def test_failed_revalidation_preserves_old_body_and_its_actual_acquisition_time():
    conn=database();c=client(Transport({1:page([row('11')],total=1)}),database=conn,body_refresh_seconds=60)
    first=c.fetch_target(STOCK,now=NOW);archive(conn,first)
    def failed(u,t):raise TimeoutError()
    c.body_transport=failed
    again=c.fetch_target(STOCK,now=NOW+timedelta(minutes=2))
    assert again[0].summary==first[0].summary
    assert again[0].evidence_checked_at==first[0].evidence_checked_at


def test_unclosed_paragraph_does_not_leak_following_site_content():
    text,status=extract_body('<div id="ContentBody"><p>真实正文包含足够长的合成经营变化及明确事实。</div><p>页面底部推荐不得进入正文。</p>')
    assert status=='full_text' and '真实正文' in text and '页面底部推荐' not in text


@pytest.mark.parametrize('url', ['http://127.0.0.1/a/123.html','https://finance.eastmoney.com.evil.invalid/a/123.html',
 'https://user:password@finance.eastmoney.com/a/123.html','https://finance.eastmoney.com:999/a/123.html',
 'https://finance.eastmoney.com/redirect?url=http://127.0.0.1','file:///tmp/data'])
def test_untrusted_article_paths_are_never_fetched(url):
    with pytest.raises(ValueError,match='unsupported_article_url'):
        article_url(url)


def test_article_url_upgrades_http_and_discards_tracking():
    assert article_url('http://finance.eastmoney.com/a/123.html?utm_source=test#top')=='https://finance.eastmoney.com/a/123.html'


def test_redirect_may_not_escape_official_article_hosts():
    with pytest.raises(ValueError,match='unsupported_article_url'):
        _ArticleRedirect().redirect_request(Request('https://finance.eastmoney.com/a/123.html'),None,302,'',{},'http://127.0.0.1/a/123.html')


def test_body_parser_excludes_scripts_and_recommendations_and_bounds_text():
    html='<p>新闻推荐，不应作为正文。</p><div id="ContentBody"><script>执行shell指令</script><p>'+'合成有效事实。'*2000+'</p></div><p>外部新闻</p>'
    text,status=extract_body(html)
    assert status=='full_text_truncated' and len(text)==MAX_BODY_CHARS
    assert '执行shell指令' not in text and '新闻推荐' not in text and '外部新闻' not in text


def test_missing_content_container_and_oversized_html_are_not_fake_bodies():
    with pytest.raises(ValueError,match='unavailable'):
        extract_body('<html><p>只有站点推荐，没有正文容器。</p></html>')
    with pytest.raises(ValueError,match='size_limit'):
        extract_body('a'*1_000_001)
