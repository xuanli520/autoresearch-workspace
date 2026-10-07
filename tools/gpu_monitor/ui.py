"""Textual's fixed-screen operator frontend. Imported only for interactive watch."""
import copy
import os
import time

from rich.text import Text
from textual import work
from textual.app import App
from textual.binding import Binding
from textual.containers import HorizontalScroll, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Collapsible, DataTable, Footer, Input, Static

try:
    from .presentation import (LEGEND, activity, duration, freshness, is_agent, local_time,
                               number, resource_lines, status, task_cells, task_lines, text, timeline)
    from .privacy import public_record
except ImportError:
    from presentation import (LEGEND, activity, duration, freshness, is_agent, local_time,
                              number, resource_lines, status, task_cells, task_lines, text, timeline)
    from privacy import public_record


class DetailView(VerticalScroll):
    def compose(self):
        yield Static('选择任务查看详情', classes='detail-caption', markup=False)
        with HorizontalScroll(classes='timeline-scroll'):
            yield Static('', classes='timeline', markup=False)
        yield Static('', classes='detail-text', markup=False)

    def show_task(self, task, data, overlay, *, colored=True):
        self.query_one('.detail-caption', Static).update('12 小时时间线 · 每格 15 分钟 · → 最近观察')
        band = Text(no_wrap=True, overflow='ignore')
        for symbol, style in timeline(task):
            band.append(symbol, style=style if colored else None)
        buckets = task.get('timeline_12h', [])
        if isinstance(buckets, dict):
            buckets = buckets.get('buckets', [])
        if buckets:
            band.append(f'  {local_time(buckets[0].get("start_at"))} → {local_time(buckets[-1].get("end_at"))}')
        self.query_one('.timeline', Static).update(band)
        self.query_one('.detail-text', Static).update(Text('\n'.join(task_lines(task, data, overlay)), overflow='fold'))

    def clear_task(self):
        self.query_one('.detail-caption', Static).update('无匹配任务')
        self.query_one('.timeline', Static).update('')
        self.query_one('.detail-text', Static).update('')


class InfoScreen(ModalScreen):
    BINDINGS = [Binding('escape', 'dismiss', '返回'), Binding('enter', 'dismiss', '返回')]

    def __init__(self, title, content):
        super().__init__()
        self.title_text, self.content_text = title, content

    def compose(self):
        with Vertical(classes='modal-box'):
            yield Static(self.title_text + ' · Esc 返回', classes='modal-title', markup=False)
            with VerticalScroll():
                yield Static(Text(self.content_text, overflow='fold'), markup=False)


class TaskScreen(ModalScreen):
    BINDINGS = [Binding('escape', 'dismiss', '返回')]

    def __init__(self, task_id):
        super().__init__()
        self.task_id = task_id

    def compose(self):
        with Vertical(classes='modal-box'):
            yield Static('任务详情 · Esc 返回', classes='modal-title')
            yield DetailView(id='full-detail')

    def on_mount(self):
        self.update_task()

    def update_task(self):
        task = next((t for t in self.app.data.get('tasks', []) if t['id'] == self.task_id), None)
        view = self.query_one(DetailView)
        if task:
            view.show_task(task, self.app.data, self.app.overlay, colored=self.app.colored)
        else:
            view.clear_task()


class SnapshotReady(Message):
    def __init__(self, data, overlay, info):
        super().__init__()
        self.data, self.overlay, self.info = public_record(data), copy.deepcopy(overlay), public_record(info)


class SourceState(Message):
    def __init__(self, info):
        super().__init__()
        self.info = public_record(info)


class SourceFinished(Message):
    def __init__(self, info, error=False):
        super().__init__()
        self.info, self.error = public_record(info), error


