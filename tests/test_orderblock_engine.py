"""Phase 7C task 3: Order Block detection engine. Deterministic synthetic candles; expected values are derived by hand from the written
rules in trademate/orderblocks/engine.py (see the comments on each fixture), not copied from the engine's output.
Pure Python: no broker, no Flask, no orders.

    python3 -m unittest tests.test_orderblock_engine -v
"""

import ast
import json
import math
import random
import unittest
from dataclasses import replace
from pathlib import Path

from trademate.orderblocks import (BEARISH, BULLISH, CANDIDATE, CONFIRMED, DATA_INCONSISTENT, DATA_INSUFFICIENT, DATA_INVALID, DATA_OK,
                                   EXPIRED, INVALIDATED, NOT_QUALIFIED, REFERENCE_LOST, OrderBlock, OrderBlockConfig,
                                   OrderBlockResult, analyze_order_blocks)
from trademate.structure import StructureConfig, analyze_market_structure

T0, STEP = 1_760_000_000, 300


def t(i):
    return T0 + i * STEP


def rows(ohlc, start=0):
    """Candle rows [time, open, high, low, close] from (open, high, low, close) tuples."""
    return [[t(start + i), o, h, l, c] for i, (o, h, l, c) in enumerate(ohlc)]


def structure(candles, left=1, right=1, closed=True, mode="close"):
    return analyze_market_structure(candles, StructureConfig(swing_left=left, swing_right=right, break_mode=mode, last_candle_closed=closed))


def ob(candles, left=1, right=1, closed=True, mode="close", config=None, **labels):
    return analyze_order_blocks(candles, structure(candles, left, right, closed, mode), config, **labels)


def one(res, direction=None):
    found = [b for b in res.blocks if direction is None or b.direction == direction]
    assert len(found) == 1, [(b.id, b.status) for b in res.blocks]
    return found[0]


def mirror(ohlc, k=24.0):
    """Price-mirrored candles: every high becomes a low and every up move a down move. Bullish scenarios become bearish ones."""
    return [(k - o, k - l, k - h, k - c) for (o, h, l, c) in ohlc]


# ---------------------------------------------------------------------------------------------------------------------------
# Fixture A (swing_left = swing_right = 1). Hand-derived:
#   swing HIGH at candle 1 (12, confirmed at candle 2); swing LOW at candle 5 (9.7, confirmed at candle 6).
#   candle 7 closes at 12.5 > 12 -> the structure engine reports a bullish BOS of that high.
#   source candle = the last BEARISH candle at or before the swing low and after the swing high: candle 5 is bullish, candle 4 is
#   bearish (10.5 -> 9.9) -> zone [9.8, 10.6]. Candidate known at candle 6, confirmed at candle 7.
A = [(10, 11, 9.5, 10.8), (10.8, 12, 10.6, 11.8), (11.8, 11.9, 11, 11.2), (11.2, 11.3, 10.4, 10.5),
     (10.5, 10.6, 9.8, 9.9), (9.9, 10.9, 9.7, 10.7), (10.7, 11.4, 10.5, 11.3), (11.3, 12.6, 11.2, 12.5)]

# Fixture D: two swing lows after the same swing high (12); the second (candle 7, low 9.5) is the deepest. Only the deepest low is the
# origin of the leg that breaks the 12 high at candle 10, so only its block (zone [9.5, 10.65]) is confirmed; the shallower candidate
# (candle 3, zone [10.0, 11.3]) is NOT_QUALIFIED. No closes below 10.0, so neither is invalidated by price.
D = [(10, 11, 9.5, 10.8), (10.8, 12, 10.6, 11.8), (11.8, 11.9, 11, 11.2), (11.2, 11.3, 10.0, 10.2), (10.2, 10.9, 10.1, 10.8),
     (10.8, 10.85, 10.7, 10.75), (10.75, 10.8, 10.55, 10.6), (10.6, 10.65, 9.5, 10.1), (10.1, 10.7, 9.9, 10.6),
     (10.6, 11.4, 10.5, 11.3), (11.3, 12.5, 11.2, 12.4)]

# Fixture C: a bearish BOS first (candle 5 breaks the 13 low), then a bullish CHoCH (candle 12 breaks the 14.4 high), with
# references replaced without being broken. Hand-derived blocks (swing_left = swing_right = 1):
#   bearish s=3 [13.8, 15.5] confirmed at 5 by the BOS (origin: highest high 15.5 at candle 3)
#   bullish s=5 [12.4, 14.3] expired REFERENCE_LOST at 8 (its high, 15.5, was replaced by the swing high 14.4 confirmed at 8)
#   bearish s=7 [13.4, 14.4] expired REFERENCE_LOST at 11 (its low 12.4 was replaced by the swing low 12.45 confirmed at 11); the
#       candle-12 close above 14.4 comes AFTER the expiry and changes nothing
#   bullish s=10 [12.45, 12.8] confirmed at 12 by the CHoCH
C = [(15, 16, 14, 14.5), (14.5, 15, 13, 13.2), (13.2, 14, 13.1, 13.9), (13.9, 15.5, 13.8, 15.4), (15.4, 15.4, 14, 14.2),
     (14.2, 14.3, 12.4, 12.5), (12.5, 13.6, 12.5, 13.5), (13.5, 14.4, 13.4, 14.3), (14.3, 14.3, 13.2, 13.3),
     (13.3, 13.4, 12.6, 12.7), (12.7, 12.8, 12.45, 12.5), (12.5, 13.3, 12.5, 13.2), (13.2, 14.6, 13.1, 14.5)]


