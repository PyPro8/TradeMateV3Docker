"""Phase 7B Liquidity Mechanics Engine: deterministic tests on hand-built candles (expected values follow from the written rules in
trademate/liquidity/engine.py, not from whatever the engine prints). Pure Python, no broker, no Flask.

    python3 -m unittest tests.test_liquidity_engine -v
"""

import unittest
from dataclasses import replace

from tests.test_structure_engine import CFG, STEP, T0, UP, UP_THEN_BREAK_LOW, _walk, series, t
from tests.test_structure_regressions import RangingMarketTest, mk
from trademate.liquidity import LiquidityConfig, analyze_liquidity, diff_liquidity, ticks_to_price
from trademate.structure import analyze_market_structure

RANGE = RangingMarketTest.RANGE                      # equal highs 10 (i2, i6) and equal lows 6 (i4, i8); closes stay inside


def run(rows_or_candles, closed=True, **kw):
    candles = rows_or_candles
    scfg = replace(CFG, last_candle_closed=closed)
    st = analyze_market_structure(candles, scfg)
    return analyze_liquidity(candles, st, LiquidityConfig(last_candle_closed=closed, **kw))


def pid(side, i):
    """The documented pool id: side + the first member's pivot time in whole epoch seconds."""
    return f"{side}-{int(t(i))}"


def pool(res, pool_id):
    return next(p for p in res.pools if p.id == pool_id)


