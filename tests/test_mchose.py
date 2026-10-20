"""Tests for providers/mchose.py - the A5 Pro Max addition. No hardware is needed.

The exchange is the one z750sasr/mchose-a5-pro-max-web-driver documents for the
first-generation A5 Pro Max (docs/PROTOCOL.md and lib/a5-protocol.ts): 64-byte feature
reports on the vendor collection (usage page 0xFFFF), report id 0; the reference's
payload carries route 2 at its byte 2, its own length 2 at byte 3, page 0 at byte 4 and
command 0x83 at byte 5. The reply starts with the 0xA1 marker and echoes page and
command at its bytes 4 and 5, with the charging state in byte 6 and the level in byte 7
(clamped to 100, as the reference clamps it). The collection shape is the reporter's
diagnostics dump from issue #149 (2023:f013 'MCHOSE A5 2.4G': the mouse, ffa0:0001 and
ffff:0001 on interface 1, a keyboard, consumer and system collection, then ffff:0000 on
interface 2).

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import mchose as M  # noqa: E402


def answer(level=58, charging=False):
    """A reply as the reference expects it: 0xA1, ..., page, command, charge, level."""
    return ([0xA1, 0x00, 0x00, 0x00, M.A5_PAGE, M.A5_CMD_BATTERY,
             1 if charging else 0, level] + [0x00] * 56)


class FakeMchose:
    """One fake receiver for every open_path; only `answer_path` replies with 0xA1."""

    def __init__(self, level=58, charging=False, answer_path=None):
        self.level = level
        self.charging = charging
        self.answer_path = answer_path
        self.path = None
        self.sent = []                    # (path, bytes)
        self.reads = 0

    # hid.device API
    def open_path(self, path):
        self.path = path

    def close(self):
        pass

    def send_feature_report(self, data):
        self.sent.append((self.path, bytes(data)))
        return len(data)

    def get_feature_report(self, report_id, length):
        self.reads += 1
        if self.path == self.answer_path:
            return [M.A5_REPORT_ID] + answer(self.level, self.charging)
        return [0x00] * length               # silent: no 0xA1 marker in it


def entries(pid=0xF013, prefix=b"a5"):
    # the reporter's dump for 2023:f013, in its order
    shape = [(0, 0x0001, 0x0002),   # the mouse: never written to
             (1, 0xFFA0, 0x0001),
             (1, 0xFFFF, 0x0001),   # the vendor page the reference prefers
             (1, 0x0001, 0x0006),
             (1, 0x000C, 0x0001),
             (1, 0x0001, 0x0080),
             (2, 0xFFFF, 0x0000)]
    return [{"vendor_id": 0x2023, "product_id": pid, "interface_number": i,
             "usage_page": p, "usage": u,
             "path": prefix + b"-%d-%04x-%02x" % (i, p, u),
             "product_string": "MCHOSE A5 2.4G"}
            for i, p, u in shape]


FF_F1 = entries()[2]["path"]         # ffff:0001 on interface 1
FF_F2 = entries()[6]["path"]         # ffff:0000 on interface 2
FFA0 = entries()[1]["path"]


class ParseTest(unittest.TestCase):
    def test_the_request_frame_is_the_references_plus_the_report_id(self):
        self.assertEqual(M.make_a5_request(),
                         [0x00, 0x00, 0x00, 0x02, 0x02, 0x00, 0x83] + [0x00] * 58)
        self.assertEqual(len(M.make_a5_request()), 65)

    def test_the_answer_reads_level_and_charging(self):
        self.assertEqual(M.parse_a5(answer(58, True)), (58, True))
        self.assertEqual(M.parse_a5(answer(58, False)), (58, False))

    def test_the_report_id_may_lead_the_answer(self):
        self.assertEqual(M.parse_a5([0x00] + answer(58, True)), (58, True))

    def test_a_level_above_100_is_clamped_as_the_reference_clamps_it(self):
        self.assertEqual(M.parse_a5(answer(200, False)), (100, False))

    def test_another_page_or_command_is_refused(self):
        wrong = answer()
        wrong[5] = 0x84
        self.assertIsNone(M.parse_a5(wrong))
        wrong = answer()
        wrong[4] = 0x02
        self.assertIsNone(M.parse_a5(wrong))

    def test_a_silent_or_short_answer_is_refused(self):
        self.assertIsNone(M.parse_a5([0x00] * 65))
        self.assertIsNone(M.parse_a5([0xA1, 0x00, 0x00]))
        self.assertIsNone(M.parse_a5(None))


class PollTest(unittest.TestCase):
    def setUp(self):
        self._saved = (M.hid, M.hidlist, M.time)
        self.now = [1000.0]
        M.time = types.SimpleNamespace(sleep=lambda s: None, time=lambda: self.now[0])

    def tearDown(self):
        M.hid, M.hidlist, M.time = self._saved

    def poll(self, fake, ents):
        M.hid = types.SimpleNamespace(device=lambda: fake)
        M.hidlist = types.SimpleNamespace(
            enumerate=lambda vid=0: [e for e in ents if e["vendor_id"] == vid])
        p = M.MchoseProvider()
        return p.poll(), p.diagnostics()

    def test_the_receiver_is_read_on_the_ffff_collection(self):
        ents = entries()
        fake = FakeMchose(level=58, charging=True, answer_path=FF_F1)
        out, diag = self.poll(fake, ents)
        self.assertEqual([(s.key, s.name, s.level, s.charging, s.online, s.source, s.kind)
                          for s in out],
                         [("mchose:2023", "MCHOSE A5 2.4G", 58, True, True,
                           "mchose", "mouse")])
        self.assertEqual({p for p, _ in fake.sent}, {FF_F1})    # nothing else was written to
        self.assertEqual(fake.sent[0][1], bytes(M.make_a5_request()))

    def test_the_second_ffff_collection_is_tried_too(self):
        # the same reply arrives on either ffff collection depending on the model/revision
        ents = entries()
        fake = FakeMchose(level=77, charging=False, answer_path=FF_F2)
        out, _ = self.poll(fake, ents)
        self.assertEqual([s.level for s in out], [77])
        self.assertTrue(any(p == FF_F2 for p, _ in fake.sent))
        self.assertNotIn(FFA0, {p for p, _ in fake.sent})

    def test_another_2023_pid_is_left_alone(self):
        ents = entries(pid=0xF017)       # an A5 Pro receiver, not a Pro Max identity
        fake = FakeMchose(level=58, charging=False, answer_path=FF_F1)
        out, diag = self.poll(fake, ents)
        self.assertEqual(out, [])
        self.assertEqual(fake.sent, [])
        self.assertTrue(any("leaving it alone" in line for line in diag))

    def test_silence_gives_nothing_and_says_so(self):
        ents = entries()
        fake = FakeMchose(answer_path=None)
        out, diag = self.poll(fake, ents)
        self.assertEqual(out, [])
        # the 0xFFA0 sibling is not written to even when the mouse stays silent
        # (review by @ahmedkhursheed23)
        self.assertNotIn(FFA0, {p for p, _ in fake.sent})
        self.assertTrue(any("no A1 answer" in line for line in diag))


class G7SmoothingTest(unittest.TestCase):
    def setUp(self):
        self.provider = M.MchoseProvider()

    def sample(self, level, at, charging=False):
        with mock.patch.object(M.time, "monotonic", return_value=at):
            return self.provider._smooth_g7(level, charging)

    def test_startup_80_waits_for_a_real_reading_instead_of_claiming_80(self):
        self.assertIsNone(self.sample(80, 0))
        self.assertIsNone(self.sample(80, 60))
        self.assertIsNone(self.sample(80, 120))
        self.assertEqual(self.sample(12, 180), 12)

    def test_five_spaced_startup_80s_eventually_show_a_level(self):
        for at in (0, 60, 120, 180):
            self.assertIsNone(self.sample(80, at))
        self.assertEqual(self.sample(80, 240), 80)
        self.assertEqual(self.sample(80, 300), 80)

    def test_fast_startup_refreshes_do_not_confirm_80(self):
        for at in range(5):
            self.assertIsNone(self.sample(80, at))
        for at in (5, 10, 15):
            self.assertIsNone(self.sample(80, at))
        self.assertEqual(self.sample(80, 20), 80)

    def test_startup_80_confirmation_resets_after_a_gap_or_backward_clock(self):
        for restart in (780, 100):
            with self.subTest(restart=restart):
                self.provider = M.MchoseProvider()
                for at in (0, 60, 120, 180):
                    self.assertIsNone(self.sample(80, at))
                for at in range(restart, restart + 240, 60):
                    self.assertIsNone(self.sample(80, at))
                self.assertEqual(self.sample(80, restart + 240), 80)

    def test_a_non80_reading_ends_startup_wait_even_on_a_fast_refresh(self):
        for at in (0, 60, 120, 180):
            self.assertIsNone(self.sample(80, at))
        self.assertEqual(self.sample(12, 181), 12)
        self.assertEqual(self.sample(80, 240), 12)

    def test_two_similar_spaced_large_drops_are_shown_on_the_second_poll(self):
        for i in range(5):
            self.sample(50, i * 60)
        self.assertEqual(self.sample(10, 300), 50)
        self.assertEqual(self.sample(12, 360), 11)

    def test_an_isolated_large_low_spike_does_not_move_the_level(self):
        for i in range(5):
            self.sample(50, i * 60)
        self.assertEqual(self.sample(0, 300), 50)
        self.assertEqual(self.sample(50, 360), 50)

    def test_inconsistent_low_replies_do_not_confirm_a_drop(self):
        for i in range(5):
            self.sample(50, i * 60)
        self.assertEqual(self.sample(10, 300), 50)
        self.assertEqual(self.sample(25, 360), 50)
        self.assertEqual(self.sample(50, 420), 50)

    def test_a_fast_rebound_cancels_low_confirmation(self):
        for i in range(5):
            self.sample(50, i * 60)
        self.assertEqual(self.sample(10, 300), 50)
        self.assertEqual(self.sample(50, 301), 50)
        self.assertEqual(self.sample(10, 305), 50)
        self.assertEqual(self.sample(10, 310), 10)

    def test_rejected_80_between_lows_cancels_confirmation(self):
        for i in range(5):
            self.sample(50, i * 60)
        self.assertEqual(self.sample(10, 300), 50)
        self.assertEqual(self.sample(80, 330), 50)
        self.assertEqual(self.sample(10, 360), 50)

    def test_a_twenty_point_drop_keeps_the_normal_median(self):
        for i in range(5):
            self.sample(50, i * 60)
        self.assertEqual(self.sample(30, 300), 50)
        self.assertEqual(self.sample(30, 360), 50)
        self.assertEqual(self.sample(30, 420), 30)

    def test_confirmed_zero_is_not_hidden(self):
        for i in range(5):
            self.sample(50, i * 60)
        self.assertEqual(self.sample(0, 300), 50)
        self.assertEqual(self.sample(0, 360), 0)

    def test_charging_cancels_pending_low_confirmation(self):
        for i in range(5):
            self.sample(50, i * 60)
        self.assertEqual(self.sample(10, 300), 50)
        self.assertEqual(self.sample(90, 301, charging=True), 90)
        self.assertEqual(self.sample(10, 360), 50)

    def test_a_confirmed_drop_can_recover_upwards(self):
        for i in range(5):
            self.sample(50, i * 60)
        self.sample(10, 300)
        self.assertEqual(self.sample(10, 360), 10)
        for i in range(7, 10):
            value = self.sample(50, i * 60)
        self.assertEqual(value, 50)

    def test_stale_80_is_rejected_at_high_and_low_charge(self):
        for base in (100, 94, 46, 12, 2):
            with self.subTest(base=base):
                self.provider = M.MchoseProvider()
                self.assertEqual(self.sample(base, 0), base)
                for i in range(1, 20):
                    self.assertEqual(self.sample(80, i * 60), base)

    def test_real_80_near_the_current_trend_is_kept(self):
        for i, level in enumerate([83, 82, 81, 80, 80, 80]):
            value = self.sample(level, i * 60)
        self.assertEqual(value, 80)

    def test_an_isolated_non80_spike_does_not_move_the_median(self):
        for i in range(5):
            self.sample(46, i * 60)
        self.assertEqual(self.sample(65, 300), 46)
        self.assertEqual(self.sample(46, 360), 46)

    def test_repeated_fast_refreshes_do_not_replace_the_median(self):
        for i in range(5):
            self.sample(46, i * 60)
        for at in range(300, 305):
            self.assertEqual(self.sample(12, at), 46)

    def test_sustained_discharge_is_followed_without_ten_percent_rounding(self):
        for i in range(5):
            self.sample(46, i * 60)
        for i in range(5, 8):
            value = self.sample(37, i * 60)
        self.assertEqual(value, 37)

    def test_wrong_initial_reading_can_recover_upwards(self):
        for i in range(5):
            self.sample(12, i * 60)
        for i in range(5, 8):
            value = self.sample(18, i * 60)
        self.assertEqual(value, 18)

    def test_real_low_battery_is_not_floored_to_ten_percent(self):
        self.assertEqual(self.sample(2, 0), 2)
        for i in range(1, 5):
            value = self.sample(0, i * 60)
        self.assertEqual(value, 0)

    def test_charging_is_immediate_and_accepts_a_real_80(self):
        self.sample(12, 0)
        self.assertEqual(self.sample(80, 1, charging=True), 80)
        self.assertEqual(self.sample(100, 2, charging=True), 100)
        self.assertEqual(self.sample(100, 3), 100)

    def test_offline_gap_starts_fresh_without_inferring_a_charge_curve(self):
        self.sample(12, 0)
        self.assertIsNone(self.sample(80, 600))
        self.assertEqual(self.sample(100, 660), 100)

    def test_backward_clock_starts_a_fresh_estimate(self):
        self.sample(46, 100)
        self.assertEqual(self.sample(2, 50), 2)

    def test_new_lower_reading_after_a_gap_is_not_held_at_the_old_value(self):
        self.sample(46, 0)
        self.assertEqual(self.sample(2, 600), 2)

    def test_other_mchose_devices_keep_the_upstream_readings(self):
        entries = [{"vendor_id": M.MEASURED_VID, "product_id": 0x1020,
                    "usage_page": M.CONFIG_PAGE, "usage": 1,
                    "path": b"other-mchose", "product_string": "Other MCHOSE"}]
        with mock.patch.object(M.hidlist, "enumerate", side_effect=lambda vid: entries if vid == M.MEASURED_VID else []), \
                mock.patch.object(self.provider, "_read_collection", side_effect=[(83, 0, 0x31, 9), (42, 0, 0x31, 9)]):
            self.assertEqual(self.provider.poll()[0].level, 83)
            self.assertEqual(self.provider.poll()[0].level, 42)

    def test_provider_cleans_only_g7_output_and_keeps_raw_diagnostics(self):
        entries = [{"vendor_id": M.G7_VID, "product_id": M.G7_PID,
                    "usage_page": M.CONFIG_PAGE, "usage": 0x10,
                    "path": b"g7", "product_string": "MCHOSE G7"}]
        with mock.patch.object(M.hidlist, "enumerate", side_effect=lambda vid: entries if vid == M.G7_VID else []), \
                mock.patch.object(self.provider, "_read_g7", side_effect=[(12, False), (80, False)]), \
                mock.patch.object(M.time, "monotonic", side_effect=[0, 60]):
            self.assertEqual(self.provider.poll()[0].level, 12)
            self.assertEqual(self.provider.poll()[0].level, 12)
        self.assertTrue(any("raw=80%" in line for line in self.provider.diagnostics()))

    def test_provider_reports_unknown_for_only_a_startup_fallback(self):
        entries = [{"vendor_id": M.G7_VID, "product_id": M.G7_PID,
                    "usage_page": M.CONFIG_PAGE, "usage": 0x10,
                    "path": b"g7", "product_string": "MCHOSE G7"}]
        with mock.patch.object(M.hidlist, "enumerate", side_effect=lambda vid: entries if vid == M.G7_VID else []), \
                mock.patch.object(self.provider, "_read_g7", return_value=(80, False)):
            status, = self.provider.poll()
        self.assertIsNone(status.level)
        self.assertTrue(status.online)

if __name__ == "__main__":
    unittest.main()