# Fixture E (swing_left = 1, swing_right = 2). Hand-derived:
#   swing HIGH candle 1 (12, known at 3); swing LOW candle 3 (10.0, known at 5) -> candidate s=3 zone [10.0, 11.3];
#   deeper low at candle 6 (9.5; it CLOSES at 10.05 so the first zone is not violated), known at candle 8 -> candidate s=6 zone [9.5, 10.35];
#   candle 7 closes at 12.5 > 12: bullish BOS. The leg's origin is candle 6, but candle 6 only becomes a confirmed swing at candle 8.
#   With data ending at candle 7 nothing can be decided about s=3: it stays a candidate. At candle 8: s=3 NOT_QUALIFIED, s=6 CONFIRMED.
E = [(10, 11, 9.5, 10.8), (10.8, 12, 10.6, 11.8), (11.8, 11.9, 11, 11.2), (11.2, 11.3, 10.0, 10.2), (10.2, 10.5, 10.1, 10.4),
     (10.4, 10.45, 10.2, 10.3), (10.3, 10.35, 9.5, 10.05), (10.05, 12.6, 9.9, 12.5), (12.5, 12.9, 12.3, 12.8)]


class BullishAndBearishRules(unittest.TestCase):
    def test_valid_bullish_candidate_before_any_break(self):
        res = ob(rows(A[:7]))
        self.assertEqual(res.data_status, DATA_OK)
        b = one(res)
        self.assertEqual((b.direction, b.status), (BULLISH, CANDIDATE))
        self.assertEqual((b.lower, b.upper, b.source_index), (9.8, 10.6, 4))
        self.assertEqual((b.source_time, b.identified_time), (t(4), t(6)))
        self.assertEqual((b.confirmed_time, b.invalidated_time, b.expired_time, b.confirming_event_id), (None, None, None, None))
        self.assertIn("Candidate", b.explanation)
        self.assertIn("Not confirmed", b.explanation)

    def test_bullish_block_is_structurally_confirmed_by_the_break(self):
        res = ob(rows(A))
        b = one(res)
        self.assertEqual((b.direction, b.status, b.lower, b.upper), (BULLISH, CONFIRMED, 9.8, 10.6))
        self.assertEqual((b.source_time, b.identified_time, b.confirmed_time), (t(4), t(6), t(7)))
        self.assertEqual((b.confirming_event_type, b.reference_level, b.reference_time, b.origin_time), ("BOS", 12, t(1), t(5)))
        s = structure(rows(A))
        self.assertEqual(b.confirming_event_id, s.events[0].id)                 # the structure engine's own event
        self.assertEqual(b.swing_id, "low:%g" % t(5))
        self.assertIn("Confirmed", b.explanation)

    def test_bearish_candidate_and_confirmed_are_the_mirror_image(self):
        m = mirror(A)
        cand = one(ob(rows(m[:7])))
        self.assertEqual((cand.direction, cand.status), (BEARISH, CANDIDATE))
        self.assertEqual((cand.lower, cand.upper, cand.source_index), (24 - 10.6, 24 - 9.8, 4))
        conf = one(ob(rows(m)))
        self.assertEqual((conf.direction, conf.status, conf.lower, conf.upper), (BEARISH, CONFIRMED, 24 - 10.6, 24 - 9.8))
        self.assertEqual((conf.source_time, conf.identified_time, conf.confirmed_time), (t(4), t(6), t(7)))
        self.assertEqual((conf.confirming_event_type, conf.reference_level), ("BOS", 24 - 12))
        self.assertEqual((conf.source_open, conf.source_close), (24 - 10.5, 24 - 9.9))   # a bullish source candle (close > open)
        self.assertGreater(conf.source_close, conf.source_open)

    def test_choch_is_reported_as_the_structure_engine_reports_it(self):
        res = ob(rows(C))
        by = {(b.direction, b.source_index): b for b in res.blocks}
        self.assertEqual(len(res.blocks), 4)
        bear = by[(BEARISH, 3)]
        self.assertEqual((bear.status, bear.lower, bear.upper, bear.identified_time, bear.confirmed_time, bear.confirming_event_type),
                         (CONFIRMED, 13.8, 15.5, t(4), t(5), "BOS"))
        bull = by[(BULLISH, 10)]
        self.assertEqual((bull.status, bull.lower, bull.upper, bull.identified_time, bull.confirmed_time, bull.confirming_event_type),
                         (CONFIRMED, 12.45, 12.8, t(11), t(12), "CHOCH"))
        s = structure(rows(C))
        self.assertEqual([e.type for e in s.events], ["BOS", "CHOCH"])

    def test_replaced_reference_levels_expire_the_candidates_and_later_breaks_do_not_revive_them(self):
        res = ob(rows(C))
        by = {(b.direction, b.source_index): b for b in res.blocks}
        a = by[(BULLISH, 5)]
        self.assertEqual((a.status, a.expired_reason, a.expired_time, a.confirmed_time), (EXPIRED, REFERENCE_LOST, t(8), None))
        self.assertEqual((a.lower, a.upper), (12.4, 14.3))
        e = by[(BEARISH, 7)]
        self.assertEqual((e.status, e.expired_reason, e.expired_time), (EXPIRED, REFERENCE_LOST, t(11)))
        self.assertIsNone(e.invalidated_time)                                   # the candle-12 close above 14.4 came after the expiry
        self.assertIn("replaced", e.explanation)

    def test_only_the_leg_origin_is_confirmed_when_two_swing_lows_share_one_break(self):
        res = ob(rows(D))
        by = {b.source_index: b for b in res.blocks}
        self.assertEqual(set(by), {3, 7})
        deep, shallow = by[7], by[3]
        self.assertEqual((deep.status, deep.lower, deep.upper, deep.confirmed_time), (CONFIRMED, 9.5, 10.65, t(10)))
        self.assertEqual((shallow.status, shallow.expired_reason, shallow.expired_time, shallow.lower, shallow.upper),
                         (EXPIRED, NOT_QUALIFIED, t(10), 10.0, 11.3))
        self.assertIsNone(shallow.confirmed_time)

    def test_a_candidate_stays_undecided_until_the_legs_origin_swing_is_knowable(self):
        early = ob(rows(E[:8]), right=2)                                        # the break is on the last candle; candle 6 is not yet a swing
        self.assertEqual([(b.source_index, b.status) for b in early.blocks], [(3, CANDIDATE)])
        self.assertEqual(structure(rows(E[:8]), right=2).events[0].candle_index, 7)
        full = ob(rows(E), right=2)
        by = {b.source_index: b for b in full.blocks}
        self.assertEqual(set(by), {3, 6})
        self.assertEqual((by[3].status, by[3].expired_reason, by[3].expired_time), (EXPIRED, NOT_QUALIFIED, t(8)))   # decided only at candle 8
        self.assertEqual((by[6].status, by[6].lower, by[6].upper, by[6].confirmed_time), (CONFIRMED, 9.5, 10.35, t(8)))
        self.assertEqual((by[6].origin_time, by[3].origin_time), (t(6), t(3)))
        self.assertEqual(by[6].swing_id, "low:%g" % t(6))                       # the confirmed block reports the swing that WAS the origin

    def test_a_large_move_without_a_structural_break_stays_a_candidate(self):
        # candle 7 is the same big up candle (high 12.6) but CLOSES at 11.9, under the 12 level: close-mode structure has no break
        weak = A[:7] + [(11.3, 12.6, 11.2, 11.9)]
        res = ob(rows(weak))
        self.assertEqual(structure(rows(weak)).events, ())
        self.assertEqual(one(res).status, CANDIDATE)
        # the identical candles with a wick-mode structure engine DO break (the structure result decides what a break is)
        wick = ob(rows(weak), mode="wick")
        self.assertEqual(one(wick).status, CONFIRMED)

    def test_no_swings_means_no_blocks_however_strong_the_move(self):
        rising = [(10 + i, 10.6 + i, 9.9 + i, 10.5 + i) for i in range(12)]
        res = ob(rows(rising))
        self.assertEqual((res.blocks, res.data_status), ((), DATA_OK))

    def test_a_leg_without_an_opposite_coloured_candle_gives_no_block(self):
        all_bullish = [(10, 11, 9.5, 10.8), (10.8, 12, 10.6, 11.8), (11.8, 11.9, 11.0, 11.85), (11.2, 11.3, 10.4, 11.25),
                       (10.5, 10.6, 9.8, 10.55), (9.9, 10.9, 9.7, 10.7), (10.7, 11.4, 10.5, 11.3), (11.3, 12.6, 11.2, 12.5)]
        res = ob(rows(all_bullish))
        self.assertEqual(structure(rows(all_bullish)).events[0].type, "BOS")     # structure did break ...
        self.assertEqual(res.blocks, ())                                        # ... but there is no bearish candle to point at

    def test_a_swing_low_with_no_earlier_swing_high_gives_no_block(self):
        res = ob(rows(A[3:]))                                                   # starts after the swing high: the leg has no known start
        self.assertEqual(res.blocks, ())

    def test_a_doji_is_not_a_source_candle(self):
        doji = [list(c) for c in A]
        doji[4] = (10.2, 10.6, 9.8, 10.2)                                      # close == open
        res = ob(rows(doji))
        self.assertEqual(res.blocks and one(res).source_index, 3)               # falls back to the previous BEARISH candle (candle 3)
        self.assertNotEqual(one(res).source_index, 4)

    def test_the_zone_is_the_sources_full_range_and_open_close_are_reported(self):
        b = one(ob(rows(A)))
        self.assertEqual((b.upper, b.lower, b.size), (10.6, 9.8, 10.6 - 9.8))
        self.assertEqual((b.source_open, b.source_close), (10.5, 9.9))
        self.assertGreater(b.upper, b.lower)


