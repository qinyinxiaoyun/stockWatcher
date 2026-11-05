"""Local-only research preparation, archive import and readable export."""
from __future__ import annotations

import hashlib
import html
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from .research import ResearchStore, SECTIONS, _time


def add_parser(subparsers):
    parser = subparsers.add_parser('research', help='Local public research archive')
    commands = parser.add_subparsers(dest='research_command', required=True)
    prepare = commands.add_parser('prepare')
    sources = prepare.add_mutually_exclusive_group(required=True)
    sources.add_argument('--watchlist', type=Path)
    sources.add_argument('--from-subscriptions', action='store_true')
    ingest = commands.add_parser('import')
    ingest.add_argument('path', type=Path)
    commands.add_parser('list')
    show = commands.add_parser('show')
    show.add_argument('company_id')
    show.add_argument('--as-of')
    export = commands.add_parser('export')
    export.add_argument('--output', type=Path, required=True)
    commands.add_parser('jobs')
    review = commands.add_parser('review')
    review.add_argument('company_id')
    review.add_argument('--reason', required=True)
    return parser


def _summary(record):
    return {key: record[key] for key in ('company_id','version','as_of','created_at','readiness')} | {'research_status':record.get('research_status'), 'review_requests':record.get('review_requests',[]), 'name': record['document']['company']['name'], 'securities': record['document']['securities'], 'sources_count': len(record['document']['sources']), 'financial_period': record['document'].get('financial_review',{}).get('period')}


def _current(store):
    company_ids = sorted({record['company_id'] for record in store.list_baselines()})
    records = [store.get_baseline(company_id) for company_id in company_ids]
    statuses = {job['company_id']:job['status'] for job in store.list_jobs()}
    for record in records:
        record['research_status'] = statuses.get(record['company_id'], 'partial')
        record['review_requests'] = store.review_requests(record['company_id'])
    return records


def _subscription_securities(conn):
    independent = conn.execute("SELECT value FROM app_meta WHERE key='independent_subscriptions'").fetchone()
    if independent:
        rows = conn.execute('SELECT c.snapshot_json FROM member_configs c JOIN members m ON m.id=c.member_id WHERE m.active=1')
        stocks = [stock for row in rows for stock in json.loads(row[0]).get('stocks', [])]
    else:
        rows = conn.execute('SELECT symbol,name,market,enabled,paused FROM stocks')
        stocks = [dict(zip(('symbol','name','market','enabled','paused'), row)) for row in rows]
    return [{'symbol': stock['symbol'], 'name': stock['name'], 'market': stock['market']} for stock in stocks if stock.get('enabled', True) and not stock.get('paused', False)]


def _prepare(store, securities, now):
    prepared = set()
    for security in securities:
        if not isinstance(security, dict) or set(security) - {'symbol','name','company_id','market','currency'}:
            raise ValueError('invalid watchlist security')
        symbol = security['symbol']
        row = store.conn.execute('SELECT company_id,market,currency FROM research_securities WHERE symbol=?', (symbol,)).fetchone()
        if row:
            company_id, market, currency = row
        else:
            company_id = security.get('company_id', 'security:' + symbol)
            market = security.get('market')
            if market is None:
                market = 'HK' if symbol.endswith('.HK') or symbol.startswith('HK:') else 'US' if symbol.startswith('US:') else 'CN' if symbol.endswith(('.SH','.SZ')) or symbol.startswith(('SH:','SZ:')) else None
            currency = security.get('currency', {'HK':'HKD','US':'USD','CN':'CNY','SH':'CNY','SZ':'CNY','SSE':'CNY','SZSE':'CNY','SEHK':'HKD'}.get(market))
            if not market or not currency:
                raise ValueError('market and currency required for unknown security')
            store.upsert_company({'company_id':company_id,'name':security['name']}, [{'symbol':symbol,'market':market,'currency':currency}])
        store.ensure_preparation(company_id, now)
        prepared.add(company_id)
    return {'prepared': sorted(prepared)}


def _safe(value):
    # Escape HTML and Markdown metacharacters, including link delimiters.
    text = html.escape(str(value), quote=True)
    for char in ('\\','`','*','_','[',']','(',')','#','!','|'):
        text = text.replace(char, '\\' + char)
    return text.replace('\n', ' ')


