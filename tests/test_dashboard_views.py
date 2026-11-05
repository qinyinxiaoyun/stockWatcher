from stock_watch.dashboard_views import render_login, render_overview, render_stock, safe_url


def test_login_escapes_and_uses_external_stylesheet():
    html = render_login('<script>x</script>', '"bad', username='<x>')
    assert '<script>' not in html
    assert '&lt;script&gt;' in html
    assert 'name="csrf"' in html
    assert '/assets/dashboard.css' in html
    assert '<style' not in html and 'style=' not in html


def test_source_urls_and_research_escape():
    view = {'symbol':'X/evil', 'name':'<img>', 'research':{'status':'needs_update','baseline':{'document':{'sections':{'business_model':{'statements':[{'kind':'fact','text':'<script>bad</script>'}]}},'sources':[{'title':'bad','url':'javascript:alert(1)'}]}}}}
    html = render_stock(view)
    assert '<script>' not in html and '<img>' not in html
    assert 'javascript:' not in html
    assert '需要更新' in html and '商业模式' in html


def test_url_policy():
    for url in ['javascript:x','https://user:pass@host/a','https://host/\nfoo','//host/a','https://host/\\foo']:
        assert safe_url(url) is None
    assert safe_url('https://example.com/a?q=1')


def test_overview_empty_and_stock_link():
    assert '暂无匹配' in render_overview({})
    html = render_overview({'stocks':[{'symbol':'X/Y','name':'<hello>','research':{}}]})
    assert '/stocks/X%2FY' in html
    assert '&lt;hello&gt;' in html
    assert 'name="csrf"' in html


def test_unverified_period_and_readiness_provenance():
    from stock_watch.dashboard_views import local_time
    r = {'financial_period':'2026Q3','status':'needs_update','as_of':'2026-11-03T00:00:00+00:00','readiness':{'reasons':['latest_financial_period_not_reconciled']},'baseline':{'document':{'financial_review':{'period':'2026Q3','latest_available_checked':False},'sections':{'business_model':{'statements':[{'kind':'fact','text':'<&','evidence_ids':['a"b']}]}},'sources':[{'id':'a"b','title':'原始材料','url':'https://example.com/a'}]}}}
    html = render_stock({'symbol':'X','research':r})
    assert '2026Q3 待核；已核历史数据见正文' in html
    assert '已核报告期 2026Q3' not in html
    assert '最新财报尚未完成核对' in html
    assert 'href="#source-a%22b"' in html and 'id="source-a%22b"' in html
    assert '&lt;&amp;' in html
    assert local_time(r['as_of']) == '2026-11-03 08:00 北京时间'


def test_current_update_historical_label_and_escape():
    from stock_watch.dashboard_views import render_stock
    page = render_stock(dict(name='Example',symbol='US:EX',research=dict(historical=True,
        current_update=dict(last_checked=None,last_updated=None,covered_through=None,
            gaps=['<script>attack</script>'],task_status='committed_export_pending',export_status='pending'))))
    assert '不代表所选历史版本当时的检查结果' in page
    assert '待恢复导出' in page
    assert '&lt;script&gt;attack&lt;/script&gt;' in page
    assert '<script>attack</script>' not in page