class InvalidationAndExpiry(unittest.TestCase):
    def test_a_close_below_the_zone_after_confirmation_invalidates_but_keeps_the_confirmation(self):
        more = A + [(12.5, 12.6, 9.5, 9.7)]                                     # candle 8 closes at 9.7, under the zone's 9.8
        b = one(ob(rows(more)), BULLISH)
        self.assertEqual((b.status, b.confirmed_time, b.invalidated_time), (INVALIDATED, t(7), t(8)))
        self.assertIn("after it was confirmed", b.explanation)
        self.assertIn("closed below", b.explanation)

    def test_a_close_exactly_on_the_boundary_does_not_invalidate(self):
        more = A + [(12.5, 12.6, 9.5, 9.8)]                                     # close == lower boundary: not beyond it
        self.assertEqual(one(ob(rows(more)), BULLISH).status, CONFIRMED)

    def test_a_wick_through_the_zone_does_not_invalidate_in_close_mode_but_does_in_wick_mode(self):
        more = A + [(12.5, 12.6, 9.0, 12.0)]                                    # candle 8 wicks to 9.0, closes at 12
        self.assertEqual(one(ob(rows(more)), BULLISH).status, CONFIRMED)
        b = one(ob(rows(more), config=OrderBlockConfig(invalidation_mode="wick")), BULLISH)
        self.assertEqual(b.status, INVALIDATED)
        self.assertEqual(b.invalidated_time, t(5))                              # candle 5 already traded to 9.7, below the 9.8 low (see next test)

    def test_wick_mode_can_invalidate_before_the_block_was_even_identified(self):
        b = one(ob(rows(A), config=OrderBlockConfig(invalidation_mode="wick")))
        self.assertEqual((b.status, b.invalidated_time, b.identified_time), (INVALIDATED, t(5), t(6)))   # invalidated BEFORE identified
        self.assertIsNone(b.confirmed_time)                                     # and so never confirmed
        self.assertIn("never confirmed", b.explanation)

    def test_invalidated_before_confirmation_is_never_confirmed(self):
        broken = [list(c) for c in A]
        broken[6] = (10.7, 11.4, 9.75, 9.78)                                    # closes at 9.78 < 9.8 but its low holds above the 9.7 swing
        b = one(ob(rows(broken)))
        self.assertEqual((b.status, b.invalidated_time, b.confirmed_time), (INVALIDATED, t(6), None))
        self.assertEqual(structure(rows(broken)).events[0].type, "BOS")        # the structure DID break at candle 7: still not confirmed

    def test_a_candle_that_breaks_structure_and_violates_the_zone_counts_as_invalidated(self):
        v = [list(c) for c in A]
        v[4] = (10.5, 10.6, 9.7, 9.9)                                           # source low equals the swing low: only the break candle pierces
        v[7] = (11.3, 12.6, 9.0, 12.5)                                          # wick to 9.0 AND close above 12 on the same candle
        close_mode = one(ob(rows(v)))
        self.assertEqual((close_mode.status, close_mode.confirmed_time), (CONFIRMED, t(7)))
        wick_mode = one(ob(rows(v), config=OrderBlockConfig(invalidation_mode="wick")))
        self.assertEqual((wick_mode.status, wick_mode.invalidated_time, wick_mode.confirmed_time), (INVALIDATED, t(7), None))

    def test_bearish_invalidation_is_a_close_above_the_zone(self):
        more = mirror(A) + [(24 - 12.5, 24 - 9.5, 24 - 12.6, 24 - 9.7)]        # mirror of the candle-8 test: closes above the upper boundary
        b = one(ob(rows(more)), BEARISH)
        self.assertEqual((b.direction, b.status, b.confirmed_time, b.invalidated_time), (BEARISH, INVALIDATED, t(7), t(8)))
        self.assertIn("closed above", b.explanation)

    def test_max_leg_candles_is_optional_and_exact(self):
        # source candle 4, break candle 7: three candles apart
        self.assertEqual(one(ob(rows(A))).status, CONFIRMED)                                     # off by default
        self.assertEqual(one(ob(rows(A), config=OrderBlockConfig(max_leg_candles=3))).status, CONFIRMED)
        b = one(ob(rows(A), config=OrderBlockConfig(max_leg_candles=2)))
        self.assertEqual((b.status, b.expired_reason, b.expired_time), (EXPIRED, NOT_QUALIFIED, t(7)))
        self.assertIn("limit 2", b.explanation)

    def test_a_reference_that_was_already_broken_before_the_swing_low_creates_no_block(self):
        s = structure(rows(A))
        ev = replace(s.events[0], candle_index=3, candle_time=t(3))             # pretend the 12 high was broken at candle 3, before the low at 5
        faked = replace(s, events=(ev,))
        self.assertEqual(analyze_order_blocks(rows(A), faked).blocks, ())

    def test_an_ambiguous_same_candle_break_expires_instead_of_confirming(self):
        s = structure(rows(A))
        faked = replace(s, events=(replace(s.events[0], same_candle_conflict=True),))
        b = one(analyze_order_blocks(rows(A), faked))
        self.assertEqual((b.status, b.expired_reason, b.expired_time, b.confirmed_time), (EXPIRED, REFERENCE_LOST, t(7), None))
        self.assertIn("unknowable", b.explanation)

    def test_a_newer_swing_high_expires_a_candidate_and_the_new_break_gets_its_own_block(self):
        # after A[:7]: candle 7-8 form a swing high at 11.8 (below 12, so 12 is not broken) -> the old candidate's reference is replaced
        # candle 9 then closes above 11.8 (the NEW reference) -> a bullish BOS whose leg started at the swing low at candle 8
        more = A[:7] + [(11.3, 11.8, 11.2, 11.4), (11.4, 11.6, 11.0, 11.1), (11.1, 12.2, 11.0, 12.1)]
        res = ob(rows(more))
        by = {b.source_index: b for b in res.blocks if b.direction == BULLISH}        # (the new swing high also makes a bearish candidate)
        self.assertEqual(set(by), {4, 8})
        self.assertEqual((by[4].status, by[4].expired_reason, by[4].expired_time), (EXPIRED, REFERENCE_LOST, t(8)))
        self.assertEqual((by[8].status, by[8].lower, by[8].upper, by[8].confirmed_time, by[8].reference_level),
                         (CONFIRMED, 11.0, 11.6, t(9), 11.8))


