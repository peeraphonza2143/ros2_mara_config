"""
ทดสอบ StatsStore (ระยะทาง / เวลาทำงาน / ไทม์ไลน์ / บันทึกไฟล์) — ไม่ต้องมี ROS

รัน:  cd ~/ros2_ws/src/agv_web_app && python3 -m unittest discover -s test -v
"""
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock

from agv_web_app import stats
from agv_web_app.stats import DAYS, MAX_GAP_S, StatsStore

# 2026-03-10 12:00 ตามเวลาท้องถิ่น (กลางวัน → ไม่ชนขอบวันเมื่อบวกลบไม่กี่ชั่วโมง)
T0 = time.mktime((2026, 3, 10, 12, 0, 0, 0, 0, -1))
DAY = 86400.0


def run(store, start, seconds, v, mode, step=0.5):
    """ป้อนตัวอย่างทุก step วินาที ; คืนเวลาของตัวอย่างสุดท้าย"""
    n = int(round(seconds / step))
    for i in range(n + 1):
        store.update(start + i * step, v, mode)
    return start + n * step


class StatsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='stats_')
        self.path = os.path.join(self.tmp, 'sub', 'stats.json')    # โฟลเดอร์ย่อยยังไม่มี
        self.errors = []
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def make(self, **kw):
        return StatsStore(self.path, on_error=self.errors.append, **kw)


class DistanceTest(StatsBase):

    def test_distance_is_speed_times_time(self):
        s = self.make()
        run(s, T0, 10.0, 0.2, 'NAVIGATION')           # 10 s @ 0.2 m/s = 2.0 m
        self.assertAlmostEqual(s.summary(T0 + 10)['today']['distance_m'], 2.0, places=2)

    def test_speed_change_within_interval_uses_the_average(self):
        s = self.make()
        s.update(T0, 0.0, 'NAVIGATION')
        s.update(T0 + 1.0, 0.4, 'NAVIGATION')             # เร่ง 0 → 0.4 ใน 1 วิ = 0.2 m (ไม่ใช่ 0.4)
        self.assertAlmostEqual(s.summary(T0 + 1)['total']['distance_m'], 0.2, places=3)
        s.update(T0 + 2.0, 0.0, 'NAVIGATION')             # เบรก 0.4 → 0 = อีก 0.2 m
        self.assertAlmostEqual(s.summary(T0 + 2)['total']['distance_m'], 0.4, places=3)

    def test_reverse_counts_as_distance(self):
        s = self.make()
        run(s, T0, 10.0, -0.2, 'MANUAL')
        self.assertAlmostEqual(s.summary(T0 + 10)['total']['distance_m'], 2.0, places=2)

    def test_noise_below_threshold_is_ignored(self):
        s = self.make(noise_speed=0.02)
        run(s, T0, 600.0, 0.015, 'NAVIGATION')
        self.assertEqual(s.summary(T0 + 600)['total']['distance_m'], 0.0)

    def test_missing_speed_is_not_counted_and_not_guessed(self):
        s = self.make()
        s.update(T0, 0.2, 'NAVIGATION')
        s.update(T0 + 0.5, None, 'NAVIGATION')        # ข้อมูลขาด
        s.update(T0 + 1.0, None, 'NAVIGATION')
        self.assertEqual(s.summary(T0 + 1)['total']['distance_m'], 0.0)

    def test_big_time_gap_is_not_counted(self):
        s = self.make()
        s.update(T0, 0.3, 'NAVIGATION')
        s.update(T0 + MAX_GAP_S + 1, 0.3, 'NAVIGATION')   # โปรแกรมค้าง 3 วิ ห้ามนับ 0.9 m
        self.assertEqual(s.summary(T0 + 10)['total']['distance_m'], 0.0)
        self.assertEqual(s.summary(T0 + 10)['total']['active_s'], 0.0)

    def test_clock_going_backwards_is_not_counted(self):
        s = self.make()
        s.update(T0, 0.3, 'NAVIGATION')
        s.update(T0 - 5, 0.3, 'NAVIGATION')
        self.assertEqual(s.summary(T0)['total']['distance_m'], 0.0)

    def test_distance_counts_even_in_standby_mode_none(self):
        """เข็นรถด้วยมือตอน Standby: ระยะทางนับ (หุ่นเคลื่อนที่จริง) แต่เวลาทำงานไม่นับ"""
        s = self.make()
        run(s, T0, 10.0, 0.2, None)
        out = s.summary(T0 + 10)
        self.assertAlmostEqual(out['today']['distance_m'], 2.0, places=2)
        self.assertEqual(out['today']['active_s'], 0.0)