def _markdown(record):
    doc = record['document']
    labels = dict(zip(SECTIONS, ('商业模式','行业、价值链与周期','竞争优势及反证','盈利驱动','财务质量与生存能力','治理与资本配置','内部情景与外部预期','估值与价格要求','风险、反证与证伪','催化与跟踪计划')))
    kinds = {'fact':'已核披露事实','inference':'研究推断','assumption':'待验证假设','unknown':'未知或待核实'}
    lines = ['# ' + _safe(doc['company']['name']), '', '公司标识：' + _safe(record['company_id']), '', '材料截止：' + _safe(record['as_of']), '本地建立：' + _safe(record['created_at']), '版本：' + str(record['version']), '', '## 研究能力', '']
    for key, label in (('trackable','可跟踪'),('fundamental','基础研究证据门槛'),('expectation_comparison','可比较事前预期'),('validated_prediction','可输出经过验证的收益预测')):
        lines.append('- ' + label + '：' + ('已具备' if record['readiness'][key] else '未具备'))
    lines.append('- 基线状态：' + _safe(record.get('research_status','未登记')))
    lines.append('- 本页为有证据的研究起点；内部条件情景不等于市场共识，证据门槛通过不代表收益预测有效。')
    if record['readiness'].get('reasons'):
        lines.append('- 待核缺口：' + _safe(', '.join(record['readiness']['reasons'])))
    lines += ['', '## 证券身份', '']
    lines += ['复核请求：' + _safe(json.dumps(record.get('review_requests',[]),ensure_ascii=False)), '']
    for security in doc['securities']:
        lines.append('- ' + _safe(json.dumps(security,ensure_ascii=False)))
    for name in SECTIONS:
        lines += ['', '## ' + labels[name], '']
        for statement in doc['sections'][name]['statements']:
            lines.append('- **' + kinds[statement['kind']] + '**：' + _safe(statement['text']) + '（证据：' + _safe(', '.join(statement['evidence_ids']) or '无；不作为已知事实') + '）')
        if doc['sections'][name].get('metrics'):
            lines += ['', '| 指标 | 期间 | 数值 | 单位 | 币种 | 口径 | 证据 |', '| --- | --- | --- | --- | --- | --- | --- |']
        for metric in doc['sections'][name].get('metrics', []):
            lines.append('| ' + ' | '.join(_safe(metric[k]) for k in ('name','period','value','unit','currency','basis')) + ' | ' + _safe(', '.join(metric['evidence_ids'])) + ' |')
    lines += ['', '## 关键假设表', '']
    for item in doc['assumptions']:
        lines += ['- 假设：' + _safe(item['hypothesis']), '  - 支持证据：' + _safe(', '.join(item['support_evidence_ids'])), '  - 反证：' + _safe(item['counter_evidence']), '  - 复核或修订条件：' + _safe(item['review_condition'])]
    lines += ['', '## 事件监控映射', '']
    for item in doc['monitor_mappings']:
        lines += ['- 事件：' + _safe(item['event']), '  - 业务/指标：' + _safe(item['business']) + ' / ' + _safe(item['metric']), '  - 经营传导：' + _safe(item['transmission']), '  - 需核材料：' + _safe(item['verify']), '  - 证据：' + _safe(', '.join(item['evidence_ids']))]
    lines += ['', '## 原始来源及时间', '']
    for source in doc['sources']:
        lines.append('- ' + _safe(source['id']) + ': ' + _safe(source['title']))
        # URLs were restricted to HTTP(S) by the archive validator. Encode link delimiters.
        lines.append('  [Original source](' + quote(source['url'], safe=':/?=&%+~#@') + ')')
        lines.append('  Original URL: ' + _safe(source['url']))
        lines.append('  ' + _safe(json.dumps({key:value for key,value in source.items() if key not in ('id','title','url')},ensure_ascii=False)))
    return '\n'.join(lines) + '\n'


