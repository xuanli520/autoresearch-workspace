"""Dependency-free display projections; never infer scientific credit from pixels."""
import datetime as dt
import math
import re
import shutil
import sys
import unicodedata
from zoneinfo import ZoneInfo


STATUS = {
    'RUNNING': ('■', '运行', 'green'), 'CREDITED': ('■', '运行', 'green'),
    'PENDING': ('■', '运行', 'green'), 'PENDING_SETTLEMENT': ('■', '运行待确认', 'green'),
    'WAITING': ('W', '等待 GPU/对账', 'yellow'), 'WAITING_GPU': ('W', '等待 GPU', 'yellow'),
    'QUEUED': ('W', '排队', 'yellow'), 'STARTING': ('W', '启动中', 'yellow'),
    'FAILED': ('!', '失败', 'red'), 'FAILED_RETRY': ('!', '失败/重试', 'red'),
    'RETRY': ('!', '重试', 'red'), 'RETRYING': ('!', '重试', 'red'),
    'EXPIRED': ('!', '请求过期', 'red'), 'TIMED_OUT': ('!', '超时', 'red'),
    'INFEASIBLE': ('!', '请求不可行', 'red'),
    'STOPPED': ('S', '已停止', 'dim'), 'MANUAL_STOP': ('S', '已停止', 'dim'),
    'CANCELLED': ('S', '已取消', 'dim'), 'PAUSED': ('S', '暂停', 'yellow'),
    'NOT_STARTED': ('·', '未启动', 'dim'),
    'COMPLETED': ('=', '完成', 'cyan'), 'SUCCEEDED': ('=', '完成', 'cyan'),
    'UNKNOWN': ('?', '未知', 'dim'), 'UNREACHABLE': ('?', '不可达', 'yellow'),
    'EXITED_WITHOUT_RESULT': ('?', '退出未确认', 'yellow'),
}
LEGEND = '■ 运行（含待结算）   W 等待/排队/对账   ! 失败/重试   S 已停止   · 未启动   ? 未知/数据缺失   = 完成'
_ESC = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def text(value):
    value = '?' if value is None else str(value)
    return ''.join(c for c in _ESC.sub('', value) if c in '\n\t' or not unicodedata.category(c).startswith('C'))


def finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def number(value, digits=1):
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return text(value)
    return f'{value:.{digits}f}'.rstrip('0').rstrip('.') if finite(value) else '?'


def duration(value, *, seconds=False):
    if not finite(value):
        return '?'
    sign = '-' if value < 0 else ''
    value = abs(int(value))
    if seconds and value < 60:
        return f'{sign}{value}s'
    return f'{sign}{value // 3600}h{value % 3600 // 60:02d}m'