class ActiveModeTest(unittest.TestCase):

    def test_only_running_real_modes_count(self):
        am = stats.active_mode
        for m in ('MAPPING', 'MANUAL', 'NAVIGATION'):
            self.assertEqual(am({'mode': m, 'state': 'RUNNING'}), m)
        self.assertIsNone(am({'mode': 'STANDBY', 'state': 'RUNNING'}))       # Standby ไม่ใช่เวลาทำงาน
        for state in ('STARTING', 'STOPPING', 'ERROR'):
            self.assertIsNone(am({'mode': 'NAVIGATION', 'state': state}), state)
        self.assertIsNone(am({'mode': 'WAT', 'state': 'RUNNING'}))
        self.assertIsNone(am({}))


class ResetTest(StatsBase):

    def filled(self, **kw):
        s = self.make(**kw)
        end = run(s, T0, 20.0, 0.2, 'NAVIGATION')        # 4.0 m, 20 s, มีช่วงโหมดเปิดอยู่
        s.save(end, force=True)
        return s, end

    def test_reset_clears_everything_and_persists(self):
        s, end = self.filled()
        self.assertTrue(s.reset(end + 1, who='10.0.0.5'))
        out = s.summary(end + 1)
        self.assertEqual((out['total']['distance_m'], out['total']['active_s']), (0.0, 0.0))
        self.assertEqual(out['total']['since'], end + 1)
        self.assertEqual((out['today']['distance_m'], out['today']['active_s']), (0.0, 0.0))
        self.assertTrue(all(d['distance_m'] == 0 and d['active_s'] == 0 for d in out['days']))
        self.assertEqual(out['timeline'], [])
        r = self.make()                                   # โหลดใหม่จากไฟล์ → ต้องว่างจริง ไม่ใช่แค่ใน memory
        self.assertEqual(r.summary(end + 2)['total']['distance_m'], 0.0)
        self.assertEqual(r.summary(end + 2)['timeline'], [])

    def test_previous_file_is_kept_as_bak_once(self):
        s, end = self.filled()
        s.reset(end + 1)
        bak = json.load(open(self.path + '.bak'))
        self.assertAlmostEqual(bak['total']['distance_m'], 4.0, places=2)
        run(s, end + 2, 10.0, 0.2, 'NAVIGATION')          # ทำงานต่อ แล้วรีเซ็ตอีกรอบ → .bak ถูกทับ (เก็บชุดเดียว)
        s.save(end + 100, force=True)
        s.reset(end + 200)
        bak2 = json.load(open(self.path + '.bak'))
        self.assertAlmostEqual(bak2['total']['distance_m'], 2.0, places=2)

    def test_bak_holds_unsaved_in_memory_state_not_the_stale_file(self):
        s = self.make()
        s.update(T0, 0.2, 'NAVIGATION')
        s.save(T0, force=True)                            # ไฟล์เก่าเกือบว่าง
        run(s, T0 + 0.5, 20.0, 0.2, 'NAVIGATION')         # ~4 m ที่ยังไม่ถูกบันทึก (รอบ 30 วิยังไม่ถึง)
        s.reset(T0 + 25)
        bak = json.load(open(self.path + '.bak'))
        self.assertAlmostEqual(bak['total']['distance_m'], 4.1, places=2)    # 41 ช่วง × 0.5 วิ × 0.2 m/s

    def test_running_mode_keeps_counting_from_the_reset_point(self):
        s, end = self.filled()
        s.reset(end + 0.5)
        run(s, end + 1.0, 10.0, 0.2, 'NAVIGATION')        # โหมดเดิมยังรันอยู่
        out = s.summary(end + 11)
        self.assertAlmostEqual(out['total']['distance_m'], 2.0, places=1)    # ไม่นับของก่อนรีเซ็ต
        self.assertAlmostEqual(out['total']['active_s'], 10.0, places=0)
        self.assertEqual(len(out['timeline']), 1)
        self.assertGreaterEqual(out['timeline'][0]['start'], end + 1.0)      # ช่วงใหม่เริ่มหลังรีเซ็ต
        self.assertTrue(out['timeline'][0]['open'])

    def test_reset_without_existing_file_makes_no_bak(self):
        s = self.make()
        self.assertTrue(s.reset(T0))
        self.assertFalse(os.path.exists(self.path + '.bak'))

    def test_reset_clears_corrupt_note(self):
        os.makedirs(os.path.dirname(self.path))
        open(self.path, 'w').write('nope')
        s = self.make()
        self.assertEqual(s.summary(T0)['persist']['note'], 'corrupt')
        s.reset(T0)
        self.assertIsNone(s.summary(T0)['persist']['note'])

    def test_unwritable_reset_still_clears_memory_and_reports(self):
        s, end = self.filled()
        with mock.patch('agv_web_app.stats.os.replace', side_effect=OSError('boom')):
            self.assertFalse(s.reset(end + 1))
        out = s.summary(end + 1)
        self.assertEqual(out['total']['distance_m'], 0.0)
        self.assertFalse(out['persist']['ok'])

    def test_backup_failure_does_not_block_reset(self):
        s, end = self.filled()
        with mock.patch.object(StatsStore, '_write_file', side_effect=[OSError('nope'), None]):
            self.assertTrue(s.reset(end + 1))
        self.assertEqual(s.summary(end + 1)['total']['distance_m'], 0.0)
        self.assertEqual(len(self.errors), 1)

    def test_reset_is_announced_with_who(self):
        events = []
        s = StatsStore(self.path, on_event=events.append)
        s.reset(T0, who='192.168.1.9')
        self.assertEqual(len(events), 1)
        self.assertIn('192.168.1.9', events[0])