def _safe_directory(path):
    path = Path(path).absolute()
    for parent in (*reversed(path.parents), path):
        if parent.is_symlink() or (parent.exists() and getattr(parent, 'is_junction', lambda: False)()):
            raise ValueError('research directory must not contain links')
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_text(target, content):
    if target.is_symlink():
        raise ValueError('export target must not be a symlink')
    fd, temporary = tempfile.mkstemp(prefix='.research-', dir=target.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if target.is_symlink():
            raise ValueError('export target must not be a symlink')
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _export_lock(output):
    # A stable sibling preserves the report-directory artifact contract.
    lock = output.parent / ('.research-export-' + hashlib.sha256(str(output).encode()).hexdigest()[:24] + '.lock')
    if lock.is_symlink() or getattr(lock, 'is_junction', lambda: False)():
        raise ValueError('invalid research export lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    locked = False
    try:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b'0')
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError('research export busy') from None
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise RuntimeError('research export busy') from None
        locked = True
        yield
    finally:
        if locked:
            if os.name == 'nt':
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _export(store, output):
    output = _safe_directory(output)
    with _export_lock(output):
        return _export_locked(store, output)


def _export_locked(store, output):
    index = ['# 已关注股票研究基线', '', '本地证据归档；没有生成收益概率或目标价。基础研究就绪与预期比较、经过验证的收益预测分别管理。', '', '| 公司 | 证券 | 已核财务期间 | 基础研究 | 当前状态 | 版本 |', '| --- | --- | --- | --- | --- | --- |']
    own_snapshot = not store.conn.in_transaction
    if own_snapshot:
        store.conn.execute('BEGIN')
    try:
        records = _current(store)
    finally:
        if own_snapshot:
            store.conn.rollback()  # read-only snapshot; never commit caller state
    for record in records:
        stem = hashlib.sha256(record['company_id'].encode()).hexdigest()[:24]
        for suffix, content in (('.md',_markdown(record)), ('.json',json.dumps(record['document'],ensure_ascii=False,sort_keys=True,indent=2)+'\n')):
            target = output / (stem + suffix)
            if target.is_symlink():
                raise ValueError('export target must not be a symlink')
            _atomic_text(target, content)
        doc = record['document']
        review = doc.get('financial_review',{})
        period = review.get('period','待核实') if review.get('latest_available_checked') else review.get('period','最新期') + '待核，已核历史指标见报告'
        index.append('| [' + _safe(doc['company']['name']) + '](' + stem + '.md) | ' + _safe(', '.join(s['symbol'] for s in doc['securities'])) + ' | ' + _safe(period) + ' | ' + ('可用' if record['readiness']['fundamental'] else '待核实') + ' | ' + _safe(record.get('research_status','未登记')) + ' | ' + str(record['version']) + ' |')
    target = output / 'index.md'
    if target.is_symlink():
        raise ValueError('export target must not be a symlink')
    _atomic_text(target, '\n'.join(index)+'\n')
    return {'output':str(output),'index':str(target),'companies':len(records)}


def run(args, conn):
    store = ResearchStore(conn)
    command = args.research_command
    now = datetime.now(timezone.utc).isoformat()
    if command == 'prepare':
        if args.from_subscriptions:
            securities = _subscription_securities(conn)
        else:
            watchlist = json.loads(Path(args.watchlist).read_text(encoding='utf-8-sig'))
            if not isinstance(watchlist, dict) or set(watchlist) != {'checked_at','securities'} or not isinstance(watchlist['securities'],list):
                raise ValueError('invalid watchlist')
            if _time(watchlist['checked_at']) > _time(now):
                raise ValueError('future watchlist check')
            securities = watchlist['securities']
        with store._atomic():
            return _prepare(store,securities,now)
    if command == 'import':
        path = Path(args.path)
        files = sorted(path.glob('*.json')) if path.is_dir() else [path]
        if not files:
            raise ValueError('no research JSON files')
        documents = [json.loads(file.read_text(encoding='utf-8-sig')) for file in files]
        with store._atomic():
            return {'imported':[_summary(store.append_baseline(doc,now)) for doc in documents]}
    if command == 'list':
        return [_summary(record) for record in _current(store)]
    if command == 'show':
        return store.get_baseline(args.company_id,args.as_of)
    if command == 'review':
        return store.mark_for_review(args.company_id,args.reason,now)
    if command == 'jobs':
        return store.list_jobs()
    if command == 'export':
        return _export(store,args.output)
    raise ValueError('unknown research command')