class ConfirmationTimingAndLookAhead(unittest.TestCase):
    QUICK = A + [(12.5, 12.9, 12.2, 12.8)]                                      # candle 8 exists so a swing right of 3 can be confirmed

    def test_a_break_before_the_swing_is_knowable_is_confirmed_only_when_the_swing_becomes_knowable(self):
        # swing_right = 3: the swing low at candle 5 is only knowable at candle 8, but the break happened at candle 7
        res = ob(rows(self.QUICK), right=3)
        b = one(res)
        self.assertEqual((b.status, b.identified_time, b.confirmed_time), (CONFIRMED, t(8), t(8)))   # NOT t(7): not earlier than the evidence
        self.assertEqual(structure(rows(self.QUICK), right=3).events[0].candle_time, t(7))
        # and with the data ending at candle 7 the block does not exist at all
        self.assertEqual(ob(rows(self.QUICK[:8]), right=3).blocks, ())

    def test_nothing_is_confirmed_by_a_future_candle(self):
        full = ob(rows(A))
        for m in range(1, len(A) + 1):
            part = ob(rows(A[:m]))
            for b in part.blocks:
                last = t(m - 1)
                for stamp in (b.identified_time, b.confirmed_time, b.invalidated_time, b.expired_time):
                    self.assertTrue(stamp is None or stamp <= last, (m, b.id, stamp))
            if m < 7:
                self.assertEqual(part.blocks, (), m)                           # swing low not yet knowable
            elif m == 7:
                self.assertEqual(one(part).status, CANDIDATE)
            else:
                self.assertEqual(one(part), one(full))

    def test_prefixes_agree_with_the_full_run_on_every_known_fact(self):
        for fixture in (A, C, D, mirror(A), mirror(C), mirror(D)):
            self.assert_prefix_consistent(rows(fixture), range(3, len(fixture) + 1))
        for fixture in (E, mirror(E)):
            self.assert_prefix_consistent(rows(fixture), range(4, len(fixture) + 1), 1, 2)

    def assert_prefix_consistent(self, candles, sizes, left=1, right=1, mode="close", config=None):
        full = ob(candles, left, right, True, mode, config)
        by_id = {b.id: b for b in full.blocks}
        for m in sizes:
            part = ob(candles[:m], left, right, True, mode, config)
            if part.data_status != DATA_OK:
                continue
            last = candles[m - 1][0]
            seen = set()
            for b in part.blocks:
                seen.add(b.id)
                f = by_id[b.id]                                                 # a prefix never invents a block the full data lacks
                self.assertEqual(b.identified_time, f.identified_time, (m, b.id))
                for stamp in (b.identified_time, b.confirmed_time, b.invalidated_time, b.expired_time):
                    self.assertTrue(stamp is None or stamp <= last, (m, b.id))
                if b.confirmed_time is not None:
                    self.assertEqual(f.confirmed_time, b.confirmed_time, (m, b.id))
                if b.invalidated_time is not None:
                    self.assertEqual(f.invalidated_time, b.invalidated_time, (m, b.id))
                if b.status == EXPIRED:
                    self.assertEqual((f.status, f.expired_time, f.expired_reason), (EXPIRED, b.expired_time, b.expired_reason), (m, b.id))
                if b.status == CONFIRMED:
                    self.assertIn(f.status, (CONFIRMED, INVALIDATED), (m, b.id))
            for f in full.blocks:                                               # and never misses one that was already knowable
                if f.identified_time <= last:
                    self.assertIn(f.id, seen, (m, f.id))

    def test_the_still_forming_candle_marks_the_block_it_establishes_as_provisional(self):
        forming = ob(rows(A), closed=False)                                     # candle 7 (the break) is the newest and may still change
        b = one(forming)
        self.assertEqual((b.status, b.confirmed_time, b.provisional), (CONFIRMED, t(7), True))
        settled = one(ob(rows(A), closed=True))
        self.assertFalse(settled.provisional)
        cand = one(ob(rows(A[:7]), closed=False))                               # swing confirmed by the forming candle
        self.assertEqual((cand.status, cand.provisional), (CANDIDATE, True))
        self.assertFalse(one(ob(rows(A[:7]), closed=True)).provisional)
        older = one(ob(rows(A + [(12.5, 12.8, 12.3, 12.7)]), closed=False), BULLISH)     # a later forming candle changes nothing about this block
        self.assertEqual((older.status, older.provisional), (CONFIRMED, False))

    def test_provisional_is_exactly_the_status_being_established_by_the_newest_candle(self):
        for seed in range(12):
            c = random_candles(random.Random(seed), 120)
            for closed in (True, False):
                res = ob(c, 2, 2, closed)
                for b in res.blocks:
                    stamp = {CANDIDATE: b.identified_time, CONFIRMED: b.confirmed_time, INVALIDATED: b.invalidated_time,
                             EXPIRED: b.expired_time}[b.status]
                    self.assertEqual(b.provisional, (not closed) and stamp == c[-1][0], (seed, closed, b.id, b.status))