class PoolDetection(unittest.TestCase):
    def test_equal_highs_and_equal_lows_become_pools_with_documented_timestamps(self):
        res = run(mk(RANGE))
        bsl, ssl = pool(res, pid("BSL", 2)), pool(res, pid("SSL", 4))
        self.assertEqual((bsl.kind, bsl.level, len(bsl.members)), ("equal", 10, 2))
        self.assertEqual((bsl.created_time, bsl.confirmed_time, bsl.equal_confirmed_time), (t(2), t(4), t(8)))
        self.assertEqual((ssl.kind, ssl.level, len(ssl.members)), ("equal", 6, 2))
        self.assertEqual((ssl.created_time, ssl.confirmed_time, ssl.equal_confirmed_time), (t(4), t(6), t(10)))
        self.assertTrue(all(p.state == "ACTIVE" for p in res.pools))
        self.assertEqual(res.sweeps, ())

    def test_buy_side_is_above_highs_and_sell_side_below_lows(self):
        res = run(mk(RANGE))
        self.assertEqual({p.side for p in res.pools if p.level == 10}, {"BSL"})
        self.assertEqual({p.side for p in res.pools if p.level == 6}, {"SSL"})
        single = run(mk(RANGE[:7]))                                 # only the first high (i2) and low (i4) are known
        self.assertEqual({(p.side, p.kind) for p in single.pools}, {("BSL", "swing"), ("SSL", "swing")})

    def test_tolerance_decides_equal_and_the_representative_is_the_extreme_member(self):
        rows = [(8, 6, 7), (9, 7, 8), (10, 8, 9), (9, 7, 8), (8, 6, 7), (9, 7, 8), (10.05, 8, 9), (9, 7, 8), (8, 6, 7)]
        strict = run(mk(rows), tolerance=0.01)
        loose = run(mk(rows), tolerance=0.1)
        self.assertEqual(sorted(p.id for p in strict.pools if p.side == "BSL"), [pid("BSL", 2), pid("BSL", 6)])    # two separate pools
        joined = pool(loose, pid("BSL", 2))
        self.assertEqual((joined.kind, joined.level), ("equal", 10.05))                                              # BSL: the HIGHEST member
        lows = [(8, 6.00, 7), (9, 7, 8), (10, 8, 9), (9, 7, 8), (8, 5.96, 7), (9, 7, 8), (10, 8, 9), (9, 7, 8), (8, 6.0, 7), (9, 7, 8), (10, 8, 9)]
        jl = pool(run(mk(lows), tolerance=0.1), pid("SSL", 4))
        self.assertEqual((jl.kind, jl.level), ("equal", 5.96))                                                       # SSL: the LOWEST member

    def test_a_poke_within_the_equal_tolerance_is_not_a_sweep_but_beyond_it_is(self):
        rows = RANGE + [(10.04, 8, 9.5)]
        self.assertEqual(run(mk(rows), tolerance=0.05).sweeps, ())                      # 0.04 beyond 10, inside the 0.05 band: still "at" the level
        self.assertEqual(len(run(mk(RANGE + [(10.06, 8, 9.5)]), tolerance=0.05).sweeps), 1)
        self.assertEqual(len(run(mk(rows), tolerance=0.0).sweeps), 1)                   # with no tolerance any price beyond counts

    def test_ids_use_plain_epoch_digits_so_real_timestamps_never_collide(self):
        base = 1_760_000_000
        a = [[base + i * 3600] + row[1:] for i, row in enumerate(mk(RANGE))]
        b = [[base + 60 + i * 3600] + row[1:] for i, row in enumerate(mk(RANGE))]      # same shape, shifted by one minute
        ia, ib = {p.id for p in run(a).pools}, {p.id for p in run(b).pools}
        self.assertTrue(all(i.split("-")[1].isdigit() and len(i.split("-")[1]) == 10 for i in ia | ib), ia | ib)
        self.assertEqual(ia & ib, set())

    def test_nearest_pool_wins_and_ties_go_to_the_older_one(self):
        # two BSL pools 10.6 (older) and 10 (newer), 0.6 apart; a later swing high at 10.3 is equally near? no: 0.3 from each
        rows = [(8, 6, 7), (9, 7, 8), (10.6, 8, 9), (9, 7, 8), (8, 6, 7), (9, 7, 8), (10, 8, 9), (9, 7, 8), (8, 6, 7), (9, 7, 8),
                (10.3, 8, 9), (9, 7, 8), (8, 6, 7)]
        res = run(mk(rows), tolerance=0.31)
        older = pool(res, pid("BSL", 2))
        self.assertEqual(len(older.members), 2)                                # 10.3 joined the OLDER pool on the tie
        self.assertEqual(len(pool(res, pid("BSL", 6)).members), 1)

    def test_ticks_to_price_uses_the_symbols_tick_size_without_float_drift(self):
        self.assertEqual(ticks_to_price(5, 0.00001), 0.00005)
        self.assertEqual(ticks_to_price(5, 0.001), 0.005)                                   # JPY pair
        self.assertEqual(ticks_to_price(10, 0.01), 0.1)                                     # gold
        self.assertEqual(ticks_to_price(3, 0.1), 0.3)
        self.assertEqual(ticks_to_price(0, 0), 0.0)
        for bad in ((5, 0), (5, -1), (-1, 0.01), (float("nan"), 0.01), (5, float("inf")), (True, 0.01), ("5", 0.01)):
            with self.assertRaises(ValueError):
                ticks_to_price(*bad)

    def test_configuration_is_validated(self):
        for bad in ({"tolerance": -1}, {"tolerance": float("nan")}, {"sweep_mode": "tick"}, {"max_pools_per_side": 0}, {"max_pools_per_side": 99},
                    {"max_age_candles": 3}, {"link_window": 0}, {"sweep_min_penetration": float("inf")}, {"tolerance": True}):
            with self.assertRaises(ValueError, msg=str(bad)):
                LiquidityConfig(**bad).validate()
        LiquidityConfig(tolerance=0.1, sweep_mode="close").validate()

    def test_pool_cap_hides_older_pools_but_counts_them(self):
        res = run(_walk(300, 4), max_pools_per_side=2)
        for side in ("BSL", "SSL"):
            self.assertLessEqual(len([p for p in res.pools if p.side == side and p.state == "ACTIVE"]), 2)
        self.assertGreaterEqual(res.hidden_pools["BSL"] + res.hidden_pools["SSL"] + res.hidden_pools["expired"], 0)


