"""One terminal surface over the monitor's single collected snapshot."""
import datetime
import math
import os
import shutil
import sys
import unicodedata
from zoneinfo import ZoneInfo


def duration(value):
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return '?'
    sign = '-' if value < 0 else ''
    minutes = abs(int(value)) // 60
    return f'{sign}{minutes // 60}h{minutes % 60:02d}m'


def cell_width(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in text)


def wrap(text, width):
    line, length = '', 0
    for char in str(text):
        size = cell_width(char)
        if length + size > width and line:
            yield line
            line, length = '', 0
        line += char
        length += size
    if line:
        yield line


def render_unified(data, color='auto', overlay=None, width=None):
    if not sys.stdout.isatty():
        overlay = None
    width = max(12, width or shutil.get_terminal_size((110, 24)).columns)
    enabled = color == 'always' or color == 'auto' and sys.stdout.isatty() and 'NO_COLOR' not in os.environ

    def emit(text, style=None):
        for line in wrap(text, width):
            print(f'\033[{style}m{line}\033[0m' if style and enabled else line)

    summary = data.get('summary', {})
    scheduler = summary.get('scheduler', {})
    try:
        local = datetime.datetime.fromisoformat(data['collected_at']).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M:%S')
    except (KeyError, ValueError):
        local = data.get('collected_at', '?')
    emit(f"AutoResearch  {local}  每 {data.get('interval_seconds', 60):g}s  采集 {summary.get('collection_seconds', '?')}s  只读", '1;36')
    emit(f"严重 {summary.get('critical_alerts', 0)} / 警告 {summary.get('warning_alerts', 0)}  运行流 {summary.get('running_streams', 0)}  GPU {summary.get('gpu_occupied', 0)}/{summary.get('gpu_total', '?')}  scheduler 运行 {scheduler.get('running', 0)} / 排队 {scheduler.get('queued', 0)} / 最久 {duration(scheduler.get('oldest_wait_seconds'))}")
    emit('GPU / scheduler', '1;36')
    for name, host in data.get('hosts', {}).items():
        emit(f"{host.get('endpoint_id', name)}  别名 {', '.join(host.get('aliases', [name]))}  {host.get('state', 'UNKNOWN')}")
        if host.get('error'):
            emit('  数据空洞: ' + host['error'])
        resources = host.get('host_resources', {})
        available = resources.get('ram_available_mib')
        total = resources.get('ram_total_mib')
        if available is None and isinstance(resources.get('ram_available_bytes'), (int, float)):
            available = round(resources['ram_available_bytes'] / 1024**2)
        if total is None and isinstance(resources.get('ram_total_bytes'), (int, float)):
            total = round(resources['ram_total_bytes'] / 1024**2)
        loads = resources.get('load_average') or []
        emit(f"  RAM 可用 {available if available is not None else '?'} MiB / 总 {total if total is not None else '?'} MiB  CPU {resources.get('cpu_count', '?')} / load {loads[0] if loads else resources.get('load_1m', '?')}")
        if not host.get('gpus'):
            emit('  GPU unavailable / unknown')
        for gpu in host.get('gpus', []):
            measured = gpu.get('measured', gpu)
            reserved = gpu.get('reserved', {})
            emit(f"  GPU{gpu.get('index', '?')} {gpu.get('name', '?')} {gpu.get('uuid', '?')}")
            emit(f"    实测 {measured.get('memory_used_mib', '?')}/{measured.get('memory_total_mib', '?')} MiB  算力 {measured.get('utilization_pct', '?')}%  温度 {measured.get('temperature_c', '?')}C")
            emit(f"    预约 {reserved.get('memory_mib', '?')} MiB / {reserved.get('compute_units', '?')} CU  RAM {reserved.get('ram_mib', '?')} MiB / CPU {reserved.get('cpu_cores', '?')}")
            for item in gpu.get('running_tasks', []):
                emit(f"    {item.get('task_id')} {item.get('state')} 显存 {item.get('memory_used_mib', '?')} MiB  {item.get('source', '?')} / {item.get('confidence', '?')}")
            unknown = gpu.get('unknown_processes', [])
            emit(f"    未归属 {gpu.get('unknown_memory_mib', '?')} MiB / {len(unknown)} 进程")
            for reason in sorted({item.get('reason', 'insufficient evidence') for item in unknown}):
                emit('      ' + reason)
            queue = gpu.get('external_queue_summary', host.get('external_queue_summary', {}))
            emit(f"    队列 {gpu.get('queued', host.get('scheduler_summary', {}).get('queued', 0))}  阻塞 {gpu.get('blocking_reason') or queue.get('blocking_reason') or ','.join(queue.get('blocking_reasons', [])) or 'unknown'}")
            if queue.get('count'):
                emit(f"    外部 {queue['count']}：资源 {queue.get('resources', '?')} / 等待 {duration(queue.get('oldest_wait_seconds'))} / {queue.get('blocking_reason') or ','.join(queue.get('blocking_reasons', [])) or 'unknown'}")
    emit('Agent / 12h (48 x 15m) -> now', '1;36')
    symbols = {'RUNNING': '#', 'CREDITED': '#', 'WAITING_GPU': 'W', 'QUEUED': 'W',
               'PENDING': '~', 'FAILED': '!', 'RETRY': '!', 'STOPPED': '|',
               'NOT_STARTED': '.', 'UNKNOWN': '?', 'UNREACHABLE': '?', 'COMPLETED': '='}
    symbols.update(credited='#', pending='~', waiting='W', failed_retry='!', manual_stop='|',
                   not_started='.', unknown='?', completed='=')
    tasks = data.get('tasks', [])
    if not tasks:
        emit('  无登记流')
    for task in tasks:
        streams = task.get('streams') or [{'id': task.get('id', '?')}]
        view = (overlay or {}).get(task.get('id'), {})
        if view.get('state') == 'VERIFIED':
            arrow = {'+': '\u2191', '-': '\u2193', '=': '\u2500'}[view.get('trend', '=')]
            anchors = f" B={view['B']:g} R={view['R']:g}" if 'B' in view and 'R' in view else ' B/R unavailable'
            emit(f"  {task.get('id')} 最佳 / 最新 {view['best']:g} / {view['latest']:g} {arrow}{anchors}" + (' \u2193优' if view.get('direction') == 'min' else ''))
        elif sys.stdout.isatty():
            emit(f"  {task.get('id')} 最佳 / 最新 unavailable / unavailable  B/R unavailable")
        for stream in streams:
            emit(f"  {task.get('id', '?')}:{stream.get('id', '?')} {task.get('state', 'UNKNOWN')}  告警 {len(task.get('alerts', []))}")
            buckets = stream.get('timeline_12h', task.get('timeline_12h', []))
            if isinstance(buckets, dict):
                buckets = buckets.get('buckets', [])
            band = ''.join(symbols.get(b.get('state', 'UNKNOWN'), '?') for b in buckets[-48:]).rjust(48, '?')
            emit('    [' + band + ']')
            timing = task.get('timing', {})
            diagnostic = task.get('diagnostic_summary', {})
            latest_event = diagnostic.get('latest_event', '?')
            if isinstance(latest_event, dict):
                latest_event = latest_event.get('event', '?')
            emit(f"    turn {task.get('current_turn', '?')}  心跳 {duration(task.get('heartbeat_age_seconds'))}  事件 {latest_event}  GPU {duration(timing.get('gpu_running_seconds'))}")
            emit(f"    live {duration(timing.get('live_elapsed_seconds'))} / pending {duration(timing.get('pending_seconds'))} / credited {duration(timing.get('credited_effective_seconds', task.get('effective_seconds')))}  截止剩余 {duration(task.get('budget_remaining_seconds'))}")
            emit(f"    需 {duration(timing.get('effective_remaining_seconds', task.get('effective_target_remaining_seconds')))} / 剩余 {duration(task.get('budget_remaining_seconds'))}  {timing.get('calculation_state', timing.get('state', 'unknown'))}  retry {diagnostic.get('retry_consecutive', '?')} / summary {diagnostic.get('summary_unchanged_generations', '?')}")
            active = task.get('active_job') or {}
            emit(f"    job {active.get('job_id', 'unknown')} {active.get('state', 'unknown')}  原因 {active.get('reason') or 'unknown'}")
            if task.get('configured_job_ids'):
                emit('    configured jobs: historical / stale')
    emit('告警', '1;33')
    for alert in data.get('alerts', []):
        emit(f"  {alert.get('task_id', alert.get('task', '?'))} {alert.get('severity', '?')} {alert.get('state', 'open')} {alert.get('id', '?')}  首次 {alert.get('first_seen', '?')} / 次数 {alert.get('observed_count', '?')}")
        emit(f"    来源 {alert.get('source', '?')}  证据 {alert.get('evidence', {})}  空洞 {alert.get('data_gap', False)}")
    if not data.get('alerts'):
        emit('  无已确认告警')
    emit('图例 # 计时中  W 等 GPU/对账  ~ 运行待结算  ! 失败重试  | 人工停止/修订  . 未启动  ? 未知/权限/数据空洞  = 完成')
    config = data.get('config', {})
    if config.get('reload_state') not in (None, 'ACTIVE'):
        emit(f"配置 {config['reload_state']}：{config.get('last_reload_error', 'unknown')}")