class StructuredResults(unittest.TestCase):
    def test_labels_are_copied_and_never_change_detection(self):
        with_labels = ob(rows(A), symbol="EURUSD", timeframe="H1")
        without = ob(rows(A))
        self.assertEqual((with_labels.symbol, with_labels.timeframe), ("EURUSD", "H1"))
        b = one(with_labels)
        self.assertEqual((b.symbol, b.timeframe), ("EURUSD", "H1"))
        self.assertEqual((one(without).symbol, one(without).timeframe), (None, None))
        a, c = b.to_dict(), one(without).to_dict()
        for k in ("symbol", "timeframe"):
            a.pop(k), c.pop(k)
        self.assertEqual(a, c)
        for bad in ({"symbol": 5}, {"timeframe": ["H1"]}):
            with self.assertRaises(ValueError):
                analyze_order_blocks(rows(A), structure(rows(A)), **bad)

    def test_ids_are_plain_epoch_digits_and_describe_direction_and_source(self):
        self.assertEqual(one(ob(rows(A))).id, f"OB-BULL-{t(4)}")
        self.assertEqual(one(ob(rows(mirror(A)))).id, f"OB-BEAR-{t(4)}")
        self.assertRegex(one(ob(rows(A))).id, r"^OB-BULL-\d{10}$")

    def test_result_is_json_serialisable_immutable_and_has_the_documented_fields(self):
        res = ob(rows(C), symbol="EURUSD", timeframe="M5")
        d = json.loads(json.dumps(res.to_dict()))
        self.assertEqual((d["data_status"], len(d["blocks"])), ("ok", 4))
        need = {"id", "direction", "status", "upper", "lower", "size", "source_time", "identified_time", "confirmed_time", "invalidated_time",
                "expired_time", "expired_reason", "confirming_event_id", "confirming_event_type", "provisional", "rule", "explanation",
                "symbol", "timeframe", "reference_level", "origin_time"}
        self.assertEqual(need - set(d["blocks"][0]), set())
        self.assertTrue(all(math.isfinite(v) for b in d["blocks"] for v in (b["upper"], b["lower"], b["size"])))
        self.assertTrue(all(b["rule"] == "structure-leg-origin-v1" and b["explanation"] for b in d["blocks"]))
        self.assertIsInstance(one(ob(rows(A))), OrderBlock)
        self.assertIsInstance(res, OrderBlockResult)
        with self.assertRaises(Exception):
            one(ob(rows(A))).upper = 99
        with self.assertRaises(Exception):
            res.data_status = "x"

    def test_every_block_is_in_chronological_order_by_source_candle(self):
        res = ob(rows(C))
        keys = [(b.source_time, b.direction) for b in res.blocks]
        self.assertEqual(keys, sorted(keys))

    def test_there_is_no_score_volume_or_trade_field_and_no_invented_claims(self):
        fields = set(one(ob(rows(A))).to_dict())
        for banned in ("score", "quality", "strength", "probability", "volume", "institutional", "entry", "stop", "stop_loss", "target",
                       "take_profit", "lots", "risk", "signal", "approved", "order_flow"):
            self.assertFalse([f for f in fields if banned in f], banned)
        res = ob(rows(A))
        self.assertIn("no volume or order-flow", res.note)
        self.assertIn("not evidence of institutional activity", res.note)

    def test_the_config_is_validated(self):
        for bad in ({"invalidation_mode": "touch"}, {"invalidation_mode": 1}, {"max_leg_candles": 0}, {"max_leg_candles": -3},
                    {"max_leg_candles": 2.5}, {"max_leg_candles": True}, {"max_leg_candles": "3"}):
            with self.assertRaises(ValueError, msg=str(bad)):
                analyze_order_blocks(rows(A), structure(rows(A)), OrderBlockConfig(**bad))
        self.assertEqual(OrderBlockConfig().to_dict(), {"invalidation_mode": "close", "max_leg_candles": None})
        with self.assertRaises(ValueError):
            analyze_order_blocks(rows(A), {"swings": []})                       # the structure argument must be a StructureResult