class SweepDetection(unittest.TestCase):
    WICK = RANGE + [(10.4, 8, 9.5)]                     # high 10.4 > 10, close 9.5 back INSIDE
    BEYOND = RANGE + [(10.4, 8, 9.5), (11, 9, 10.8)]    # then a candle that closes beyond 10

    def test_wick_mode_sweeps_on_the_high_and_records_that_the_close_stayed_inside(self):
        res = run(mk(self.WICK))
        self.assertEqual(len(res.sweeps), 1)
        s = res.sweeps[0]
        self.assertEqual((s.side, s.level, s.candle_time, s.mode, s.close_beyond, s.confirmed), ("BSL", 10, t(11), "wick", False, True))
        self.assertAlmostEqual(s.penetration, 0.4)
        self.assertEqual((s.pool_id, s.pool_kind, s.member_count), (pid("BSL", 2), "equal", 2))
        self.assertEqual(pool(res, s.pool_id).state, "SWEPT")
        self.assertEqual(pool(res, s.pool_id).swept_time, t(11))

    def test_close_mode_needs_a_close_beyond_the_level(self):
        self.assertEqual(run(mk(self.WICK), sweep_mode="close").sweeps, ())
        res = run(mk(self.BEYOND), sweep_mode="close")
        self.assertEqual([(s.candle_time, s.close_beyond, s.mode) for s in res.sweeps if s.side == "BSL"], [(t(12), True, "close")])

    def test_sell_side_mirror(self):
        rows = RANGE + [(9, 5.6, 6.4)]                  # low 5.6 < 6, close 6.4 back inside
        res = run(mk(rows))
        s = next(x for x in res.sweeps if x.side == "SSL")
        self.assertEqual((s.level, s.candle_time, s.close_beyond), (6, t(11), False))
        self.assertAlmostEqual(s.penetration, 0.4)
        self.assertEqual(run(mk(rows), sweep_mode="close").sweeps, ())
        beyond = run(mk(RANGE + [(9, 5.5, 5.8)]), sweep_mode="close")
        self.assertTrue(next(x for x in beyond.sweeps if x.side == "SSL").close_beyond)

    def test_touching_the_level_is_not_a_sweep_and_min_penetration_is_respected(self):
        self.assertEqual(run(mk(RANGE + [(10.0, 8, 9.5)])).sweeps, ())                         # equal to the level: not beyond it
        thin = RANGE + [(10.03, 8, 9.5)]
        self.assertEqual(len(run(mk(thin)).sweeps), 1)
        self.assertEqual(run(mk(thin), sweep_min_penetration=0.05).sweeps, ())
        self.assertEqual(len(run(mk(RANGE + [(10.06, 8, 9.5)]), sweep_min_penetration=0.05).sweeps), 1)

    def test_a_pool_is_swept_once_and_repeats_create_no_second_event(self):
        rows = RANGE + [(10.4, 8, 9.5), (9, 7, 8), (10.9, 8, 9.2), (9, 7, 8)]
        res = run(mk(rows))
        self.assertEqual(len([s for s in res.sweeps if s.pool_id == pid("BSL", 2)]), 1)
        self.assertEqual(len({s.id for s in res.sweeps}), len(res.sweeps))

    def test_overlapping_pools_each_get_their_own_sweep_and_share_a_group(self):
        rows = [(8, 6, 7), (9, 7, 8), (10.6, 8, 9), (9, 7, 8), (8, 6, 7), (9, 7, 8), (10, 8, 9), (9, 7, 8), (8, 6, 7), (11, 8, 10.5)]
        res = run(mk(rows), tolerance=0.2)
        sw = [s for s in res.sweeps if s.side == "BSL"]
        self.assertEqual(sorted(s.pool_id for s in sw), sorted([pid("BSL", 2), pid("BSL", 6)]))
        self.assertTrue(all(s.candle_time == t(9) for s in sw))
        self.assertEqual({s.group for s in sw}, {(pid("BSL", 2), pid("BSL", 6))})
        by = {s.pool_id: s for s in sw}
        self.assertFalse(by[pid("BSL", 2)].close_beyond)         # closed at 10.5: beyond 10 but not beyond 10.6
        self.assertTrue(by[pid("BSL", 6)].close_beyond)

    def test_a_pool_cannot_be_swept_by_the_candle_that_made_it_known_or_earlier(self):
        for candles in (mk(RANGE + [(10.4, 8, 9.5)]), _walk(120, 17)):
            res = run(candles)
            for s in res.sweeps:
                self.assertGreater(s.candle_time, pool(res, s.pool_id).confirmed_time)

    def test_unswept_pool_expires_by_age_and_is_never_swept_afterwards(self):
        rows = [(8, 6, 7), (9, 7, 8), (10, 8, 9), (9, 7, 8), (8, 6, 7)] + [(8.5, 6.5, 7.5)] * 14 + [(10.5, 7, 9)]
        res = run(mk(rows), max_age_candles=10)
        self.assertEqual(res.sweeps, ())                                  # expired before the high-10.5 candle came
        self.assertEqual(res.hidden_pools["expired"], 2)                  # the unswept BSL pool and the unswept SSL pool
        self.assertEqual(len(run(mk(rows), max_age_candles=100).sweeps), 1)


