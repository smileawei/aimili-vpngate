"""Durable switch history, independent of the short-lived diagnostic logs."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib
import ipaddress
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
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
               *, timestamp=None, event_id=None, source='structured', from_exit_ip=None, to_exit_ip=None):
        now = time.time() if timestamp is None else timestamp
        stamp = datetime.fromtimestamp(now, timezone.utc)
        event = dict(id=event_id or uuid.uuid4().hex, timestamp=now,
                     timestamp_iso=stamp.isoformat(), from_node_id=str(from_node_id or ''),
                     to_node_id=str(to_node_id or ''), trigger=trigger, result=result,
                     reason=str(reason or '')[:4000], duration_ms=duration_ms, source=source)
        if source != 'legacy' or from_exit_ip is not None or to_exit_ip is not None:
            event.update(from_exit_ip=from_exit_ip, to_exit_ip=to_exit_ip)
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

    def read(self, start, end, offset=0, limit=50, result='', *, _all=False):
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
        return dict(entries=entries if _all else entries[offset:offset+limit], total=len(entries), offset=offset,
                    limit=limit, has_more=offset+limit < len(entries), retention_days=self.retention_days)

    @staticmethod
    def ip_change_basis(event):
        if event.get('result') not in ('success', 'reconnected') or event.get('trigger') == 'startup':
            return 'excluded'
        if not event.get('from_node_id') or not event.get('to_node_id'):
            return 'excluded'
        measured = 'from_exit_ip' in event or 'to_exit_ip' in event
        def parse(value):
            try:
                return ipaddress.ip_address(str(value))
            except ValueError:
                return None
        if measured:
            before, after = parse(event.get('from_exit_ip')), parse(event.get('to_exit_ip'))
        else:
            def node_ip(value):
                match = re.fullmatch(r'[A-Za-z]{2}_([^_]+)_\d+_(?:tcp|udp)', str(value))
                return parse(match.group(1)) if match else None
            before, after = node_ip(event.get('from_node_id')), node_ip(event.get('to_node_id'))
        if before is None or after is None:
            return 'unknown'
        return ('confirmed' if measured else 'inferred') if before != after else 'same_ip'

    def statistics(self, start, end, granularity='day', timezone_name='UTC', include_legacy=False):
        if granularity not in ('hour', 'day', 'week'):
            raise ValueError('统计维度无效')
        try:
            tz = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise ValueError('时区无效') from exc
        entries = self.read(start, end, _all=True)['entries']
        def floor(stamp):
            local = datetime.fromtimestamp(stamp, tz)
            if granularity == 'hour':
                return local.replace(minute=0, second=0, microsecond=0)
            local = local.replace(hour=0, minute=0, second=0, microsecond=0)
            return local-timedelta(days=local.weekday()) if granularity == 'week' else local
        buckets = {}
        current = floor(start)
        while current.timestamp() < end:
            stamp = current.timestamp()
            buckets[stamp] = dict(timestamp=stamp, label=current.isoformat(), count=0, confirmed=0, inferred=0)
            if granularity == 'hour':
                next_stamp = stamp+3600
                current = floor(next_stamp)
                while current.timestamp() <= stamp:
                    next_stamp += 1800
                    current = floor(next_stamp)
            else:
                current += timedelta(days=7 if granularity == 'week' else 1)
        counts = dict(confirmed=0, inferred=0, unknown=0, same_ip=0, excluded=0)
        for event in entries:
            basis = self.ip_change_basis(event)
            counts[basis] += 1
            if basis not in ('confirmed', 'inferred'):
                continue
            bucket = buckets.get(floor(event['timestamp']).timestamp())
            if bucket is not None:
                bucket[basis] += 1
                if basis == 'confirmed' or include_legacy:
                    bucket['count'] += 1
        return dict(buckets=list(buckets.values()), total=counts['confirmed']+(counts['inferred'] if include_legacy else 0),
                    counts=counts, granularity=granularity, timezone=timezone_name,
                    include_legacy=include_legacy, week_starts_on='Monday')

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