class DataHandling(unittest.TestCase):
    def test_insufficient_history_is_reported_not_raised(self):
        for n in (0, 1, 2):
            res = ob(rows(A[:n]))
            self.assertEqual((res.blocks, res.data_status), ((), DATA_INSUFFICIENT), n)
            self.assertIn("Need at least", res.reason)
        self.assertEqual(analyze_order_blocks(None, structure([])).data_status, DATA_INSUFFICIENT)
        self.assertEqual(ob(rows(A[:3])).data_status, DATA_OK)                  # 3 candles is exactly enough for swing_left = swing_right = 1
        wide = ob(rows(A[:6]), left=3, right=3)                                  # 6 candles < 3 + 3 + 1
        self.assertEqual(wide.data_status, DATA_INSUFFICIENT)

    def test_enough_candles_but_no_swing_is_ok_with_no_blocks(self):
        flat = [(10, 10.5, 9.5, 10)] * 9
        res = ob(rows(flat))
        self.assertEqual((res.data_status, res.blocks), (DATA_OK, ()))

    def test_malformed_candles_block_detection_and_are_reported(self):
        for bad in (None, "x", True, float("nan"), float("inf"), float("-inf")):
            c = rows(A)
            c[3][2] = bad
            res = ob(c)
            self.assertEqual((res.data_status, res.blocks), (DATA_INVALID, ()), repr(bad))
            self.assertEqual(res.diagnostics.invalid[0]["index"], 3)
            self.assertIn("impossible or missing", res.reason)

    def test_incorrect_boundary_values_are_rejected(self):
        c = rows(A)
        c[4][2], c[4][3] = 9.8, 10.6                                           # high below low
        self.assertEqual(ob(c).data_status, DATA_INVALID)
        for col in (1, 4):
            c = rows(A)
            c[4][col] = 99                                                      # open / close outside the range
            self.assertEqual(ob(c).data_status, DATA_INVALID, col)

    def test_zero_and_negative_prices_are_rejected(self):
        for bad in (0, -1):
            c = rows(A)
            c[0][3] = bad
            res = ob(c)
            self.assertEqual((res.data_status, res.blocks, res.non_positive_price_times), (DATA_INVALID, (), (t(0),)))
            self.assertIn("zero or below", res.reason)

    def test_wrong_shaped_rows_never_crash(self):
        for junk in ([t(9), 1, 2, 3], "candle", 42, {"time": t(9)}, object()):
            self.assertEqual(ob(rows(A) + [junk]).data_status, DATA_INVALID, repr(junk))

    def test_an_invalid_candle_never_bridges_its_neighbours(self):
        c = rows(A)
        c[4] = [t(4), 0, 0, 0, 0]
        self.assertEqual((ob(c).data_status, ob(c).blocks), (DATA_INVALID, ()))

    def test_conflicting_duplicate_timestamps_block_detection_identical_ones_do_not(self):
        c = rows(A)
        dup = list(c[4])
        dup[3] = 9.75
        res = ob(c + [dup])
        self.assertEqual((res.data_status, res.diagnostics.duplicates_conflicting), (DATA_INVALID, 1))
        self.assertIn("conflicting", res.reason)
        same = ob(c + [list(c[4])])
        self.assertEqual((same.data_status, one(same).status, same.diagnostics.duplicates_removed), (DATA_OK, CONFIRMED, 1))

    def test_invalid_data_takes_precedence_over_insufficient(self):
        self.assertEqual(ob([[t(0), 1, float("nan"), 1, 1]]).data_status, DATA_INVALID)

    def test_a_structure_result_from_other_candles_is_refused(self):
        other = structure(rows(mirror(A)))                                       # same length, different prices
        res = analyze_order_blocks(rows(A), other)
        self.assertEqual((res.data_status, res.blocks), (DATA_INCONSISTENT, ()))
        self.assertIn("does not match", res.reason)
        short = analyze_order_blocks(rows(A), structure(rows(A[:7])))
        self.assertEqual((short.data_status, short.blocks), (DATA_INCONSISTENT, ()))
        self.assertIn("used 7 candles", short.reason)
        shifted = analyze_order_blocks(rows(A), structure(rows(A, start=50)))
        self.assertEqual(shifted.data_status, DATA_INCONSISTENT)

    def test_input_is_not_modified(self):
        c = rows(A)
        before = json.dumps(c)
        ob(c)
        self.assertEqual(json.dumps(c), before)


