"""Escaped, dependency-free HTML views for the read-only portal."""
from datetime import datetime, timedelta, timezone
from html import escape
from urllib.parse import quote, urlsplit

CSS = '''*{box-sizing:border-box}body{margin:0;background:#f4f5f2;color:#243b42;font:16px/1.65 system-ui,sans-serif}a{color:#12675e}a:hover{text-decoration:underline}a:focus-visible,button:focus-visible,input:focus-visible{outline:3px solid #d58d26;outline-offset:4px}.skip{position:absolute;left:-9999px}.skip:focus{left:20px;top:12px;background:white;padding:12px}aside{position:fixed;width:220px;height:100vh;background:#153e43;color:white;padding:32px 24px}aside a{display:block;color:white;margin:20px 0}main{margin-left:220px;padding:36px 5%;max-width:1500px}header{display:flex;justify-content:space-between;gap:20px;align-items:center}h1{font-size:32px;line-height:1.3}h2{margin-top:38px}h3{margin-top:0}.muted{color:#64777a;font-size:14px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:20px}.card{background:white;border:1px solid #dce3dd;border-radius:14px;padding:24px;margin:12px 0}.stat{font-size:30px;font-weight:700}.chip{display:inline-block;background:#eaf1ed;padding:3px 10px;border-radius:18px;margin:4px;font-size:13px}.warn{background:#fff0d9;color:#835b20}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:12px;border-bottom:1px solid #e3e8e2;vertical-align:top}input,button{font:inherit;border:1px solid #bbc9c4;border-radius:8px;padding:10px 14px}button{background:#176359;color:white;cursor:pointer}input{max-width:100%;background:white}label{display:block;margin:16px 0 6px}.login{margin:8vh auto;max-width:460px;padding:32px}.login input{width:100%}.login button{margin-top:24px;width:100%}p{overflow-wrap:anywhere}.section-nav a{display:inline-block;margin:5px 14px 5px 0}@media(max-width:760px){aside{position:static;width:auto;height:auto;padding:16px 24px}aside a{display:inline-block;margin:8px 18px 0 0}main{margin:0;padding:24px}header{align-items:flex-start;flex-wrap:wrap}h1{font-size:27px}.grid{grid-template-columns:1fr}}'''
DASHBOARD_CSS = CSS


def e(value):
    return escape(str(value if value is not None else '—'), quote=True)