def timestamp(value):
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def local_time(value):
    epoch = timestamp(value)
    return dt.datetime.fromtimestamp(epoch, ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M:%S') if epoch is not None else '?'


def freshness(data, now):
    epoch = timestamp(data.get('collected_at'))
    age = max(0, now - epoch) if epoch is not None else None
    interval = data.get('interval_seconds', 60)
    interval = interval if finite(interval) and interval >= 1 else 60
    collected = data.get('summary', {}).get('collection_seconds', 0)
    collected = collected if finite(collected) else 0
    return age, age is None or age > max(2 * interval, interval + collected)


def status(value):
    return STATUS.get(str(value).upper(), ('?', text(value), 'dim'))


def activity(task):
    # Lifecycle UNKNOWN/UNREACHABLE must not be hidden by old timing data.
    state = task.get('state', 'UNKNOWN')
    if state == 'RUNNING' and task.get('timing', {}).get('waiting_gpu'):
        return 'WAITING_GPU'
    return state


def is_agent(task):
    """Classify by the official longrun registration, never by labels or GPU use."""
    controller = task.get('controller') or {}
    return controller.get('type') == 'research_handoff' and bool(controller.get('run_id'))


def timeline(task):
    buckets = task.get('timeline_12h', [])
    if isinstance(buckets, dict):
        buckets = buckets.get('buckets', [])
    buckets = buckets[-48:]
    result = [('?', 'dim')] * (48 - len(buckets))
    for bucket in buckets:
        value = 'UNKNOWN' if bucket.get('data_gap') else bucket.get('category', bucket.get('state', 'unknown'))
        symbol, _, style = status(value)
        result.append((symbol, style))
    return result


def task_cells(task):
    symbol, label, _ = status(activity(task))
    timing = task.get('timing', {})
    return [text(task.get('label', task.get('id'))), f'{symbol} {label}',
            text(task.get('current_turn')), duration(timing.get('credited_effective_seconds', task.get('effective_seconds'))),
            duration(timing.get('remaining_wall_seconds', task.get('budget_remaining_seconds'))),
            str(len(task.get('alerts', [])))]


def resource_lines(data, *, detailed=False):
    lines = []
    for alias, host in data.get('hosts', {}).items():
        endpoint = host.get('endpoint_id', alias)
        if host.get('error') or host.get('state') == 'UNREACHABLE':
            lines.append(f'{alias}  不可达 / GPU 未知  {text(host.get("error"))}')
            continue
        resources = host.get('host_resources', {})
        avail, total = resources.get('ram_available_mib'), resources.get('ram_total_mib')
        if avail is None and finite(resources.get('ram_available_bytes')):
            avail = resources['ram_available_bytes'] / 1024**2
        if total is None and finite(resources.get('ram_total_bytes')):
            total = resources['ram_total_bytes'] / 1024**2
        loads = resources.get('load_average') or []
        load = loads[0] if loads else resources.get('load_1m')
        lines.append(f'{alias}  RAM 可用 {number(avail)}/{number(total)} MiB  CPU {text(resources.get("cpu_count"))} 核  负载 {number(load, 2)}')
        if detailed:
            lines.append(f'endpoint: {text(endpoint)}  别名: {text(host.get("aliases", [alias]))}')
        for gpu in host.get('gpus', []):
            m, r = gpu.get('measured', gpu), gpu.get('reserved', {})
            lines.append(f'GPU{text(gpu.get("index"))} {text(gpu.get("name"))}  '
                         f'显存 {number(m.get("memory_used_mib"))}/{number(m.get("memory_total_mib"))} MiB  '
                         f'利用率 {number(m.get("utilization_pct"))}%  温度 {number(m.get("temperature_c"))}°C')
            lines.append(f'预约: 显存 {number(r.get("memory_mib"))} MiB / {number(r.get("compute_units"))} CU  '
                         f'RAM {number(r.get("ram_mib"))} MiB / CPU {number(r.get("cpu_cores"))} 核  排队 {text(gpu.get("queued", 0))}')
            if detailed:
                lines.append(f'GPU UUID: {text(gpu.get("uuid"))}')
                for owner in gpu.get('running_tasks', []):
                    lines.append(f'  {text(owner.get("task_id"))}  显存 {number(owner.get("memory_used_mib"))} MiB  '
                                 f'归属 {text(owner.get("source"))}/{text(owner.get("confidence"))}')
                unknown = gpu.get('unknown_processes', [])
                lines.append(f'未归属: {len(unknown)} 进程 / {number(gpu.get("unknown_memory_mib", 0))} MiB')
                for reason in sorted({text(row.get('reason')) for row in unknown}):
                    lines.append(f'  {reason}')
                lines.append(f'阻塞原因: {text(gpu.get("blocking_reason") or "无已确认原因")}')
        if not host.get('gpus'):
            lines.append('GPU 不可用 / 未知')
        queue = host.get('external_queue_summary') or next((g.get('external_queue_summary')
                for g in host.get('gpus', []) if g.get('external_queue_summary')), {})
        if queue.get('count'):
            lines.append(f'外部队列 {text(queue.get("count"))} 项  最久等待 {duration(queue.get("oldest_wait_seconds"))}')
            if detailed:
                lines.append(f'外部队列资源: {text(queue.get("resources"))}  原因: {text(queue.get("blocking_reasons"))}')
    return lines or ['无已登记资源']


def score_line(view):
    if view.get('state') != 'VERIFIED' or not all(finite(view.get(k)) for k in ('best', 'latest')):
        return '评分: 不可用（需要前台直接采集及验证记录）'
    trend = {'+': '改善', '-': '退步', '=': '持平'}.get(view.get('trend'), '未知')
    direction = '最小化' if view.get('direction') == 'min' else '最大化'
    line = f'评分: 最佳 {view["best"]:g} / 最新 {view["latest"]:g}  {trend}（{direction}）'
    if all(finite(view.get(k)) for k in ('B', 'R')):
        line += f'  B={view["B"]:g} R={view["R"]:g}'
    return line


def task_lines(task, data, overlay=None):
    timing, diag = task.get('timing', {}), task.get('diagnostic_summary', {})
    state = task.get('state', 'UNKNOWN')
    controller = task.get('controller', {})
    lines = [f'任务: {text(task.get("id"))}  主机: {text(task.get("host"))}',
             f'生命周期: {text(state)}  本轮活动: {status(activity(task))[1]}  轮次: {text(task.get("current_turn"))}',
             f'run_id: {text(controller.get("run_id"))}',
             f'心跳距观察: {duration(task.get("heartbeat_age_seconds"), seconds=True)}  最近事件: {text(diag.get("latest_event"))}',
             f'本轮经过: {duration(timing.get("live_elapsed_seconds"))}  本轮待确认估计: {duration(timing.get("pending_seconds"))}',
             f'累计已确认: {duration(timing.get("credited_effective_seconds", task.get("effective_seconds")))}  '
             f'距有效目标: {duration(timing.get("effective_remaining_seconds", task.get("effective_target_remaining_seconds")))}',
             f'距硬截止: {duration(timing.get("remaining_wall_seconds", task.get("budget_remaining_seconds")))}  '
             f'截止: {local_time(timing.get("deadline_at", task.get("deadline_at")))}',
             f'本轮非等待时长估计: {duration(timing.get("gpu_running_seconds"))}  排除 GPU 等待: {duration(timing.get("excluded_gpu_wait_seconds"))}',
             f'结算口径: {text(timing.get("credit_policy"))}  结算状态: {text(timing.get("calculation_state"))}  '
             f'来源: {text(timing.get("source"))}',
             f'最近确认: {local_time(timing.get("last_credit_at"))}  重试: {text(diag.get("retry_consecutive"))}  '
             f'摘要未变: {text(diag.get("summary_unchanged_generations"))}',
             score_line((overlay or {}).get(task.get('id'), {}))]
    if state in ('UNREACHABLE', 'UNKNOWN'):
        lines.append(f'当前观察未知；最后成功: {local_time(task.get("last_success_at"))}；保留计时为历史值')
    job = task.get('active_job') or {}
    lines.extend([f'活动作业: {text(job.get("job_id", job.get("id")))}  {text(job.get("state"))}',
                  f'request_id: {text(job.get("request_id"))}  原因: {text(job.get("reason") or "无已确认原因")}',
                  f'历史登记作业: {text(task.get("configured_job_ids", []))}'])
    for job in task.get('scheduler_history', []):
        lines.append(f'历史作业: {text(job.get("job_id", job.get("id")))} {text(job.get("state"))} {text(job.get("reason"))}')
    for stream in task.get('streams', []):
        latest = stream.get('latest') or {}
        lines.append(f'日志流 {text(stream.get("id"))}: 步数 {text(latest.get("step"))}/{text(stream.get("total_steps"))}  '
                     f'预计剩余 {duration(stream.get("eta_seconds"))}  状态 {text(stream.get("state"))}')
    for code in task.get('alerts', []):
        lines.append(f'当前告警: {text(code)}')
    buckets = task.get('timeline_12h', [])
    if isinstance(buckets, dict):
        buckets = buckets.get('buckets', [])
    for bucket in buckets:
        flags = [key for key in ('waiting', 'running', 'failed_retry', 'manual_stop') if bucket.get(key)]
        if bucket.get('event_categories') or bucket.get('data_gap') or len(flags) > 1:
            lines.append(f'时间格 {local_time(bucket.get("start_at"))} → {local_time(bucket.get("end_at"))}: '
                         f'事件 {text(bucket.get("event_categories", []))} / 活动 {text(flags)} / 缺失 {text(bucket.get("data_gap", []))}')
    for alert in data.get('alerts', []):
        if alert.get('task_id', alert.get('task')) != task.get('id'):
            continue
        lines.extend([f'{"已恢复" if alert.get("state") == "resolved" else "当前"}告警 [{text(alert.get("severity"))}]: {text(alert.get("id"))}',
                      f'首次 {local_time(alert.get("first_seen"))}  次数 {text(alert.get("observed_count"))}  来源 {text(alert.get("source"))}',
                      f'证据: {text(alert.get("evidence", {}))}'])
    if timing.get('data_gap'):
        lines.append(f'计时数据缺失: {text(timing["data_gap"])}')
    return lines


def cell_width(value):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1 for c in value)


def wrap(value, width):
    line = ''
    for char in text(value):
        if char == '\n' or cell_width(line + char) > width:
            yield line
            line = ''
        if char != '\n':
            line += char
    if line:
        yield line


def render_unified(data, color='auto', overlay=None, width=None):
    """Small diagnostic text projection. Interactive watch uses Textual exclusively."""
    width = max(2, width or shutil.get_terminal_size((110, 24)).columns)
    if not sys.stdout.isatty():
        overlay = None
    lines = [f'AutoResearch  {local_time(data.get("collected_at"))}  只读', 'GPU / scheduler']
    lines.extend(resource_lines(data, detailed=True))
    lines.append('Agent / 12h（48 × 15m）')
    for task in data.get('tasks', []):
        lines.extend(task_lines(task, data, overlay))
        lines.append(''.join(symbol for symbol, _ in timeline(task)))
    lines.append(LEGEND)
    for line in lines:
        for part in wrap(line, width):
            print(f'\033[36m{part}\033[0m' if color == 'always' else part)