class OrderingAndRepeatedAnalysis(unittest.TestCase):
    SERIES = rows(C)

    def test_shuffled_input_gives_the_same_blocks_as_ordered_input(self):
        want = ob(self.SERIES)
        for seed in range(8):
            shuffled = list(self.SERIES)
            random.Random(seed).shuffle(shuffled)
            got = ob(shuffled)
            self.assertEqual(got.blocks, want.blocks, seed)
            if shuffled != self.SERIES:
                self.assertTrue(got.diagnostics.out_of_order)

    def test_newest_first_input_does_not_flip_the_direction(self):
        res = ob(list(reversed(rows(A))))
        b = one(res)
        self.assertEqual((b.direction, b.status, b.lower, b.upper), (BULLISH, CONFIRMED, 9.8, 10.6))
        self.assertTrue(res.diagnostics.out_of_order)
        self.assertFalse(ob(rows(A)).diagnostics.out_of_order)

    def test_the_same_candles_twice_give_the_identical_result_and_unique_ids(self):
        a, b = ob(self.SERIES), ob(self.SERIES)
        self.assertEqual(a, b)
        self.assertEqual(len({x.id for x in a.blocks}), len(a.blocks))

    def test_the_same_rows_supplied_twice_do_not_double_the_blocks(self):
        once = ob(self.SERIES)
        twice = ob(self.SERIES + self.SERIES)
        self.assertEqual(twice.blocks, once.blocks)
        self.assertEqual((twice.diagnostics.duplicates_removed, twice.diagnostics.duplicates_conflicting), (len(self.SERIES), 0))

    def test_a_growing_series_never_duplicates_and_only_moves_forward(self):
        order = {CANDIDATE: 0, CONFIRMED: 1, INVALIDATED: 2, EXPIRED: 2}
        seen = {}
        for n in range(3, len(self.SERIES) + 1):
            res = ob(self.SERIES[:n])
            ids = [b.id for b in res.blocks]
            self.assertEqual(len(ids), len(set(ids)), n)
            for b in res.blocks:
                if b.id in seen:
                    self.assertGreaterEqual(order[b.status], order[seen[b.id].status], (n, b.id))    # never un-confirmed or un-expired
                    self.assertEqual(b.source_time, seen[b.id].source_time)
                seen[b.id] = b
        self.assertEqual(set(seen), {b.id for b in ob(self.SERIES).blocks})

    def test_revision_changes_when_a_candle_is_corrected(self):
        c = rows(A)
        fixed = [list(r) for r in c]
        fixed[4][3] = 9.75
        self.assertNotEqual(ob(c).revision, ob(fixed).revision)
        self.assertEqual(ob(c).revision, ob(c).revision)
        self.assertEqual(one(ob(fixed)).lower, 9.75)                             # recomputed from the corrected candle