def local_time(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M 北京时间')
    except (ValueError, TypeError):
        return '—'


def period_label(research):
    review = (research.get('baseline') or {}).get('document', {}).get('financial_review', {})
    period = research.get('financial_period') or review.get('period')
    if not period:
        return '报告期尚待核对'
    if not review.get('latest_available_checked', research.get('financial_verified', False)):
        return e(period) + ' 待核；已核历史数据见正文'
    return '已核报告期 ' + e(period)


def evidence_links(values):
    return ' '.join('<a class="chip" href="#source-' + e(quote(str(value), safe='')) + '">来源 ' + e(value) + '</a>' for value in values)


def stock_url(symbol):
    return '/stocks/' + quote(str(symbol), safe='')


def safe_url(value):
    value = str(value or '')
    if any(ord(c) <= 32 or ord(c) == 127 for c in value) or '\\' in value:
        return None
    try:
        p = urlsplit(value)
        if p.scheme in ('http', 'https') and p.hostname and not p.username and not p.password:
            return value
    except ValueError:
        pass
    return None


def _page(title, body, csrf_token='', login=False):
    nav = '' if login else '<aside><strong>Stock Watch</strong><p>研讯观察 · 订阅与研究</p><nav aria-label="主导航"><a href="/">总览</a><a href="/#members">成员订阅</a><a href="/#stocks">股票研究</a></nav><form action="/logout" method="post"><input type="hidden" name="csrf" value="' + e(csrf_token) + '"><button>退出登录</button></form></aside>'
    return '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>' + e(title) + ' · Stock Watch</title><link rel="stylesheet" href="/assets/dashboard.css"></head><body><a class="skip" href="#main">跳到内容</a>' + nav + ('<main id="main" class="login">' if login else '<main id="main">') + body + '</main></body></html>'


def _status(research):
    return {'usable':'研究可用','partial':'部分可用 · 待核','needs_update':'需要更新','queued':'等待研究','running':'研究整理中','failed':'研究待核','not_started':'尚无研究'}.get(research.get('status'), '研究待核')


def render_overview(view, csrf_token=''):
    members, stocks = view.get('members', []), view.get('stocks', [])
    usable = sum(s.get('research', {}).get('status') == 'usable' for s in stocks)
    enabled = sum(bool(s.get('enabled')) and not s.get('paused') and bool(m.get('active')) for m in members for s in m.get('stocks', []))
    body = '<header><div><p class="muted">组织订阅工作台</p><h1>订阅与研究总览</h1></div><div class="muted">快照时间 ' + e(local_time(view.get('snapshot_at'))) + '<br><a href="/">刷新概览</a></div></header><form method="get" action="/"><label for="q">搜索成员、股票或主题</label><input id="q" name="q" value="' + e(view.get('query', '')) + '" placeholder="成员 / 证券代码 / 公司名称"><button>搜索</button></form><div class="grid">'
    for label, count in [('成员数',view.get('counts',{}).get('members',len(members))),('启用关注',view.get('counts',{}).get('active_stocks',enabled)),('研究可用',view.get('counts',{}).get('usable_research',usable)),('待核研究',view.get('counts',{}).get('pending_research',len(stocks)-usable))]:
        body += '<div class="card"><div class="muted">'+label+'</div><div class="stat">'+e(count)+'</div></div>'
    body += '</div><h2 id="members">成员订阅</h2><div class="grid">'
    for m in members:
        body += '<article class="card"><h3>'+e(m.get('user_id'))+'</h3><p class="muted">'+e({'admin':'管理员','member':'成员','legacy':'共享订阅'}.get(m.get('role'),'成员'))+' · '+('启用' if m.get('active') else '已停用')+'</p>'
        body += '<p class="muted">配置版本 '+e(m.get('config_version'))+'</p>'
        for s in m.get('stocks',[]):
            body += '<a class="chip" href="'+e(stock_url(s.get('symbol')))+'">'+e(s.get('name'))+' '+e(s.get('symbol'))+' · '+('关注中' if s.get('enabled') and not s.get('paused') else '已暂停')+'</a><p class="muted">方向 '+e({'both':'双向','up':'向上','down':'向下'}.get(s.get('direction'),s.get('direction')))+' · 等级 '+e(s.get('push_level'))+'<br>关注点 '+e(s.get('focus'))+'</p>'
        for t in m.get('themes',[]):
            body += '<span class="chip">主题：'+e(t.get('name'))+' · '+('启用' if t.get('enabled') else '停用')+'</span><p class="muted">关键词：'+e('、'.join(t.get('keywords',[])))+'</p>'
        body += '</article>'
    body += '</div><h2 id="stocks">股票研究</h2><div class="grid">'
    for s in stocks:
        r = s.get('research',{})
        body += '<article class="card"><span class="chip">'+_status(r)+'</span><h3><a href="'+e(stock_url(s.get('symbol')))+'">'+e(s.get('name'))+' · '+e(s.get('symbol'))+'</a></h3><p>关注人数 '+e(len(s.get('watchers',[])))+'</p><p class="muted">'+period_label(r)+'<br>材料截止 '+e(local_time(r.get('as_of')))+'</p></article>'
    body += '</div>'
    if not members and not stocks:
        body += '<div class="card">暂无匹配的订阅或研究。可清空搜索后查看全部内容。</div>'
    return _page('订阅概览',body,csrf_token)


SECTIONS = [('business_model','商业模式'),('industry_cycle','行业周期'),('competitive_advantage','竞争优势'),('earnings_drivers','盈利驱动'),('financial_quality','财务质量'),('governance','公司治理'),('scenarios_expectations','情景与预期'),('valuation','估值'),('risks','风险'),('catalysts','催化因素')]


def _table(rows, fields):
    if not rows:
        return '<p class="muted">暂无已核材料</p>'
    return '<div class="table-wrap"><table><thead><tr>'+''.join('<th scope="col">'+e(label)+'</th>' for _,label in fields)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+(evidence_links(row.get(key,[])) if key == 'evidence_ids' else e(row.get(key)))+'</td>' for key,_ in fields)+'</tr>' for row in rows)+'</tbody></table></div>'