class UptimeAndTimelineTest(StatsBase):

    def test_active_time_counts_only_when_in_a_mode(self):
        s = self.make()
        end = run(s, T0, 10.0, 0.0, None)
        end = run(s, end + 0.5, 20.0, 0.0, 'MAPPING')
        self.assertAlmostEqual(s.summary(end)['today']['active_s'], 20.0, places=1)

    def test_timeline_records_each_mode_session(self):
        s = self.make()
        e1 = run(s, T0, 10.0, 0.0, 'MAPPING')
        e2 = run(s, e1 + 0.5, 5.0, 0.0, None)             # Standby คั่น
        run(s, e2 + 0.5, 10.0, 0.0, 'NAVIGATION')
        tl = s.summary(T0 + 40)['timeline']
        self.assertEqual([x['mode'] for x in tl], ['MAPPING', 'NAVIGATION'])
        self.assertFalse(tl[0]['open'])
        self.assertTrue(tl[1]['open'])
        self.assertLess(tl[0]['end'], tl[1]['start'])

    def test_switching_directly_between_modes_makes_two_segments(self):
        s = self.make()
        e = run(s, T0, 5.0, 0.0, 'MANUAL')
        run(s, e + 0.5, 5.0, 0.0, 'NAVIGATION')
        tl = s.summary(T0 + 20)['timeline']
        self.assertEqual([x['mode'] for x in tl], ['MANUAL', 'NAVIGATION'])

    def test_data_gap_splits_segment_at_last_seen_time(self):
        s = self.make()
        s.update(T0, 0.0, 'NAVIGATION')
        s.update(T0 + 1, 0.0, 'NAVIGATION')
        s.update(T0 + 100, 0.0, 'NAVIGATION')             # ค้าง 99 วิ
        tl = s.summary(T0 + 101)['timeline']
        self.assertEqual(len(tl), 2)
        self.assertEqual(tl[0]['end'], T0 + 1)            # ไม่ลากเส้นข้ามช่วงที่ไม่มีข้อมูล

    def test_timeline_only_shows_last_24h_clipped(self):
        s = self.make()
        run(s, T0, 10.0, 0.0, 'MAPPING')
        later = T0 + 30 * 3600
        run(s, later, 10.0, 0.0, 'MANUAL')
        tl = s.summary(later + 10)['timeline']
        self.assertEqual([x['mode'] for x in tl], ['MANUAL'])
        s2 = self.make()
        s2.segments = [{'start': T0 - 3600, 'end': T0 + 3600, 'mode': 'MAPPING'}]
        tl = s2.summary(T0 + 3600 + 24 * 3600 - 1800)['timeline']
        self.assertEqual(len(tl), 1)
        self.assertGreater(tl[0]['start'], T0 - 3600)     # ถูกตัดที่ขอบหน้าต่าง

    def test_segment_count_is_capped(self):
        s = self.make()
        t = T0
        for i in range(stats.MAX_SEGMENTS + 50):
            s.update(t, 0.0, 'MANUAL' if i % 2 else 'MAPPING')
            t += 0.5
        self.assertLessEqual(len(s.segments), stats.MAX_SEGMENTS)