def random_candles(rng, n, start=100.0):
    out, price = [], start
    for i in range(n):
        o = price
        c = round(max(5.0, o + rng.gauss(0, 1.2) + (0.3 if (i // 25) % 2 == 0 else -0.3)), 2)
        h = round(max(o, c) + abs(rng.gauss(0, 0.5)), 2)
        l = round(max(0.5, min(o, c) - abs(rng.gauss(0, 0.5))), 2)
        h, l = max(h, o, c), min(l, o, c)
        out.append([t(i), o, h, l, c])
        price = c
    return out


def mirror_rows(candles, k=300.0):
    return [[r[0], round(k - r[1], 2), round(k - r[3], 2), round(k - r[2], 2), round(k - r[4], 2)] for r in candles]


class IndependentInvariants(unittest.TestCase):
    """Properties recomputed from the raw candles on many random series, so the engine is not only compared with itself."""

    SETTINGS = [(1, 1), (2, 2), (3, 2), (2, 4)]

    def run_cases(self):
        for seed in range(14):
            for left, right in self.SETTINGS:
                for mode in ("close", "wick"):
                    c = random_candles(random.Random(seed * 7 + left), 140)
                    yield seed, left, right, mode, c

    def test_every_block_satisfies_the_written_rules_when_checked_from_the_raw_candles(self):
        counts = {CANDIDATE: 0, CONFIRMED: 0, INVALIDATED: 0, EXPIRED: 0}
        for seed, left, right, mode, c in self.run_cases():
            cfg = OrderBlockConfig(invalidation_mode=mode)
            s = structure(c, left, right, True, "close")
            res = analyze_order_blocks(c, s, cfg)
            self.assertEqual(res.data_status, DATA_OK)
            idx = {r[0]: i for i, r in enumerate(c)}
            ids = [b.id for b in res.blocks]
            self.assertEqual(len(ids), len(set(ids)))
            events = {(e.direction, e.reference_pivot_time, e.candle_time): e for e in s.events}
            for b in res.blocks:
                counts[b.status] += 1
                tag = (seed, left, right, mode, b.id)
                src = c[idx[b.source_time]]
                bull = b.direction == BULLISH
                self.assertEqual((b.upper, b.lower), (src[2], src[3]), tag)                 # zone = the source candle's range
                self.assertGreater(b.upper, b.lower)
                self.assertTrue(src[4] < src[1] if bull else src[4] > src[1], tag)          # opposite-coloured source
                self.assertEqual(b.id, f"OB-{'BULL' if bull else 'BEAR'}-{b.source_time:.0f}")
                self.assertGreaterEqual(b.origin_time, b.source_time)
                self.assertGreater(b.identified_time, b.source_time)
                if b.confirmed_time is None:
                    self.assertGreater(b.identified_time, b.origin_time)               # known only after its swing was confirmed
                else:
                    self.assertGreater(b.confirmed_time, b.origin_time)                # confirmed only after the leg's origin swing was knowable
                # price violation recomputed independently
                violated = next((r[0] for r in c[idx[b.source_time] + 1:]
                                 if ((r[3] if mode == "wick" else r[4]) < b.lower if bull else (r[2] if mode == "wick" else r[4]) > b.upper)), None)
                if b.status == INVALIDATED:
                    self.assertEqual(b.invalidated_time, violated, tag)
                    self.assertIsNone(b.expired_time)
                if b.confirmed_time is not None:
                    ev = events[(b.direction, b.reference_time, b.confirming_event_time)]
                    self.assertEqual(ev.type, b.confirming_event_type, tag)
                    self.assertFalse(ev.same_candle_conflict)
                    self.assertGreaterEqual(b.confirmed_time, ev.candle_time)
                    self.assertGreaterEqual(b.confirmed_time, b.identified_time)
                    self.assertTrue(violated is None or violated > b.confirmed_time, tag)   # never confirmed at / after a violation
                    # the origin of the leg is the extreme between the reference swing and the break (earliest if equal)
                    lo, hi = idx[b.reference_time] + 1, idx[ev.candle_time]
                    window = c[lo:hi]
                    if bull:
                        origin = min(window, key=lambda r: (r[3], r[0]))
                    else:
                        origin = max(window, key=lambda r: (r[2], -r[0]))
                    self.assertEqual(origin[0], b.origin_time, tag)
                    self.assertEqual(b.reference_level, c[idx[b.reference_time]][2 if bull else 3], tag)    # the broken level is that swing's high / low
                else:
                    self.assertEqual((b.confirming_event_id, b.confirming_event_time), (None, None))
                if b.status == EXPIRED:
                    self.assertIn(b.expired_reason, (REFERENCE_LOST, NOT_QUALIFIED))
                    self.assertTrue(violated is None or violated > b.expired_time, tag)
                else:
                    self.assertIsNone(b.expired_reason)
                self.assertFalse(b.provisional)                                          # all candles closed here
        for status, n in counts.items():                                                 # the property run exercised every state
            self.assertGreater(n, 5, (status, counts))

    def test_prefix_consistency_holds_on_random_series(self):
        tester = ConfirmationTimingAndLookAhead()
        for seed in range(6):
            c = random_candles(random.Random(100 + seed), 90)
            for left, right in ((1, 1), (2, 2), (2, 3)):
                for mode in ("close", "wick"):
                    tester.assert_prefix_consistent(c, list(range(left + right + 1, len(c) + 1, 7)) + [len(c)], left, right, "close",
                                                    OrderBlockConfig(invalidation_mode=mode))

    def test_bullish_and_bearish_are_exact_mirrors_on_random_series(self):
        compared = 0
        for seed in range(12):
            c = random_candles(random.Random(300 + seed), 120)
            m = mirror_rows(c)
            for left, right in ((1, 1), (2, 2), (3, 1)):
                a = ob(c, left, right)
                b = ob(m, left, right)
                self.assertEqual(len(a.blocks), len(b.blocks), (seed, left, right))
                for x, y in zip(a.blocks, b.blocks):
                    self.assertEqual(x.direction, BULLISH if y.direction == BEARISH else BEARISH)
                    for k in ("status", "source_time", "identified_time", "confirmed_time", "invalidated_time", "expired_time", "expired_reason",
                              "origin_time", "reference_time", "confirming_event_type", "source_index"):
                        self.assertEqual(getattr(x, k), getattr(y, k), (seed, left, right, x.id, k))
                    self.assertEqual((round(300 - x.upper, 2), round(300 - x.lower, 2)), (y.lower, y.upper))
                    compared += 1
        self.assertGreater(compared, 50)

    def test_input_order_never_matters_on_random_series(self):
        for seed in range(5):
            c = random_candles(random.Random(900 + seed), 100)
            want = ob(c, 2, 2).blocks
            shuffled = list(c)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(ob(shuffled, 2, 2).blocks, want)


class IsolationAndScope(unittest.TestCase):
    def test_the_engine_imports_nothing_about_trading_brokers_or_the_web(self):
        root = Path(__file__).resolve().parent.parent / "trademate" / "orderblocks"
        found = set()
        for path in root.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module:
                    found.add(node.module)
                elif isinstance(node, ast.Import):
                    found.update(a.name for a in node.names)
        for mod in found:
            self.assertNotIn(mod.split(".")[0], {"flask", "app", "requests", "sqlite3", "socket"}, mod)
            self.assertNotRegex(mod, r"broker|risk|execution_gate|journal|pending|app$|fvg|liquidity")

    def test_only_the_analysis_and_api_layers_use_the_engine_never_trading_code(self):
        """Replaces the Task 3 "no integration yet" guard (the chart and Analysis integration now exist, Phase 7C repair). What still matters:
        the engine is read-only analysis, referenced only by the analysis view and the chart API, never by broker, risk or execution code. Phase 8 added trademate/strategy/mtf.py (read-only analysis that proposes setups;
        tests.test_mtf_strategy.Boundaries proves that package never touches broker order, risk or execution code)."""
        root = Path(__file__).resolve().parent.parent
        users = sorted(str(p.relative_to(root)) for p in list((root / "trademate").rglob("*.py")) + [root / "app.py"]
                       if "orderblocks" in p.read_text(encoding="utf-8") and "orderblocks" not in p.parts)
        self.assertEqual(users, ["app.py", "trademate/analysis.py", "trademate/strategy/mtf.py"])      # Phase 8: the read-only multi-timeframe layer

    def test_the_existing_structure_liquidity_and_fvg_engines_are_unchanged_by_importing_it(self):
        from trademate.fvg import analyze_fvg
        from trademate.liquidity import analyze_liquidity
        self.assertTrue(callable(analyze_fvg) and callable(analyze_liquidity))
        self.assertEqual(analyze_fvg(rows(A)).data_status, DATA_OK)


if __name__ == "__main__":
    unittest.main()