def render_stock(view, csrf_token=''):
    r = view.get('research',{})
    body = '<a href="/#stocks">← 返回股票研究</a><header><h1>'+e(view.get('name'))+' <small>'+e(view.get('symbol'))+'</small></h1><span class="chip">'+_status(r)+'</span></header>'
    body += _table(r.get('securities',[]), [('symbol','证券'),('market','市场'),('currency','币种')])
    update = r.get('current_update', {})
    if update:
        body += '<section class="card"><h2>当前检查与导出状态</h2>'
        if r.get('historical'):
            body += '<p class="muted">以下为当前运行状态，不代表所选历史版本当时的检查结果。</p>'
        for key, label in [('last_checked','最近检查'),('last_updated','最近实质更新'),('covered_through','覆盖水位')]:
            body += '<p>'+label+'：'+e(local_time(update.get(key)))+'</p>'
        statuses = {'queued':'等待处理','running':'处理中','completed':'已完成','validated':'仅校验',
                    'no_change':'无实质变化','incomplete':'材料不完整','conflict':'版本冲突','failed':'处理失败',
                    'committed_export_pending':'已入库，等待导出'}
        body += '<p>最近任务状态：'+e(statuses.get(update.get('task_status'), '暂无任务'))+'</p>'
        body += '<p>导出状态：'+e({'exported':'已导出','pending':'待恢复导出','not_required':'本次无需导出'}.get(update.get('export_status'),'暂无记录'))+'</p>'
        gaps = update.get('gaps', [])
        body += '<p>覆盖缺口：'+(e('；'.join(str(gap) for gap in gaps)) if gaps else '暂无已记录缺口')+'</p></section>'
    baseline = r.get('baseline')
    if not baseline:
        return _page('股票研究',body+'<section class="card"><h2>研究尚待建立</h2><p>该股票已进入关注清单。研究材料完成整理与核验后，将在此展示。</p></section>',csrf_token)
    doc = baseline.get('document',{})
    body += '<p class="muted">材料截止 '+e(local_time(r.get('as_of')))+' · '+period_label(r)+' · 版本 '+e(baseline.get('version'))+'</p>'
    if r.get('historical'):
        body += '<p class="chip warn">当前查看历史版本</p>'
    body += '<div class="grid">'
    for key,label in [('trackable','事件可追踪'),('fundamental','基本面可参考'),('expectation_comparison','预期比较已验证'),('validated_prediction','预测能力已验证')]:
        body += '<div class="card">'+label+'<p>'+('已具备' if r.get('readiness',{}).get(key) else '尚未具备')+'</p></div>'
    reasons = r.get('readiness',{}).get('reasons',[])
    if reasons:
        labels = {'latest_financial_period_not_reconciled':'最新财报尚未完成核对','missing_financial_review':'缺少财务期间核验','missing_financial_metrics':'财务指标证据不足','missing_evidenced_monitor_mapping':'监控路径缺少可追溯证据','missing_evidenced_core_assumption':'核心假设缺少可追溯证据'}
        body += '</div><section class="card"><h2>研究待核事项</h2>' + ''.join('<p>'+e(labels.get(reason, '研究章节尚待补全或核验' if ':' in reason else '研究证据尚未满足完整核验要求'))+'</p>' for reason in reasons) + '</section>'
    body += ('' if reasons else '</div>') + '<nav class="section-nav"   aria-label="研究章节">'+''.join('<a href="#'+key+'">'+label+'</a>' for key,label in SECTIONS)+'</nav>'
    for key,label in SECTIONS:
        section = doc.get('sections',{}).get(key,{})
        body += '<section class="card" id="'+key+'"><h2>'+label+'</h2>'
        for statement in section.get('statements',[]):
            body += '<p><span class="chip">'+e({'fact':'事实','inference':'推断','assumption':'假设','unknown':'未知'}.get(statement.get('kind'),'待核'))+'</span>'+e(statement.get('text'))+' '+evidence_links(statement.get('evidence_ids',[]))+'</p>'
        if not section.get('statements'):
            body += '<p class="muted">本节暂无已核内容</p>'
        body += _table(section.get('metrics',[]), [('name','指标'),('value','数值'),('period','期间'),('unit','单位'),('currency','币种'),('basis','口径'),('evidence_ids','证据')]) if section.get('metrics') else ''
        body += '</section>'
    body += '<section class="card"><h2>假设与反证</h2>'+_table(doc.get('assumptions',[]), [('hypothesis','假设'),('counter_evidence','反证'),('review_condition','复核条件')])+'</section><section class="card"><h2>监控观察路径</h2>'+_table(doc.get('monitor_mappings',[]), [('event','事件'),('business','业务'),('metric','指标'),('transmission','影响路径'),('verify','验证方法')])+'</section><section class="card"><h2>原始来源</h2>'
    for source in doc.get('sources',[]):
        url = safe_url(source.get('url'))
        title = e(source.get('title'))
        body += '<p id="source-'+e(quote(str(source.get('id')),safe=''))+'">'+('<a rel="noopener noreferrer" href="'+e(url)+'">'+title+'</a>' if url else title)+'<br><span class="muted">'+e(source.get('locator'))+' · '+e(source.get('published_at'))+'</span></p>'
    body += '</section><section class="card"><h2>历史版本</h2>'
    for version in r.get('versions',[]):
        body += '<p><a href="'+e(stock_url(view.get('symbol'))+'?version='+quote(str(version.get('version')),safe=''))+'">版本 '+e(version.get('version'))+' · '+e(version.get('as_of'))+'</a></p>'
    return _page('股票研究',body+'</section>',csrf_token)


def render_login(message='', csrf_token='', next_path='/', username=''):
    body = '<p class="muted">STOCK WATCH · 研讯观察</p><h1>登录研究工作台</h1><p>查看成员订阅与股票研究基线。</p>'
    if message:
        body += '<p role="alert">'+e(message)+'</p>'
    body += '<form action="/login" method="post"><input type="hidden" name="csrf" value="'+e(csrf_token)+'"><input type="hidden" name="next" value="'+e(next_path)+'"><label for="username">用户名</label><input id="username" name="username" autocomplete="username" required value="'+e(username)+'"><label for="password">密码</label><input id="password" name="password" type="password" autocomplete="current-password" required><button>登录</button></form>'
    return _page('登录',body,login=True)


def render_error(message, status=503):
    return _page('暂时无法显示','<h1>暂时无法显示</h1><p>'+e(message)+'</p><a href="/">返回概览</a>')