class MonitorApp(App):
    TITLE = 'AutoResearch 监控器'
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding('/', 'filter', '筛选'), Binding('r', 'refresh_data', '刷新'),
        Binding('enter', 'details', '详情'), Binding('g', 'resources', '资源'),
        Binding('?', 'help', '帮助'), Binding('q', 'quit', '退出'),
        Binding('ctrl+c', 'quit', '退出', show=False, priority=True),
        Binding('ctrl+q', 'quit', '退出', show=False, priority=True),
        Binding('escape', 'clear_filter', '清除筛选', show=False),
    ]
    CSS = '''
    Screen { layout: vertical; }
    #status { height: auto; max-height: 5; padding: 0 1; background: $panel; }
    #resources { height: 6; border: round $primary; padding: 0 1; }
    #resource-text { height: auto; }
    #filter { display: none; height: 3; }
    #filter.visible { display: block; }
    #body { height: 1fr; min-height: 3; layout: horizontal; }
    #task-pane { width: 1fr; height: 1fr; border: round $primary; }
    #tasks { height: 1fr; }
    #general { height: auto; padding: 0; }
    #general-tasks { height: 6; }
    #detail { width: 1fr; height: 1fr; border: round $primary; padding: 0 1; }
    #body.stacked { layout: vertical; }
    #body.stacked #task-pane { width: 100%; height: 1fr; }
    #body.stacked #detail { width: 100%; height: 1fr; }
    #body.narrow #detail { display: none; }
    #alerts { height: auto; max-height: 3; padding: 0 1; background: $panel; }
    .compact #resources { height: 3; }
    .compact #status { max-height: 3; }
    .compact #alerts { max-height: 2; }
    .detail-caption { height: auto; }
    .timeline-scroll { height: 3; width: 100%; }
    .timeline { width: auto; height: 1; }
    .detail-text { height: auto; }
    ModalScreen { align: center middle; background: $background 80%; }
    .modal-box { width: 96%; height: 94%; border: round $primary; padding: 0 1; }
    .modal-title { height: auto; color: $accent; }
    .modal-box DetailView { height: 1fr; }
    .modal-box Static { height: auto; }
    '''

    def __init__(self, source, *, color='auto', task_ids=()):
        super().__init__()
        self.source, self.task_ids = source, set(task_ids)
        self.colored = color != 'never' and not (color == 'auto' and 'NO_COLOR' in os.environ)
        if not self.colored:
            self.console.no_color = True
        self.data, self.overlay = {}, {}
        self.info = {'mode': source.mode, 'state': 'STARTING'}
        self.selected_id, self.filter_text = None, ''
        self._narrow, self._row_ids = False, {'tasks': [], 'general-tasks': []}
        self._exit_requested = False
        self._table_width = None
        self.error = None

    def compose(self):
        yield Static('AutoResearch · 等待首个快照', id='status', markup=False)
        with VerticalScroll(id='resources'):
            yield Static('等待资源数据', id='resource-text', markup=False)
        yield Input(placeholder='筛选任务名称、ID 或主机；Esc 清除', id='filter')
        with Vertical(id='body'):
            with Vertical(id='task-pane'):
                yield DataTable(id='tasks', cursor_type='row', zebra_stripes=True)
                with Collapsible(title='通用任务（0）', collapsed=True, id='general'):
                    yield DataTable(id='general-tasks', cursor_type='row', zebra_stripes=True)
            yield DetailView(id='detail')
        yield Static('', id='alerts', markup=False)
        yield Footer()

    def base(self, selector, widget_type=None):
        # Timed updates continue while a modal screen is open.
        return self.screen_stack[0].query_one(selector, widget_type)

    def on_mount(self):
        self.base('#resources').border_title = 'GPU 资源 / 调度器 · g 查看完整资源'
        self.base('#detail').border_title = '选中详情'
        self._layout(self.size.width, self.size.height)
        self.base('#tasks', DataTable).focus()
        self.set_interval(1, self._update_header)
        self.collect()

    @work(thread=True, exclusive=True)
    def collect(self):
        try:
            info = self.source.run(lambda data, overlay, info: self.post_message(SnapshotReady(data, overlay, info)),
                                   lambda info: self.post_message(SourceState(info)))
            self.post_message(SourceFinished(info))
        except Exception as exc:
            # Arbitrary exception strings may include paths or credentials.
            self.post_message(SourceFinished({'state': 'ERROR', 'error': type(exc).__name__}, error=True))

    def on_snapshot_ready(self, event):
        self.data, self.info = event.data, event.info
        self.overlay = event.overlay if event.info.get('mode') == 'foreground' else {}
        self.base('#resource-text', Static).update(Text('\n'.join(resource_lines(self.data)), overflow='fold'))
        self._update_rows()
        self._update_header()
        if isinstance(self.screen, TaskScreen):
            self.screen.update_task()

    def on_source_state(self, event):
        self.info.pop('cache_error', None)
        self.info.update(event.info)
        if self.info.get('mode') != 'foreground':
            self.overlay.clear()
        self._update_header()

    def on_source_finished(self, event):
        self.info.update(event.info)
        if event.error:
            self.error = event.info.get('error', 'MonitorError')
        if self._exit_requested:
            self.exit(1 if event.error else 0)
        else:
            self._update_header()
            self.set_timer(0.15, lambda: self.exit(1 if event.error else 0))

    def _layout(self, width, height):
        body = self.base('#body')
        self.screen_stack[0].set_class(height < 24, 'compact')
        body.set_class(width < 120, 'stacked')
        body.set_class(width < 80, 'narrow')
        narrow = width < 80
        rebuild = width != self._table_width or narrow != self._narrow or not self.base('#tasks', DataTable).columns
        self._table_width = width
        self._narrow = narrow
        if rebuild:
            self._update_rows(rebuild=True)

    def on_resize(self, event):
        if self.is_mounted and self.screen_stack[0].query('#body'):
            self._layout(event.size.width, event.size.height)

    def _update_rows(self, *, rebuild=False):
        tasks = [t for t in self.data.get('tasks', []) if (not self.task_ids or t['id'] in self.task_ids)
                 and self.filter_text in ' '.join(text(t.get(k, '')) for k in ('id', 'label', 'host')).casefold()]
        agents, general = [t for t in tasks if is_agent(t)], [t for t in tasks if not is_agent(t)]
        ids = [t['id'] for t in tasks]
        if self.selected_id not in ids:
            self.selected_id = (agents or general or [{}])[0].get('id')
        for name, rows in (('tasks', agents), ('general-tasks', general)):
            table = self.base('#' + name, DataTable)
            row_ids = [t['id'] for t in rows]
            if rebuild or row_ids != self._row_ids[name]:
                x, y = table.scroll_x, table.scroll_y
                table.clear(columns=rebuild)
                if rebuild:
                    labels = ['任务', '当前活动', '告警'] if self._narrow else ['任务', '当前活动', '轮次', '已确认', '距截止', '告警']
                    available = self.size.width // 2 - 2 if self.size.width >= 120 else self.size.width - 2
                    widths = ([max(8, min(32, available - 22)), 12, 4] if self._narrow else
                              [max(8, min(28, available - 44)), 10, 4, 7, 7, 4])
                    for index, (label, width) in enumerate(zip(labels, widths)):
                        table.add_column(label, key=str(index), width=width)
                for task in rows:
                    table.add_row(*self._cells(task), key=task['id'])
                self._row_ids[name] = row_ids
                if self.selected_id in row_ids:
                    table.move_cursor(row=row_ids.index(self.selected_id), scroll=False)
                table.scroll_to(x=x, y=y, animate=False)
            else:
                for task in rows:
                    for index, value in enumerate(self._cells(task)):
                        table.update_cell(task['id'], str(index), value)
        self.base('#task-pane').border_title = f'正式长时间 Agent · {len(agents)}'
        self.base('#general', Collapsible).title = f'通用任务（{len(general)}）'
        if self.filter_text and general:
            self.base('#general', Collapsible).collapsed = False
        self._update_detail()

    def _cells(self, task):
        cells = task_cells(task)
        if self._narrow:
            cells = [cells[0], cells[1], cells[5]]
        result = [Text(value, no_wrap=True, overflow='ellipsis') for value in cells]
        if self.colored:
            result[1].stylize(status(activity(task))[2])
        return result

    def on_data_table_row_highlighted(self, event):
        table = event.data_table
        if (table.has_focus and event.cursor_row == table.cursor_row
                and event.row_key.value in self._row_ids[table.id]):
            self.selected_id = event.row_key.value
            self._update_detail()

    def on_data_table_row_selected(self, event):
        self.selected_id = event.row_key.value
        self.action_details()

    def _update_detail(self):
        task = next((t for t in self.data.get('tasks', []) if t['id'] == self.selected_id), None)
        view = self.base('#detail', DetailView)
        if task:
            view.show_task(task, self.data, self.overlay, colored=self.colored)
        else:
            view.clear_task()

    def _update_header(self):
        if not self.is_mounted:
            return
        age, stale = freshness(self.data, time.time())
        mode = '后台快照' if self.info.get('mode') == 'background' else '前台采集'
        state = '采集中' if self.info.get('collecting') else '等待采集'
        if self.info.get('mode') == 'background':
            state = '采集器运行' if self.info.get('collector_alive') else '采集器已停止 / 状态未知'
        if self.info.get('state') in ('ERROR', 'STOPPED') and self.info.get('mode') != 'background':
            state = text(self.info.get('reason', self.info['state']))
        interval = self.data.get('interval_seconds', self.info.get('interval_seconds', 60))
        summary = self.data.get('summary', {})
        critical, warnings = summary.get('critical_alerts', 0), summary.get('warning_alerts', 0)
        value = (f'AutoResearch · {mode} · 只读 · {state}\n'
                 f'更新 {local_time(self.data.get("collected_at"))} · 数据年龄 {duration(age, seconds=True)}'
                 f'{"（过期）" if stale else ""} · 间隔 {number(interval)}s · 采集 {number(summary.get("collection_seconds"), 3)}s · '
                 f'严重 {critical} / 警告 {warnings}')
        if self.info.get('mode') == 'background':
            scope = self.info.get('task_ids')
            scope_label = f'{len(scope)} 个任务（固定筛选）' if scope else f'快照 {len(self.data.get("tasks", []))} 个登记任务'
            value += '\n采集范围: ' + scope_label + ' · r 重读快照 · 私有评分不可用'
            missing = self.task_ids - {t['id'] for t in self.data.get('tasks', [])}
            if missing:
                value += '\n所选任务未包含在快照: ' + ', '.join(sorted(missing))
        if self.info.get('cache_error'):
            value += '\n' + text(self.info['cache_error'])
        self.base('#status', Static).update(Text(value, overflow='fold'))
        alerts = [a for a in self.data.get('alerts', []) if a.get('state') == 'open']
        order = {'critical': 0, 'error': 1, 'warning': 2, 'info': 3}
        alerts.sort(key=lambda a: order.get(a.get('severity'), 9))
        agents = [t for t in self.data.get('tasks', []) if is_agent(t)]
        running = sum(t.get('state') == 'RUNNING' for t in agents)
        general = sum(t.get('state') == 'RUNNING' for t in self.data.get('tasks', []) if not is_agent(t))
        sched = summary.get('scheduler', {})
        value = (f'Agent {running}/{len(agents)} 运行 · 通用任务 {general} 运行 · '
                 f'GPU {summary.get("gpu_occupied", 0)}/{text(summary.get("gpu_total"))} · '
                 f'调度器 {sched.get("running", 0)} 运行 / {sched.get("queued", 0)} 排队')
        value += '\n' + ('当前告警: ' + ' | '.join(text(a.get('id')) for a in alerts[:3]) if alerts else '无已确认当前告警')
        self.base('#alerts', Static).update(Text(value, overflow='fold'))

    def on_input_changed(self, event):
        if event.input.id == 'filter':
            self.filter_text = event.value.casefold().strip()
            self._update_rows()

    def on_input_submitted(self):
        self.base('#tasks', DataTable).focus()

    def action_filter(self):
        control = self.base('#filter', Input)
        control.add_class('visible')
        control.focus()

    def action_clear_filter(self):
        control = self.base('#filter', Input)
        control.value = ''
        control.remove_class('visible')
        self.base('#tasks', DataTable).focus()

    def action_refresh_data(self):
        self.source.refresh()

    def action_details(self):
        if self.selected_id:
            self.push_screen(TaskScreen(self.selected_id))

    def action_resources(self):
        self.push_screen(InfoScreen('完整资源 / 队列', '\n'.join(resource_lines(self.data, detailed=True))))

    def action_help(self):
        self.push_screen(InfoScreen('操作与图例',
            '↑↓ / 鼠标: 选择任务与滚动\nTab: 切换焦点\nEnter: 完整任务详情\n/: 筛选\n'
            'Esc: 返回或清除筛选\nr: 刷新\ng: 完整资源\nq / Ctrl-C: 退出本地界面\n\n' + LEGEND + '\n\n'
            '时间线每格 15 分钟，运行包含待结算区间；混合事件和数据缺失在详情保留。\n'
            '“=”也可能表示轮次成功结束，不表示整个任务终止。\n'
            '已确认时长仅取官方计时；本轮待确认估计不增加正式信用。\n'
            '前台刷新请求合并处理；后台模式只重读快照，不请求远端刷新。\n'
            '前台退出结束自己的本地采集；后台退出只关闭界面。\n'
            'Agent 仅含 research_handoff 官方长运行登记；其余任务位于折叠的通用任务。'))

    def action_quit(self):
        if self._exit_requested:
            return
        self._exit_requested = True
        self.source.stop()
        self.info.update(reason='退出中，等待当前探测结束', state='STOPPED')
        self._update_header()

    def on_unmount(self):
        self.source.stop()
        self.overlay.clear()


def run_tui(source, *, color='auto', task_ids=()):
    app = MonitorApp(source, color=color, task_ids=task_ids)
    result = app.run()
    if app.error:
        import sys
        print(f'gpu-monitor: 本地采集失败（{app.error}）；请用 status --json 检查配置与连接。', file=sys.stderr)
    return result or 0
