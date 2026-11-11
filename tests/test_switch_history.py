import json
import tempfile
import time
import unittest
from unittest import mock
from datetime import datetime, timezone, timedelta
from pathlib import Path
from switch_history import SwitchHistory


class SwitchHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SwitchHistory(Path(self.tmp.name)/'history')
        self.now = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip_result_filter_and_newest_first_pagination(self):
        for index in range(5):
            self.store.append('old','new','auto','failed' if index == 2 else 'success',
                              '<script>untrusted</script>',1200,timestamp=self.now+index/100)
        page=self.store.read(self.now-1,self.now+1,limit=2)
        self.assertEqual(5,page['total'])
        self.assertTrue(page['has_more'])
        self.assertGreater(page['entries'][0]['timestamp'],page['entries'][1]['timestamp'])
        self.assertEqual(2,len(self.store.read(self.now-1,self.now+1,offset=2,limit=2)['entries']))
        failed=self.store.read(self.now-1,self.now+1,result='failed')['entries']
        self.assertEqual(1,len(failed))
        self.assertEqual(1200,failed[0]['duration_ms'])
        self.assertTrue(failed[0]['timestamp_iso'].endswith('+00:00'))

    def test_time_range_is_inclusive_start_exclusive_end(self):
        for index in [-1,0,1]:
            self.store.append('a','b','manual','success',timestamp=self.now+index)
        entries=self.store.read(self.now,self.now+1)['entries']
        self.assertEqual(1,len(entries))
        self.assertEqual(self.now,entries[0]['timestamp'])

    def test_invalid_query_parameters_are_rejected(self):
        for start,end,offset,limit,result in [
            (float('nan'),self.now,0,50,''), (self.now,self.now-1,0,50,''),
            (self.now-93*86400,self.now,0,50,''), (self.now-1,self.now,0,101,''),
            (self.now-1,self.now,-1,50,''), (self.now-1,self.now,0,50,'invalid')]:
            with self.assertRaises(ValueError):
                self.store.read(start,end,offset,limit,result)

    def test_corrupt_lines_do_not_hide_valid_history(self):
        self.store.append('a','b','manual','success')
        p=next(self.store.root.glob('*.jsonl'))
        with p.open('a') as f:
            f.write('{broken\n'+json.dumps({'timestamp':self.now,'result':'success'})+'\n')
        self.assertEqual(1,self.store.read(self.now-1,self.now+1)['total'])

    def test_history_survives_store_recreation_and_log_removal(self):
        self.store.append('a','b','auto','success')
        reopened=SwitchHistory(self.store.root)
        self.assertEqual(1,reopened.read(self.now-1,self.now+1)['total'])

    def test_retention_only_deletes_old_history_files(self):
        self.store.root.mkdir()
        old=self.store.root/((datetime.now(timezone.utc)-timedelta(days=92)).strftime('%Y-%m-%d')+'.jsonl')
        old.write_text('{}\n')
        other=self.store.root/'notes.txt';other.write_text('keep')
        self.store.append('a','b','manual','success')
        self.assertFalse(old.exists())
        self.assertTrue(other.exists())

    def test_legacy_import_preserves_local_log_time_and_is_idempotent(self):
        logs=Path(self.tmp.name)/'logs';logs.mkdir()
        stamp=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        rows=[{'timestamp':stamp,'message':'节点 node-a 连接成功，出口网卡 tun0 已启用'},
              {'timestamp':stamp,'message':'连接节点 node-c 失败: timeout'},
              {'timestamp':stamp,'message':'热备提升成功: node-b'}]
        (logs/(stamp[:10]+'.json')).write_text('\n'.join(json.dumps(r,ensure_ascii=False) for r in rows))
        self.store.import_legacy(logs)
        self.store.import_legacy(logs)
        self.assertEqual(3,self.store.read(self.now-5,self.now+5)['total'])
        # Resume after a crash between appending records and writing the marker.
        (self.store.root/'.legacy-imported').unlink()
        self.store.import_legacy(logs)
        entries=self.store.read(self.now-5,self.now+5)['entries']
        self.assertEqual(3,len(entries))
        second=next(e for e in entries if e['to_node_id']=='node-b')
        self.assertEqual('node-a',second['from_node_id'])
        self.assertEqual('legacy',second['source'])
        self.assertAlmostEqual(datetime.strptime(stamp,'%Y-%m-%d %H:%M:%S').astimezone().timestamp(),second['timestamp'])

    def test_statistics_only_counts_verified_exit_changes_by_default(self):
        self.store.append('same-node','same-node','auto','reconnected',from_exit_ip='203.0.113.1',to_exit_ip='203.0.113.2')
        self.store.append('different-node-a','different-node-b','auto','success',from_exit_ip='203.0.113.1',to_exit_ip='203.0.113.1')
        self.store.append('a','b','auto','success',from_exit_ip='2001:db8::1',to_exit_ip='2001:0db8:0:0:0:0:0:1')
        self.store.append('JP_192.0.2.1_443_tcp','JP_192.0.2.2_443_tcp','legacy','success',source='legacy')
        self.store.append('JP_192.0.2.1_443_tcp','JP_192.0.2.1_555_tcp','legacy','success',source='legacy')
        self.store.append('unknown-a','unknown-b','auto','success')
        self.store.append('JP_192.0.2.1_443_tcp','JP_192.0.2.2_443_tcp','auto','success',to_exit_ip='203.0.113.2')
        for result,trigger,old in [('failed','auto','a'),('success','startup','a'),('success','manual','')]:
            self.store.append(old,'b',trigger,result,from_exit_ip='203.0.113.1',to_exit_ip='203.0.113.2')
        data=self.store.statistics(self.now-1,self.now+1)
        self.assertEqual(1,data['total'])
        self.assertEqual(dict(confirmed=1,inferred=1,unknown=2,same_ip=3,excluded=3),data['counts'])
        estimated=self.store.statistics(self.now-1,self.now+1,include_legacy=True)
        self.assertEqual(2,estimated['total'])

    def test_statistics_reads_beyond_history_page_and_fills_zeros(self):
        for i in range(105):
            self.store.append('a','b','auto','success',from_exit_ip='203.0.113.1',to_exit_ip='203.0.113.2')
        data=self.store.statistics(self.now-86400,self.now+1,'hour','Asia/Shanghai')
        self.assertEqual(105,data['total'])
        self.assertEqual(105,sum(b['count'] for b in data['buckets']))
        self.assertTrue(any(b['count']==0 for b in data['buckets']))
        self.assertGreater(len(data['buckets']),23)

    def test_statistics_uses_browser_timezone_and_monday_weeks(self):
        # Sunday 23:30 / Monday 00:30 in Shanghai, independent of host TZ.
        first=datetime(2024,11,10,15,30,tzinfo=timezone.utc).timestamp()
        second=first+3600
        with mock.patch.object(self.store,'_prune'):
            for stamp in [first,second]:
                self.store.append('a','b','auto','success',timestamp=stamp,from_exit_ip='192.0.2.1',to_exit_ip='192.0.2.2')
            data=self.store.statistics(first-60,second+60,'week','Asia/Shanghai')
            self.assertEqual([1,1],[b['count'] for b in data['buckets']])
            self.assertTrue(all(datetime.fromisoformat(b['label']).weekday()==0 for b in data['buckets']))

    def test_hourly_statistics_keeps_dst_repeated_hours_separate(self):
        start=datetime(2024,11,3,4,tzinfo=timezone.utc).timestamp()
        with mock.patch.object(self.store,'_prune'):
            for hour in [5,6]:
                self.store.append('a','b','auto','success',timestamp=datetime(2024,11,3,hour,30,tzinfo=timezone.utc).timestamp(),from_exit_ip='192.0.2.1',to_exit_ip='192.0.2.2')
            data=self.store.statistics(start,start+25*3600,'hour','America/New_York')
        self.assertEqual(25,len(data['buckets']))
        self.assertEqual(2,data['total'])
        repeated=[b for b in data['buckets'] if 'T01:00:00' in b['label']]
        self.assertEqual([1,1],[b['count'] for b in repeated])
        self.assertNotEqual(repeated[0]['label'],repeated[1]['label'])

    def test_statistics_rejects_invalid_granularity_and_timezone(self):
        for kind,tz in [('minute','UTC'),('day','../../etc/passwd'),('week','not-a-zone')]:
            with self.assertRaises(ValueError):
                self.store.statistics(self.now-1,self.now+1,kind,tz)