class DaysTest(StatsBase):

    def test_crossing_midnight_splits_into_two_days(self):
        s = self.make()
        before = time.mktime((2026, 3, 10, 23, 59, 55, 0, 0, -1))
        end = run(s, before, 10.0, 0.2, 'NAVIGATION')     # คร่อม 00:00
        out = s.summary(end)
        self.assertEqual(out['today']['date'], '2026-03-11')
        self.assertEqual(out['days'][-1]['date'], '2026-03-11')
        self.assertEqual(out['days'][-2]['date'], '2026-03-10')
        self.assertGreater(out['days'][-2]['distance_m'], 0.5)
        self.assertGreater(out['days'][-1]['distance_m'], 0.5)
        self.assertAlmostEqual(out['days'][-2]['distance_m'] + out['days'][-1]['distance_m'],
                               out['total']['distance_m'], places=1)

    def test_days_list_always_has_7_entries_oldest_first_with_zero_fill(self):
        s = self.make()
        run(s, T0, 2.0, 0.2, 'NAVIGATION')
        days = s.summary(T0 + 2)['days']
        self.assertEqual(len(days), DAYS)
        self.assertEqual([d['date'] for d in days], sorted(d['date'] for d in days))
        self.assertEqual(sum(1 for d in days if d['distance_m'] > 0), 1)
        self.assertEqual(days[-1]['date'], '2026-03-10')

    def test_old_days_are_pruned_but_total_keeps(self):
        s = self.make()
        run(s, T0, 10.0, 0.2, 'NAVIGATION')
        later = T0 + 10 * DAY
        run(s, later, 10.0, 0.2, 'NAVIGATION')
        self.assertNotIn('2026-03-10', s.days)
        out = s.summary(later + 10)
        self.assertAlmostEqual(out['total']['distance_m'], 4.0, places=1)
        self.assertAlmostEqual(out['today']['distance_m'], 2.0, places=1)

    def test_day_boundary_uses_injected_day_function(self):
        s = StatsStore(self.path, day_of=lambda ts: '2030-01-01')
        s.update(T0, 0.2, None)
        self.assertIn('2030-01-01', s.days)