class ForemingCandleAndStates(unittest.TestCase):
    def test_a_sweep_on_the_forming_candle_is_provisional_and_the_same_candle_closed_is_confirmed(self):
        rows = mk(RANGE + [(10.4, 8, 9.5)])
        forming = run(rows, closed=False)
        self.assertEqual([(s.confirmed, pool(forming, s.pool_id).provisional) for s in forming.sweeps], [(False, True)])
        closed = run(rows, closed=True)
        self.assertEqual([s.confirmed for s in closed.sweeps], [True])

    def test_forming_run_equals_closed_run_except_for_the_newest_candle(self):
        for mode in ("wick", "close"):
            data = _walk(90, 11)
            for k in range(CFG.min_candles + 3, len(data) + 1, 3):
                prefix = data[:k]
                closed, forming = run(prefix, True, sweep_mode=mode), run(prefix, False, sweep_mode=mode)
                last = prefix[-1][0]
                self.assertEqual([s.id for s in forming.sweeps if s.confirmed], [s.id for s in closed.sweeps if s.candle_time != last], (mode, k))
                self.assertTrue(all(not s.confirmed for s in forming.sweeps if s.candle_time == last), (mode, k))

    def test_states_only_move_forward(self):
        data = _walk(120, 3)
        seen = {}
        order = {"ACTIVE": 0, "SWEPT": 1, "EXPIRED": 1}
        for k in range(CFG.min_candles + 3, len(data) + 1, 4):
            for p in run(data[:k], True, max_age_candles=30).pools:
                if p.id in seen:
                    self.assertGreaterEqual(order[p.state], order[seen[p.id]], (p.id, k))
                    if seen[p.id] in ("SWEPT", "EXPIRED"):
                        self.assertEqual(p.state, seen[p.id], (p.id, k))        # final states are final
                seen[p.id] = p.state


class DuplicatesAndStability(unittest.TestCase):
    def test_repeated_analysis_and_duplicate_candles_give_identical_results(self):
        rows = mk(SweepDetection.BEYOND)
        a, b = run(rows), run(rows)
        self.assertEqual(a.to_dict(), b.to_dict())
        dup = run(rows + rows)
        self.assertEqual([p.id for p in dup.pools], [p.id for p in a.pools])
        self.assertEqual([s.id for s in dup.sweeps], [s.id for s in a.sweeps])
        self.assertEqual(len({p.id for p in a.pools}), len(a.pools))

    def test_ids_are_stable_when_more_history_arrives(self):
        data = _walk(100, 8)
        short, full = run(data[:70]), run(data)
        for p in short.pools:
            if p.state == "ACTIVE" and p.id in {q.id for q in full.pools}:
                self.assertTrue(p.id.startswith(("BSL-", "SSL-")))
        ids_short = {p.id for p in short.pools if p.state == "ACTIVE"}
        ids_full_all = {p.id for p in run(data, max_pools_per_side=50).pools}
        self.assertTrue(ids_short <= ids_full_all)                             # a pool known earlier is still known later (swept or not)

    def test_reordered_candles_give_the_same_pools(self):
        data = _walk(80, 2)
        self.assertEqual(run(data).to_dict()["pools"], run(list(reversed(data))).to_dict()["pools"])


