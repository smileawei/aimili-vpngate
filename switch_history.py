"""Durable switch history, independent of the short-lived diagnostic logs."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib
import json
import re
import threading
import time
import uuid


class SwitchHistory:
    retention_days = 90

    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()

    def append(self, from_node_id, to_node_id, trigger, result, reason='', duration_ms=None,
               *, timestamp=None, event_id=None, source='structured'):
        now = time.time() if timestamp is None else timestamp
        stamp = datetime.fromtimestamp(now, timezone.utc)
        event = dict(id=event_id or uuid.uuid4().hex, timestamp=now,
                     timestamp_iso=stamp.isoformat(), from_node_id=str(from_node_id or ''),
                     to_node_id=str(to_node_id or ''), trigger=trigger, result=result,
                     reason=str(reason or '')[:4000], duration_ms=duration_ms, source=source)
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / f'{stamp:%Y-%m-%d}.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps(event, ensure_ascii=False)+'\n')
            self._prune()
        return event

    def _prune(self):
        cutoff = (datetime.now(timezone.utc)-timedelta(days=self.retention_days)).date()
        for p in self.root.glob('????-??-??.jsonl'):
            try:
                if datetime.strptime(p.stem, '%Y-%m-%d').date() < cutoff:
                    p.unlink(missing_ok=True)
            except ValueError:
                continue

    def read(self, start, end, offset=0, limit=50, result=''):
        if not (0 <= start < end <= time.time()+86400*2):
            raise ValueError('日期范围无效')
        if end-start > 90*86400+7200:
            raise ValueError('单次最多查询 90 天，请缩小日期范围')
        if offset < 0 or offset > 100000 or not 1 <= limit <= 100:
            raise ValueError('分页参数无效')
        if result not in ('', 'success', 'reconnected', 'failed', 'cancelled', 'disconnected'):
            raise ValueError('记录结果筛选无效')
        entries = []
        start_day = datetime.fromtimestamp(start, timezone.utc).date()
        end_day = datetime.fromtimestamp(end, timezone.utc).date()
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            self._prune()
            for p in self.root.glob('????-??-??.jsonl'):
                try:
                    day = datetime.strptime(p.stem, '%Y-%m-%d').date()
                except ValueError:
                    continue
                if not start_day <= day <= end_day:
                    continue
                for line in p.read_text(encoding='utf-8').splitlines():
                    try:
                        e = json.loads(line)
                        if isinstance(e.get('id'), str) and start <= float(e['timestamp']) < end and (not result or e['result'] == result):
                            e['timestamp'] = float(e['timestamp'])
                            entries.append(e)
                    except (ValueError, TypeError, KeyError):
                        continue
        entries.sort(key=lambda e: (e['timestamp'], e['id']), reverse=True)
        return dict(entries=entries[offset:offset+limit], total=len(entries), offset=offset,
                    limit=limit, has_more=offset+limit < len(entries), retention_days=self.retention_days)

    def import_legacy(self, logs):
        """Import once; old timestamps use the application's local timezone."""
        with self.lock:
            marker = self.root / '.legacy-imported'
            if marker.exists():
                return
            existing = set()
            self.root.mkdir(parents=True, exist_ok=True)
            for p in self.root.glob('*.jsonl'):
                for line in p.read_text(encoding='utf-8').splitlines():
                    try:
                        existing.add(json.loads(line)['id'])
                    except (ValueError, KeyError, TypeError):
                        pass
            previous = ''
            for p in sorted(Path(logs).glob('????-??-??.json')):
                for line in p.read_text(encoding='utf-8').splitlines():
                    try:
                        e = json.loads(line)
                        message = str(e.get('message', ''))
                        match = re.fullmatch(r'热备提升成功: (\S+)', message) or re.match(r'节点 (\S+) 连接成功，', message)
                        failure = re.match(r'连接节点 (\S+) 失败: (.*)', message)
                        if not match and not failure:
                            continue
                        target = (match or failure).group(1)
                        stamp = datetime.strptime(e['timestamp'], '%Y-%m-%d %H:%M:%S').astimezone().timestamp()
                        event_id = hashlib.sha256((e['timestamp']+message).encode()).hexdigest()
                        if event_id not in existing:
                            self.append(previous, target, 'legacy', 'failed' if failure else ('reconnected' if previous == target else 'success'),
                                        ('历史连接失败: '+failure.group(2)) if failure else '从历史运行日志恢复；原日志未完整记录触发原因及耗时',
                                        timestamp=stamp, event_id=event_id, source='legacy')
                            existing.add(event_id)
                        if not failure:
                            previous = target
                    except (ValueError, KeyError, TypeError):
                        continue
            marker.write_text(datetime.now(timezone.utc).isoformat(), encoding='utf-8')
