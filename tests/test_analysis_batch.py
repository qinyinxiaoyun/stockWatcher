"""Offline batch identity, per-task recovery, measured usage and budget checks."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from stock_watch.analyzer import (AnalysisResult, AnalysisValidationError,
    DeepSeekAnalyzer, DeepSeekHTTPTransport, build_batch_prompt, validate_analysis)
from stock_watch.model_usage import (SCHEMA_SQL, budget_status, record_usage,
    recover_reservations, reserve_budget, settle_budget)
from stock_watch.news import NewsArticle, NewsTarget
from stock_watch.intent import DeepSeekIntentClient, IntentError
from stock_watch.settings import Settings

NOW = datetime(2026, 10, 23, 2, tzinfo=timezone.utc)
TARGET = NewsTarget('batch-test', '合成企业', '合成企业', focus=('新订单',))
ARTICLE = NewsArticle(key='synthetic',title='合成企业签订新订单',
    url='https://example.invalid/synthetic',published_at=NOW,target_keys=(TARGET.key,))
VALID = {'level':'重要','impact':'利好','reason':'新增订单支持业务增长。',
         'followup':'核实交付安排。','matched_targets':[TARGET.key]}
USAGE = {'prompt_tokens':40,'completion_tokens':20,'total_tokens':60,
         'prompt_cache_hit_tokens':30,'prompt_cache_miss_tokens':10}


def tasks(count=2):
    return [(f'task-{i}',ARTICLE,(TARGET,)) for i in range(count)]


def envelope(items):
    return {'results':[{'task_id':task_id,'analysis':analysis} for task_id,analysis in items]}


@pytest.mark.parametrize('bad_items', [
    [('task-0',VALID)],
    [('task-0',VALID),('task-0',VALID)],
    [('task-0',VALID),('unknown',VALID)],
    [(None,VALID),('task-1',VALID)],
])
def test_batch_identity_ambiguity_fails_every_item(bad_items):
    analyzer = DeepSeekAnalyzer(lambda **kw:envelope(bad_items))
    results = analyzer.analyze_batch(tasks())
    assert set(results) == {'task-0','task-1'}
    assert all(isinstance(value,Exception) for value in results.values())


def test_batch_bad_item_preserves_independently_validated_sibling():
    analyzer = DeepSeekAnalyzer(lambda **kw:envelope([
        ('task-0',VALID),('task-1',{**VALID,'shell':'sensitive-untrusted-value'})]))
    results = analyzer.analyze_batch(tasks())
    assert isinstance(results['task-0'],AnalysisResult)
    assert isinstance(results['task-1'],AnalysisValidationError)
    assert 'sensitive' not in str(results['task-1'])
    assert analyzer.request_count == 1
    assert analyzer.logical_task_count == 2
    records = analyzer.drain_usage_records()
    assert records[0]['logical_tasks'] == 2
    assert records[0]['total_tokens'] is None
    assert records[0]['budget_charge'] > 0
    assert analyzer.drain_usage_records() == []


def test_batch_does_not_accept_another_targets_judgment():
    other = NewsTarget('other-test', '另一合成企业', '另一合成企业', focus=('重大风险',))
    other_article = NewsArticle(key='other',title='另一合成企业公告',
        url='https://example.invalid/other',published_at=NOW,target_keys=(other.key,))
    analyzer = DeepSeekAnalyzer(lambda **kw:envelope([
        ('first',VALID),('second',VALID)]))
    results = analyzer.analyze_batch([('first',ARTICLE,(TARGET,)),
                                      ('second',other_article,(other,))])
    assert isinstance(results['first'],AnalysisResult)
    assert isinstance(results['second'],AnalysisValidationError)


@pytest.mark.parametrize('raw',['not-json','{"results":[]}',
    '{"results":[{"task_id":"task-0","task_id":"task-1","analysis":{}}]}'])
def test_batch_malformed_json_never_guesses_success(raw):
    result = DeepSeekAnalyzer(lambda **kw:raw).analyze_batch(tasks())
    assert all(isinstance(item,Exception) for item in result.values())


def test_batch_input_limits_and_shared_prefix():
    analyzer = DeepSeekAnalyzer(lambda **kw:None)
    with pytest.raises(ValueError):
        analyzer.analyze_batch(tasks(6))
    with pytest.raises(ValueError):
        analyzer.analyze_batch([tasks(1)[0]]*2)
    prompt = build_batch_prompt(tasks(5))
    assert prompt.count('你是证券新闻分类器') == 1
    assert analyzer.estimate_batch_tokens(tasks(5)) > 6000
    huge = NewsTarget('batch-test','合成企业','合成企业',focus=('字'*100_000,))
    with pytest.raises(ValueError,match='batch_input_limit_exceeded'):
        analyzer.analyze_batch([('oversize',ARTICLE,(huge,))])
    assert analyzer.request_count == 0


def test_no_target_is_valid_only_for_explicit_no_notification():
    result = validate_analysis({**VALID,'matched_targets':[],'should_notify':False},
                               allowed_target_keys=[TARGET.key])
    assert not result.should_notify
    with pytest.raises(AnalysisValidationError):
        validate_analysis({**VALID,'matched_targets':[]},allowed_target_keys=[TARGET.key])


@pytest.mark.parametrize('changes',[
    {'is_fundamental':'true'},{'event_type':'execute_sql'},
    {'event_date':'2026-02-30'},{'new_facts':['x']*6},
])
def test_optional_event_fields_are_constrained(changes):
    with pytest.raises(AnalysisValidationError):
        validate_analysis({**VALID,**changes},allowed_target_keys=[TARGET.key])


class Response:
    def __init__(self,payload):
        self.payload = json.dumps(payload).encode()
    def __enter__(self):
        return self
    def __exit__(self,*args):
        pass
    def read(self,size):
        return self.payload[:size]


def test_real_transport_keeps_usage_and_scales_output_on_truncation(monkeypatch):
    requests = []
    def opening(request,timeout):
        requests.append(json.loads(request.data))
        return Response({'choices':[{'finish_reason':'length','message':{'content':'incomplete'}}],
                         'usage':USAGE,'system_fingerprint':'fp_synthetic'})
    monkeypatch.setattr('stock_watch.analyzer.urlopen',opening)
    analyzer = DeepSeekAnalyzer(DeepSeekHTTPTransport(api_key='synthetic-only'),max_attempts=1)
    assert all(isinstance(item,Exception) for item in analyzer.analyze_batch(tasks(5)).values())
    record = analyzer.drain_usage_records()[0]
    assert requests[0]['max_tokens'] == 6000
    assert record['total_tokens'] == record['budget_charge'] == 60
    assert record['prompt_cache_hit_tokens'] == 30
    assert record['system_fingerprint'] == 'fp_synthetic'
    assert record['status'] == 'truncated'
    assert not {'prompt','content','api_key'} & set(record)


def test_full_envelope_usage_survives_analysis_validation_error():
    analyzer = DeepSeekAnalyzer(lambda **kw:{'choices':[{'message':{'content':'not json'}}],
                                            'usage':USAGE})
    with pytest.raises(AnalysisValidationError):
        analyzer.analyze(ARTICLE,[TARGET])
    assert analyzer.drain_usage_records()[0]['total_tokens'] == 60


def test_each_network_retry_has_unknown_charge_and_safe_error():
    def transport(**kwargs):
        raise TimeoutError('synthetic-secret-must-not-leak')
    analyzer = DeepSeekAnalyzer(transport,max_attempts=2,sleeper=lambda _:None)
    with pytest.raises(RuntimeError) as error:
        analyzer.analyze(ARTICLE,[TARGET])
    assert 'synthetic-secret' not in str(error.value)
    assert error.value.__suppress_context__
    records = analyzer.drain_usage_records()
    assert len(records) == 2
    assert all(item['total_tokens'] is None and item['budget_charge'] > 0 for item in records)


@pytest.fixture
def usage_db(tmp_path):
    path = tmp_path/'usage.sqlite'
    conn = sqlite3.connect(path,isolation_level=None)
    conn.executescript(SCHEMA_SQL)
    conn.close()
    return path


def test_budget_reserves_major_capacity_and_settles_actual_idempotently(usage_db):
    options = dict(now=NOW,hour_limit=100,day_limit=1000)
    first = reserve_budget(usage_db,estimated_tokens=80,**options)
    assert first
    assert reserve_budget(usage_db,estimated_tokens=1,**options) is None
    major = reserve_budget(usage_db,estimated_tokens=20,suspected_major=True,**options)
    assert major
    assert reserve_budget(usage_db,estimated_tokens=1,suspected_major=True,**options) is None
    settle_budget(usage_db,first,{'total_tokens':25,'status':'success'},now=NOW)
    settle_budget(usage_db,first,{'total_tokens':99,'status':'success'},now=NOW)
    status = budget_status(usage_db,**options)
    assert status['hour_used'] == 25
    assert status['hour_reserved'] == 20
    with sqlite3.connect(usage_db) as conn:
        assert conn.execute('SELECT COUNT(*) FROM model_usage').fetchone()[0] == 1


def test_unknown_usage_and_restart_recovery_do_not_invent_actuals(usage_db):
    token = reserve_budget(usage_db,estimated_tokens=200,now=NOW)
    assert token
    assert recover_reservations(usage_db,before=NOW+timedelta(seconds=1),now=NOW) == 1
    with sqlite3.connect(usage_db) as conn:
        row = conn.execute('SELECT total_tokens,budget_charge,status FROM model_usage').fetchone()
    assert row == (None,200,'interrupted')
    assert budget_status(usage_db,now=NOW)['unknown_attempts'] == 1


def test_record_keeps_caller_transaction_and_strips_content(usage_db):
    with sqlite3.connect(usage_db,isolation_level=None) as conn:
        conn.execute('BEGIN IMMEDIATE')
        record_usage(conn,{'category':'news','status':'success','total_tokens':12,
                           'prompt':'private-content','system_fingerprint':'private content'})
        assert conn.in_transaction
        conn.rollback()
        assert conn.execute('SELECT COUNT(*) FROM model_usage').fetchone()[0] == 0


def test_intent_validation_failure_records_its_own_usage_category(usage_db):
    class Snapshot:
        def to_dict(self):
            return {'config_version':1,'stocks':[],'themes':[]}
    settings = Settings(data_dir=usage_db.parent,database_path=usage_db)
    response = {'choices':[{'message':{'content':'not-json'}}],'usage':USAGE}
    client = DeepSeekIntentClient(settings,lambda payload:response,usage_db_path=usage_db)
    with pytest.raises(IntentError):
        client.parse('合成请求',Snapshot())
    with sqlite3.connect(usage_db) as conn:
        assert conn.execute('SELECT category,status,total_tokens FROM model_usage').fetchone() == (
            'intent','validation_failure',60)


def test_validation_diagnostics_keep_only_known_static_codes():
    from stock_watch.analyzer import AnalysisValidationError
    from stock_watch.analysis_jobs import safe_error_code
    error = AnalysisValidationError('level must be one of the allowed values')
    assert safe_error_code(AnalysisValidationError(error.code)) == 'invalid_level'
    assert safe_error_code(AnalysisValidationError('batch_task_identity_mismatch')) == 'batch_task_identity_mismatch'
    assert safe_error_code(AnalysisValidationError('private synthetic model response')) == 'invalid_analysis'
