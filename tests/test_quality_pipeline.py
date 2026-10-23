"""Synthetic end-to-end policy replay; no provider or delivery network calls."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json

import pytest

from stock_watch.analyzer import AnalysisResult, DeepSeekAnalyzer, FakeAnalyzer, build_batch_prompt
from stock_watch.db import connect, init_db
from stock_watch.model_usage import budget_status, record_usage
from stock_watch.notifications import flush_notifications
from stock_watch.news import NewsArticle
from stock_watch.settings import Settings
from stock_watch.subscriptions import MemberConfigStore, MemberRegistry
from stock_watch.subscription_monitor import SubscriptionMonitorService

NOW = datetime(2026,10,23,2,tzinfo=timezone.utc)
CORP = 'synthetic-quality-corp'


class Fetcher:
    def __init__(self,articles=()):
        self.articles = list(articles)
    def fetch_target(self,target,**kwargs):
        return [replace(article,target_keys=(target.key,)) for article in self.articles]


def article(key,title='合成企业公告获得订单100万元',*,summary='新签订单支持收入增长。',published=NOW,url=None):
    return NewsArticle(key=key,raw_id=key,title=title,summary=summary,
                       url=url or f'https://example.invalid/{key}',published_at=published)


def make_state(tmp_path,users=('synthetic-alice',),*,two_targets=False,
               stock_name='合成企业',stock_focus='订单'):
    path = tmp_path/'quality.sqlite'
    conn = connect(path)
    init_db(conn)
    registry = MemberRegistry(conn,CORP)
    members = [registry.add(user) for user in users]
    for member in members:
        store = MemberConfigStore(conn,member.id,clock=lambda:NOW-timedelta(days=2))
        store.apply_operation({'op':'add_stock','symbol':'123456.SH','name':stock_name,'focus':stock_focus},actor=member.user_id)
        if two_targets:
            store.apply_operation({'op':'add_stock','symbol':'654321.SH','name':'另一企业','focus':'经营风险'},actor=member.user_id)
    return path,conn,registry,members


def scan(path,fetcher,analyzer,*,now=NOW,**kwargs):
    service = SubscriptionMonitorService(path,fetcher,analyzer,corp_id=CORP,
        environ={'MONITOR_ENABLED':'false'},**kwargs)
    return service.run_once(now=now,force=True)


def candidates(conn):
    return [json.loads(row[0]) for row in conn.execute('SELECT payload_json FROM notification_candidates ORDER BY id')]


def test_150_observation_replay_keeps_operating_facts_and_removes_noise_cost(tmp_path):
    path,conn,_,_ = make_state(tmp_path)
    try:
        facts = [article(f'fact-{i}',f'合成企业公告第{i}项目新订单{100+i}万元') for i in range(30)]
        facts += [article(f'fact-risk-{i}',f'合成企业公告第{i}项目重大事故导致停产',
                          summary='公告确认生产中断并调查事故影响。') for i in range(5)]
        facts += [article(f'fact-update-{i}',f'合成企业更正第{i}项目订单金额为{80+i}万元',
                          summary=f'更正：订单金额由{100+i}万元修正为{80+i}万元。') for i in range(5)]
        noise = [article(f'noise-{i}',f'南向资金净卖出合成企业{100+i}万元',summary='日内交易资金播报。') for i in range(40)]
        old = [article(f'old-{i}',f'合成企业昨日订单回顾{i}',published=NOW-timedelta(hours=10)) for i in range(40)]
        duplicates = [replace(facts[i],key=f'snippet-{i}',title='  '+facts[i].title+'  ',
                              summary='不同检索摘录，订单仍为同一来源事实。',
                              url=facts[i].url+'?utm_source=synthetic#highlight') for i in range(30)]
        corpus = [*facts,*noise,*old,*duplicates]
        assert len(corpus) == 150
        fetcher,analyzer = Fetcher(corpus),FakeAnalyzer()
        results = [scan(path,fetcher,analyzer,now=NOW+timedelta(minutes=i)) for i in range(5)]
        assert all(result.status == 'succeeded' for result in results)
        analyzed_ids = {item.raw_id for item,_ in analyzer.calls}
        assert analyzed_ids == {item.raw_id for item in facts}
        assert len(analyzer.calls) == 40
        assert len(analyzer.calls) / len(corpus) <= .5
        assert len(candidates(conn)) == 40
        assert sum(result.filtered for result in results) >= 40
        assert sum(result.expired for result in results) >= 40
        assert conn.execute('SELECT COUNT(*) FROM quality_article_observations').fetchone()[0] >= 150
        assert flush_notifications(path,NOW+timedelta(minutes=30)) == 1
        payload = json.loads(conn.execute('SELECT payload_json FROM outbox').fetchone()[0])
        assert payload['event'] == 'subscription.digest'
        assert len(payload['items']) == 3
        assert all(item['article']['title'] in {fact.title for fact in facts} for item in payload['items'])
    finally:
        conn.close()


def test_same_url_different_query_snippets_and_targets_merge_once(tmp_path):
    path,conn,_,_ = make_state(tmp_path,two_targets=True)
    try:
        items = [article('source-id'),replace(article('source-id'),key='query-rendering',
                 title='合成 企业 公告 获得订单100万元',summary='查询摘录发生变化。',
                 url='https://example.invalid/source-id?utm_source=other')]
        analyzer = FakeAnalyzer()
        first = scan(path,Fetcher(items),analyzer)
        assert first.analysis_tasks == len(analyzer.calls) == 2
        assert first.queued == 1
        assert len(candidates(conn)) == 1
        assert len(candidates(conn)[0]['target_analyses']) == 2
        assert scan(path,Fetcher(list(reversed(items))),analyzer,now=NOW+timedelta(minutes=1)).analysis_tasks == 0
        assert len(analyzer.calls) == 2
        assert flush_notifications(path,NOW+timedelta(minutes=30)) == 1
        assert conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 1
    finally:
        conn.close()


def test_exact_factual_reprint_across_urls_needs_one_analysis(tmp_path):
    path,conn,_,_ = make_state(tmp_path)
    try:
        analyzer = FakeAnalyzer()
        items = [article('outlet-a'),article('outlet-b')]
        result = scan(path,Fetcher(items),analyzer)
        assert result.analysis_tasks == len(analyzer.calls) == 1
        assert result.queued == len(candidates(conn)) == 1
    finally:
        conn.close()


def test_material_correction_is_analyzed_and_notifies_after_original(tmp_path):
    path,conn,_,_ = make_state(tmp_path)
    try:
        analyzer = FakeAnalyzer()
        original = article('correction-source')
        assert scan(path,Fetcher([original]),analyzer).queued == 1
        assert flush_notifications(path,NOW+timedelta(minutes=30)) == 1
        conn.execute("UPDATE outbox SET status='sent'")
        correction = replace(original,title='合成企业更正公告订单金额为80万元',
                             summary='更正：此前100万元订单金额修正为80万元。',
                             published_at=NOW+timedelta(minutes=31))
        result = scan(path,Fetcher([correction]),analyzer,now=NOW+timedelta(minutes=31))
        assert result.queued == 1
        assert len(analyzer.calls) == 2
        assert len(candidates(conn)) == 2
        assert flush_notifications(path,NOW+timedelta(minutes=60)) == 1
        assert conn.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 2
    finally:
        conn.close()


def test_public_failure_is_once_per_cycle_and_recovers_after_cooldown(tmp_path):
    path,conn,_,_ = make_state(tmp_path,users=('synthetic-alice','synthetic-bob'))
    try:
        class Failing(FakeAnalyzer):
            def __init__(self):
                super().__init__()
                self.attempts = 0
                self.fail = True
            def analyze(self,item,targets):
                self.attempts += 1
                if self.fail:
                    raise TimeoutError('synthetic-untrusted-upstream-data')
                return super().analyze(item,targets)
        fetcher,bad = Fetcher([article('failed-once')]),Failing()
        assert scan(path,fetcher,bad).status == 'partial'
        assert bad.attempts == 1
        assert scan(path,fetcher,bad,now=NOW+timedelta(minutes=1)).analysis_tasks == 0
        assert bad.attempts == 1
        assert conn.execute('SELECT COUNT(*) FROM member_evaluations').fetchone()[0] == 0
        bad.fail = False
        result = scan(path,fetcher,bad,now=NOW+timedelta(minutes=6))
        assert result.analysis_tasks == 1
        assert result.queued == 2
        assert len(candidates(conn)) == 2
        assert conn.execute('SELECT attempts,state FROM analysis_attempts').fetchone()['attempts'] == 2
    finally:
        conn.close()


def test_private_member_has_independent_failure_and_success_cache(tmp_path):
    path,conn,registry,members = make_state(tmp_path,users=('synthetic-alice','synthetic-bob'))
    try:
        registry.set_analysis_private(members[1].user_id,True)
        analyzer = FakeAnalyzer()
        result = scan(path,Fetcher([article('private-domains')]),analyzer)
        assert result.analysis_tasks == 2
        assert len(analyzer.calls) == 2
        assert result.queued == 2
        assert conn.execute('SELECT COUNT(*) FROM subscription_analysis').fetchone()[0] == 2
    finally:
        conn.close()


def test_unknown_provider_usage_is_charged_but_never_counted_as_actual(tmp_path):
    path,conn,_,_ = make_state(tmp_path)
    try:
        def transport(*,model,prompt,timeout):
            data = json.loads(prompt.split('<UNTRUSTED_TASKS>\n',1)[1].split('\n</UNTRUSTED_TASKS>',1)[0])
            return {'results':[{'task_id':item['task_id'],'analysis':{
                'level':'重要','impact':'利好','reason':'新订单支持收入。','followup':'',
                'matched_targets':[item['targets'][0]['key']]}} for item in data]}
        analyzer = DeepSeekAnalyzer(transport)
        result = scan(path,Fetcher([article('unknown-cost')]),analyzer)
        assert result.analysis_tasks == 1 and result.queued == 1
        row = conn.execute('SELECT total_tokens,budget_charge,estimated_tokens FROM model_usage').fetchone()
        assert row['total_tokens'] is None
        assert row['budget_charge'] == row['estimated_tokens'] > 0
        assert budget_status(path,now=NOW)['unknown_attempts'] == 1
        assert conn.execute("SELECT COUNT(*) FROM model_budget_reservations WHERE state='reserved'").fetchone()[0] == 0
    finally:
        conn.close()


def test_remote_model_holds_no_sqlite_write_transaction(tmp_path):
    path,conn,_,_ = make_state(tmp_path)
    try:
        class LockProbe(FakeAnalyzer):
            def analyze(self,item,targets):
                other = connect(path,busy_timeout_ms=10)
                try:
                    other.execute('BEGIN IMMEDIATE')
                    other.rollback()
                finally:
                    other.close()
                return super().analyze(item,targets)
        result = scan(path,Fetcher([article('lock-probe')]),LockProbe())
        assert result.status == 'succeeded' and result.queued == 1
    finally:
        conn.close()


def test_scope_change_during_model_call_cannot_persist_public_result(tmp_path):
    path,conn,registry,members = make_state(tmp_path)
    try:
        class ChangePrivacy(FakeAnalyzer):
            def analyze(self,item,targets):
                result = super().analyze(item,targets)
                registry.set_analysis_private(members[0].user_id,True)
                return result
        result = scan(path,Fetcher([article('scope-race')]),ChangePrivacy())
        assert result.queued == 0
        assert result.analysis_succeeded == 0
        assert conn.execute('SELECT COUNT(*) FROM subscription_analysis').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM member_evaluations').fetchone()[0] == 0
        assert not candidates(conn)
    finally:
        conn.close()


def test_lease_lost_during_model_call_cannot_write_success(tmp_path):
    path,conn,_,_ = make_state(tmp_path)
    try:
        class LoseLease(FakeAnalyzer):
            def analyze(self,item,targets):
                result = super().analyze(item,targets)
                conn.execute("UPDATE monitor_leases SET lease_owner='synthetic-other-owner' WHERE name='monitor'")
                return result
        result = scan(path,Fetcher([article('lease-race')]),LoseLease())
        assert result.status == 'failed' and result.reason == 'lease_lost'
        assert result.queued == 0
        assert conn.execute('SELECT COUNT(*) FROM subscription_analysis').fetchone()[0] == 0
        assert not candidates(conn)
    finally:
        conn.close()


def test_batch_partial_success_retries_only_failed_task_after_cooldown(tmp_path):
    path,conn,_,_ = make_state(tmp_path)
    try:
        calls = []
        fail = [True]
        def transport(*,model,prompt,timeout):
            data = json.loads(prompt.split('<UNTRUSTED_TASKS>\n',1)[1].split('\n</UNTRUSTED_TASKS>',1)[0])
            calls.append([item['task_id'] for item in data])
            items = []
            for item in data:
                output = {'level':'重要','impact':'利好','reason':'新的经营事实。','followup':'',
                          'matched_targets':[item['targets'][0]['key']]}
                if fail[0] and '待恢复' in item['article']['title']:
                    output['untrusted_extra'] = 'must-not-persist'
                items.append({'task_id':item['task_id'],'analysis':output})
            return {'results':items}
        analyzer = DeepSeekAnalyzer(transport)
        fetcher = Fetcher([article('successful','合成企业公告新订单100万元'),
                           article('retry','合成企业公告待恢复新订单200万元')])
        first = scan(path,fetcher,analyzer)
        assert first.status == 'partial' and first.analysis_succeeded == 1
        assert len(calls) == 1 and len(calls[0]) == 2
        assert scan(path,fetcher,analyzer,now=NOW+timedelta(minutes=1)).analysis_tasks == 0
        fail[0] = False
        second = scan(path,fetcher,analyzer,now=NOW+timedelta(minutes=6))
        assert second.status == 'succeeded' and second.analysis_tasks == 1
        assert len(calls) == 2 and len(calls[1]) == 1
        assert len(candidates(conn)) == 2
        assert conn.execute('SELECT COUNT(*) FROM subscription_analysis').fetchone()[0] == 2
        assert conn.execute('SELECT COUNT(*) FROM model_usage').fetchone()[0] == 2
    finally:
        conn.close()


def test_budget_exhaustion_defers_without_spending_attempt_then_recovers(tmp_path):
    path,conn,_,_ = make_state(tmp_path)
    try:
        calls = []
        def transport(*,model,prompt,timeout):
            data = json.loads(prompt.split('<UNTRUSTED_TASKS>\n',1)[1].split('\n</UNTRUSTED_TASKS>',1)[0])
            calls.append(data)
            return {'results':[{'task_id':item['task_id'],'analysis':{
                'level':'重要','impact':'利好','reason':'新订单。','followup':'',
                'matched_targets':[item['targets'][0]['key']]}} for item in data]}
        record_usage(path,{'category':'news','status':'success','total_tokens':60000},now=NOW)
        analyzer,fetcher = DeepSeekAnalyzer(transport),Fetcher([article('budget-deferred')])
        result = scan(path,fetcher,analyzer)
        assert result.status == 'succeeded'
        assert result.deferred >= 1 and result.analysis_tasks == 0
        assert calls == [] and not candidates(conn)
        row = conn.execute('SELECT attempts,state,last_error_code FROM analysis_attempts').fetchone()
        assert tuple(row) == (0,'failed','budget_deferred')
        assert conn.execute('SELECT COUNT(*) FROM member_evaluations').fetchone()[0] == 0
        recovered = scan(path,fetcher,analyzer,now=NOW+timedelta(hours=1))
        assert recovered.analysis_tasks == 1 and recovered.queued == 1
        assert len(calls) == 1
    finally:
        conn.close()


def test_config_drift_during_model_call_discards_stale_success(tmp_path):
    path,conn,_,members = make_state(tmp_path)
    try:
        class ChangeFocus(FakeAnalyzer):
            def analyze(self,item,targets):
                result = super().analyze(item,targets)
                MemberConfigStore(conn,members[0].id).apply_operation(
                    {'op':'set_focus','target':'123456.SH','focus':'偿债风险'},
                    actor=members[0].user_id)
                return result
        result = scan(path,Fetcher([article('config-drift')]),ChangeFocus())
        assert result.queued == 0 and result.analysis_succeeded == 0
        assert conn.execute('SELECT COUNT(*) FROM subscription_analysis').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM member_evaluations').fetchone()[0] == 0
        assert not candidates(conn)
    finally:
        conn.close()


def test_daily_token_budget_resets_at_beijing_midnight(tmp_path):
    path,conn,_,_ = make_state(tmp_path)
    try:
        before = datetime(2026,10,23,15,59,tzinfo=timezone.utc)
        after = before+timedelta(minutes=1)
        record_usage(path,{'category':'news','status':'success','total_tokens':150},now=before)
        assert budget_status(path,now=before)['day_used'] == 150
        assert budget_status(path,now=after)['day_used'] == 0
        record_usage(path,{'category':'news','status':'success','total_tokens':7},now=after)
        assert budget_status(path,now=after)['day_used'] == 7
        assert budget_status(path,now=after)['hour_used'] == 7
    finally:
        conn.close()


def test_major_reserve_cannot_fund_ordinary_tasks_in_same_scope(tmp_path):
    path,conn,_,_ = make_state(tmp_path)
    try:
        record_usage(path,{'category':'news','status':'success','total_tokens':80000},now=NOW)
        calls = []
        def transport(*,model,prompt,timeout):
            data = json.loads(prompt.split('<UNTRUSTED_TASKS>\n',1)[1].split('\n</UNTRUSTED_TASKS>',1)[0])
            calls.append(data)
            return {'choices':[{'message':{'content':json.dumps({'results':[
                {'task_id':item['task_id'],'analysis':{
                    'level':'重大','impact':'利空','reason':'公告确认重大事故导致停产。','followup':'',
                    'matched_targets':[item['targets'][0]['key']]}} for item in data]})}}],
                    'usage':{'prompt_tokens':800,'completion_tokens':200,'total_tokens':1000}}
        items = [article('reserve-major','合成企业公告重大事故导致停产'),
                 article('reserve-normal','合成企业公告新订单200万元')]
        result = scan(path,Fetcher(items),DeepSeekAnalyzer(transport),
                      budget_limits={'hour_limit':100000,'day_limit':100000,'major_reserve_fraction':.2})
        assert result.analysis_tasks == 1 and result.queued == 1
        assert len(calls) == len(calls[0]) == 1
        assert '重大事故' in calls[0][0]['article']['title']
        assert result.deferred >= 1
        assert len(candidates(conn)) == 1
        assert conn.execute('SELECT COUNT(*) FROM member_evaluations').fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize('name,value',[
    ('NEWS_HOURLY_TOKEN_BUDGET','-1'),('NEWS_HOURLY_TOKEN_BUDGET','1.5'),
    ('NEWS_DAILY_TOKEN_BUDGET','invalid'),('NEWS_DAILY_TOKEN_BUDGET','-20'),
    ('NEWS_MAJOR_RESERVE_FRACTION','1'),('NEWS_MAJOR_RESERVE_FRACTION','-0.1'),
    ('NEWS_MAJOR_RESERVE_FRACTION','nan'),('NEWS_MAJOR_RESERVE_FRACTION','inf'),
])
def test_invalid_budget_settings_are_rejected(monkeypatch,name,value):
    monkeypatch.setenv('NEWS_HOURLY_TOKEN_BUDGET','60000')
    monkeypatch.setenv('NEWS_DAILY_TOKEN_BUDGET','300000')
    monkeypatch.setenv('NEWS_MAJOR_RESERVE_FRACTION','0.2')
    monkeypatch.setenv(name,value)
    with pytest.raises(ValueError,match=name):
        Settings.from_env()


def test_configurable_budget_settings_accept_zero_and_reserve_fraction(monkeypatch):
    monkeypatch.setenv('NEWS_HOURLY_TOKEN_BUDGET','0')
    monkeypatch.setenv('NEWS_DAILY_TOKEN_BUDGET','65432')
    monkeypatch.setenv('NEWS_MAJOR_RESERVE_FRACTION','0.35')
    settings = Settings.from_env()
    assert settings.news_hourly_token_budget == 0
    assert settings.news_daily_token_budget == 65432
    assert settings.news_major_reserve_fraction == .35


def measured_batch_transport(calls):
    def transport(*,model,prompt,timeout):
        data = json.loads(prompt.split('<UNTRUSTED_TASKS>\n',1)[1].split('\n</UNTRUSTED_TASKS>',1)[0])
        calls.append(data)
        return {'choices':[{'message':{'content':json.dumps({'results':[
            {'task_id':item['task_id'],'analysis':{'level':'重要','impact':'利好',
                'reason':'新增经营订单。','followup':'',
                'matched_targets':[item['targets'][0]['key']]}} for item in data]})}}],
                'usage':{'prompt_tokens':80,'completion_tokens':20,'total_tokens':100}}
    return transport


def test_batch_splits_to_fit_budget_without_deferring_affordable_individuals(tmp_path):
    path,conn,_,_ = make_state(tmp_path)
    try:
        calls = []
        items = [article(f'large-summary-{i}',f'合成企业公告新订单{100+i}万元',
                         summary='新订单。'+'经营'*1998) for i in range(12)]
        result = scan(path,Fetcher(items),DeepSeekAnalyzer(measured_batch_transport(calls)))
        assert result.status == 'succeeded'
        assert result.analysis_tasks == result.analysis_succeeded == 10
        assert sum(map(len,calls)) == 10
        assert max(map(len,calls)) < 5
        assert result.analyzed == len(calls)
        assert conn.execute('SELECT SUM(logical_tasks) FROM model_usage').fetchone()[0] == 10
        assert conn.execute('SELECT COUNT(*) FROM model_usage').fetchone()[0] == len(calls)
        assert conn.execute("SELECT COUNT(*) FROM analysis_attempts WHERE state='succeeded' AND attempts=1").fetchone()[0] == 10
        assert conn.execute("SELECT COUNT(*) FROM model_budget_reservations WHERE state='reserved'").fetchone()[0] == 0
    finally:
        conn.close()


def test_batch_input_limit_splits_and_preserves_every_original_task(tmp_path):
    path,conn,_,_ = make_state(tmp_path)
    try:
        calls = []
        class TightInputAnalyzer(DeepSeekAnalyzer):
            def estimate_batch_tokens(self,tasks):
                if len(tasks) > 2:
                    raise ValueError('batch_input_limit_exceeded')
                return super().estimate_batch_tokens(tasks)
        items = [article(f'input-cap-{i}',f'合成企业公告新订单{100+i}万元') for i in range(5)]
        result = scan(path,Fetcher(items),TightInputAnalyzer(measured_batch_transport(calls)))
        assert result.status == 'succeeded'
        assert result.analysis_tasks == result.analysis_succeeded == 5
        assert [len(batch) for batch in calls] == [2,2,1]
        assert len({item['task_id'] for batch in calls for item in batch}) == 5
        assert conn.execute('SELECT SUM(logical_tasks) FROM model_usage').fetchone()[0] == 5
    finally:
        conn.close()


def test_single_task_over_input_limit_is_visible_without_failing_other_tasks(tmp_path):
    path,conn,_,_ = make_state(tmp_path)
    try:
        calls = []
        class OneOversizedAnalyzer(DeepSeekAnalyzer):
            def estimate_batch_tokens(self,tasks):
                if any('超限' in item[1].title for item in tasks):
                    raise ValueError('batch_input_limit_exceeded')
                return super().estimate_batch_tokens(tasks)
        items = [article('oversized-task','合成企业公告超限新订单999万元'),
                 *[article(f'normal-{i}',f'合成企业公告新订单{100+i}万元') for i in range(3)]]
        result = scan(path,Fetcher(items),OneOversizedAnalyzer(measured_batch_transport(calls)))
        assert result.status == 'succeeded'
        assert result.analysis_tasks == result.analysis_succeeded == 3
        assert sum(map(len,calls)) == 3
        assert all('超限' not in item['article']['title'] for batch in calls for item in batch)
        assert result.deferred == 1
        row = conn.execute("SELECT attempts,last_error_code FROM analysis_attempts WHERE state='failed'").fetchone()
        assert tuple(row) == (0,'input_limit_exceeded')
        assert conn.execute("SELECT COUNT(*) FROM quality_policy_audit WHERE reason_code='input_limit_exceeded'").fetchone()[0] == 1
    finally:
        conn.close()


def test_real_100kb_input_limit_splits_valid_long_configuration(tmp_path):
    # A valid supplementary-plane glyph occupies four UTF-8 bytes; counting
    # configured characters alone must not bypass the request byte ceiling.
    glyph = '\U00020bb7'
    path,conn,_,members = make_state(tmp_path,stock_name='合成企业'+glyph*196,
                                   stock_focus='经营订单'+glyph*996)
    try:
        calls = []
        items = [replace(article(f'real-input-{i}',f'订单{i}'+glyph*497,
                                 summary='经营订单'+glyph*3996,
                                 url=f'https://example.invalid/{i}/'+'u'*970),
                         source=glyph*100) for i in range(5)]
        service = SubscriptionMonitorService(path,Fetcher(items),DeepSeekAnalyzer(measured_batch_transport(calls)),
            corp_id=CORP,environ={'MONITOR_ENABLED':'false'},
            budget_limits={'hour_limit':1000000,'day_limit':1000000})
        snapshot = MemberConfigStore(conn,members[0].id).snapshot()
        from stock_watch.news import NewsTarget
        target = NewsTarget.from_value(snapshot.targets[0])
        with pytest.raises(ValueError,match='batch_input_limit_exceeded'):
            build_batch_prompt([(f'task-{i}',replace(item,target_keys=(target.key,)),(target,))
                                for i,item in enumerate(items)])
        result = service.run_once(now=NOW,force=True)
        assert result.status == 'succeeded'
        assert result.analysis_tasks == result.analysis_succeeded == 5
        assert len(calls) == 2 and sum(map(len,calls)) == 5
        assert all(1 <= len(batch) < 5 for batch in calls)
        assert conn.execute('SELECT SUM(logical_tasks) FROM model_usage').fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(*) FROM model_budget_reservations WHERE state='reserved'").fetchone()[0] == 0
    finally:
        conn.close()