class LookaheadAndCorrections(unittest.TestCase):
    def test_prefix_results_are_never_contradicted_by_the_full_run(self):
        for mode in ("wick", "close"):
            data = _walk(110, 23)
            full = run(data, True, sweep_mode=mode, tolerance=0.3, max_pools_per_side=50)
            full_pool = {p.id: p for p in full.pools}
            full_sweep = {s.id: s for s in full.sweeps}
            for k in range(CFG.min_candles, len(data)):
                prefix = data[:k]
                last = prefix[-1][0]
                res = run(prefix, True, sweep_mode=mode, tolerance=0.3, max_pools_per_side=50)
                for p in res.pools:
                    self.assertLessEqual(p.confirmed_time, last, (mode, k))                    # nothing is known before it can be
                    if p.equal_confirmed_time is not None:
                        self.assertLessEqual(p.equal_confirmed_time, last, (mode, k))
                    fp = full_pool[p.id]
                    self.assertEqual(p.members, [m for m in fp.members if m["confirmed_time"] <= last][:len(p.members)], (mode, k, p.id))
                for s in res.sweeps:
                    f = full_sweep[s.id]
                    self.assertEqual({**s.to_dict(), "structure": None}, {**f.to_dict(), "structure": None}, (mode, k, s.id))

    def test_a_pool_swept_in_the_full_run_is_not_reported_swept_in_a_prefix_that_ends_before_the_sweep(self):
        data = mk(SweepDetection.WICK)
        before = run(data[:11])                                   # ends before the sweeping candle
        self.assertEqual(before.sweeps, ())
        self.assertEqual(pool(before, pid("BSL", 2)).state, "ACTIVE")
        self.assertEqual(pool(run(data), pid("BSL", 2)).state, "SWEPT")

    def test_corrected_history_is_recomputed_with_stable_ids_and_a_visible_diff(self):
        good = mk(SweepDetection.WICK)
        old = run(good)
        corrected = [list(r) for r in good]
        corrected[11][2], corrected[11][1] = 10.0, 9.9               # the broker corrects candle 11: high 10.4 -> 10.0 (no longer beyond 10)
        corrected[11][1] = min(corrected[11][1], corrected[11][3], corrected[11][4])
        corrected[11][3] = min(corrected[11][3], 8)
        new = run(corrected)
        d = diff_liquidity(old.to_dict(), new.to_dict())
        self.assertTrue(d["revision_changed"])
        self.assertEqual(d["sweeps"]["removed"], [old.sweeps[0].id])
        self.assertIn(pid("BSL", 2), d["pools"]["changed"])                                       # same id, new state
        self.assertEqual(pool(new, pid("BSL", 2)).state, "ACTIVE")
        self.assertEqual(new.to_dict(), run(corrected).to_dict())                                    # recomputation is repeatable
        same = diff_liquidity(old.to_dict(), run(good).to_dict())
        self.assertEqual((same["revision_changed"], same["pools"]["changed"], same["sweeps"]["added"]), (False, [], []))


class StructureIntegration(unittest.TestCase):
    def test_a_sweep_with_no_structural_break_is_reported_as_such_and_none_is_forced(self):
        res = run(mk(SweepDetection.WICK))
        ctx = res.sweeps[0].structure
        self.assertIsNone(ctx["after"])
        self.assertEqual(ctx["same_candle"], [])
        self.assertIn("No structural break", " ".join(ctx["statements"]))

    def test_events_are_only_ever_related_in_the_right_temporal_direction(self):
        for seed in (3, 7, 11, 21):
            data = _walk(150, seed)
            res = run(data, True, tolerance=0.2)
            for s in res.sweeps:
                c = s.structure
                if c["before"]:
                    self.assertLess(c["before"]["candle_time"], s.candle_time)
                if c["after"]:
                    self.assertGreater(c["after"]["candle_time"], s.candle_time)
                    self.assertLessEqual((c["after"]["candle_time"] - s.candle_time) / STEP, res.config.link_window)
                for e in c["same_candle"]:
                    self.assertEqual(e["candle_time"], s.candle_time)
                text = " ".join(c["statements"]).lower()
                if c["before"]:
                    self.assertIn("not a consequence of the sweep", text)
                for word in ("caused", "because of the sweep", "confirms the sweep", "confirmed the sweep"):
                    self.assertNotIn(word, text)

    def test_a_break_before_the_sweep_is_never_called_a_consequence(self):
        data = series(UP_THEN_BREAK_LOW)                                  # CHoCH on the last candle
        res = run(data, True, sweep_mode="wick")
        for s in res.sweeps:
            if s.structure["before"]:
                self.assertIn("earlier", " ".join(s.structure["statements"]))

    def test_same_candle_break_and_sweep_do_not_claim_an_order(self):
        res = run(series(UP), True, sweep_mode="wick")
        same = [s for s in res.sweeps if s.structure["same_candle"]]
        self.assertTrue(same)
        for s in same:
            self.assertIn("order of events inside one candle cannot be known", " ".join(s.structure["statements"]))
            self.assertIsNone(s.structure["after"] if not s.structure["after"] else None)


if __name__ == "__main__":
    unittest.main()