class PersistenceTest(StatsBase):

    def test_save_and_reload_round_trip(self):
        s = self.make()
        end = run(s, T0, 10.0, 0.2, 'NAVIGATION')
        self.assertTrue(s.save(end, force=True))
        r = self.make()
        out = r.summary(end + 1)
        self.assertAlmostEqual(out['total']['distance_m'], 2.0, places=2)
        self.assertAlmostEqual(out['total']['active_s'], 10.0, places=1)
        self.assertEqual(out['persist'], {'ok': True, 'note': None})
        self.assertEqual(self.errors, [])

    def test_open_segment_is_closed_at_last_seen_after_unclean_restart(self):
        s = self.make()
        end = run(s, T0, 10.0, 0.0, 'NAVIGATION')
        s.save(end, force=True)
        r = self.make()
        self.assertEqual(r.segments[0]['end'], end)       # ไม่ลากยาวถึง "ตอนนี้"
        out = r.summary(end + 5 * 3600)
        self.assertFalse(out['timeline'][0]['open'])
        self.assertEqual(out['timeline'][0]['end'], end)

    def test_first_update_after_restart_does_not_count_downtime(self):
        s = self.make()
        end = run(s, T0, 10.0, 0.2, 'NAVIGATION')
        s.save(end, force=True)
        r = self.make()
        r.update(end + 3600, 0.2, 'NAVIGATION')           # ปิดไป 1 ชั่วโมง
        self.assertAlmostEqual(r.summary(end + 3600)['total']['distance_m'], 2.0, places=2)

    def test_corrupt_file_starts_fresh_and_is_kept_aside(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, 'w') as f:
            f.write('{ not json')
        s = self.make()
        self.assertEqual(s.summary(T0)['total']['distance_m'], 0.0)
        self.assertEqual(s.summary(T0)['persist']['note'], 'corrupt')
        self.assertTrue(os.path.exists(self.path + '.corrupt'))
        self.assertEqual(len(self.errors), 1)

    def test_wrong_shape_file_is_treated_as_corrupt(self):
        os.makedirs(os.path.dirname(self.path))
        for bad in ('[]', '{"total": 1}', '{"total": {"distance_m": "x", "active_s": 0},'
                    ' "days": {}, "segments": []}',
                    '{"total": {"distance_m": 1, "active_s": 0}, "days": {"bad-date":'
                    ' {"distance_m": 1, "active_s": 1}}, "segments": []}'):
            with open(self.path, 'w') as f:
                f.write(bad)
            s = self.make()
            self.assertEqual(s.summary(T0)['persist']['note'], 'corrupt', bad)
            self.assertEqual(s.total['distance_m'], 0.0, bad)

    def test_unwritable_location_keeps_counting_and_reports_once(self):
        os.makedirs(self.tmp + '/ro')
        s = StatsStore(self.tmp + '/ro/x/stats.json', on_error=self.errors.append)
        os.chmod(self.tmp + '/ro', 0o500)
        self.addCleanup(os.chmod, self.tmp + '/ro', 0o700)
        if os.access(self.tmp + '/ro', os.W_OK):
            self.skipTest('รันเป็น root: chmod ไม่มีผล')
        end = run(s, T0, 10.0, 0.2, 'NAVIGATION')
        self.assertFalse(s.save(end, force=True))
        self.assertFalse(s.save(end + 100, force=True))
        self.assertEqual(len(self.errors), 1)             # ไม่รายงานซ้ำทุกรอบ
        out = s.summary(end)
        self.assertFalse(out['persist']['ok'])
        self.assertAlmostEqual(out['total']['distance_m'], 2.0, places=2)

    def test_save_is_atomic_failure_between_write_and_rename_keeps_old_file(self):
        s = self.make()
        end = run(s, T0, 10.0, 0.2, 'NAVIGATION')
        s.save(end, force=True)
        good = open(self.path).read()
        run(s, end + 0.5, 10.0, 0.2, 'NAVIGATION')
        with mock.patch('agv_web_app.stats.os.replace', side_effect=OSError('boom')):
            self.assertFalse(s.save(end + 100, force=True))
        self.assertEqual(open(self.path).read(), good)    # ไฟล์เดิมยังสมบูรณ์
        json.loads(good)

    def test_save_is_throttled_and_skipped_when_unchanged(self):
        s = self.make()
        end = run(s, T0, 10.0, 0.2, 'NAVIGATION')
        self.assertTrue(s.save(end + stats.SAVE_INTERVAL_S))
        self.assertFalse(s.save(end + stats.SAVE_INTERVAL_S + 1))          # ไม่มีอะไรเปลี่ยน
        s.update(end + stats.SAVE_INTERVAL_S + 1.5, 0.2, 'NAVIGATION')
        self.assertFalse(s.save(end + stats.SAVE_INTERVAL_S + 2))          # เปลี่ยนแต่ยังไม่ถึงเวลา
        self.assertTrue(s.save(end + 2 * stats.SAVE_INTERVAL_S + 2))

    def test_no_path_means_memory_only(self):
        s = StatsStore('', on_error=self.errors.append)
        run(s, T0, 4.0, 0.2, 'NAVIGATION')
        self.assertFalse(s.save(T0 + 100, force=True))
        self.assertGreater(s.summary(T0 + 4)['total']['distance_m'], 0)

    def test_summary_shape_and_zero_rounds_at_first(self):
        out = self.make().summary(T0)
        self.assertEqual(set(out), {'now', 'today', 'total', 'days', 'timeline', 'window',
                                    'rounds', 'persist'})
        self.assertEqual((out['rounds'], out['today']['rounds'], out['total']['rounds']), (0, 0, 0))
        self.assertTrue(all(d['rounds'] == 0 for d in out['days']))
        json.dumps(out)

    def test_add_round_counts_per_day_and_total(self):
        s = self.make()
        self.assertEqual(s.add_round(T0), 1)
        self.assertEqual(s.add_round(T0 + 10), 2)
        out = s.summary(T0 + 20)
        self.assertEqual((out['rounds'], out['today']['rounds'], out['total']['rounds']), (2, 2, 2))
        self.assertEqual(out['days'][-1]['rounds'], 2)

    def test_rounds_split_by_day_and_pruned_with_days(self):
        s = StatsStore('', day_of=lambda ts: '2026-10-27' if ts < T0 + 86400 else '2026-10-28')
        s.add_round(T0)
        s.add_round(T0 + 90000)
        out = s.summary(T0 + 90000)
        self.assertEqual((out['today']['rounds'], out['total']['rounds']), (1, 2))
        self.assertEqual([d['rounds'] for d in out['days']][-2:], [1, 1])

    def test_rounds_persist_and_old_files_without_rounds_still_load(self):
        s = self.make()
        s.add_round(T0)
        s.add_round(T0)
        self.assertTrue(s.save(T0 + 100, force=True))
        again = StatsStore(s.path)
        self.assertEqual(again.summary(T0 + 100)['total']['rounds'], 2)
        with open(s.path, encoding='utf-8') as f:
            data = json.load(f)
        del data['total']['rounds']
        for rec in data['days'].values():
            del rec['rounds']
        with open(s.path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        old = StatsStore(s.path)
        self.assertIsNone(old.load_note)
        self.assertEqual(old.summary(T0 + 100)['total']['rounds'], 0)

    def test_reset_clears_rounds_and_backs_up_state_that_has_only_rounds(self):
        s = self.make()
        s.add_round(T0)
        s.reset(T0 + 5, who='t')
        out = s.summary(T0 + 6)
        self.assertEqual((out['rounds'], out['total']['rounds']), (0, 0))
        self.assertTrue(os.path.exists(s.path + '.bak'))


if __name__ == '__main__':
    unittest.main()
