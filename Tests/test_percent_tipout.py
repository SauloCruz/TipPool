"""PERCENT_TIPOUT (La Fontana) engine tests — the §7 fixture list from
docs/M5-la-fontana.md. Conservation asserted on every case."""

import pytest

from engine import ManagerInPoolError, compute_day_percent_tipout, validate_percentages


def conserve(out):
    # daily payouts + the month-carried BOH slice account for every cent
    carried = out.pools["boh"]["carried_cents"]
    assert sum(out.payout_cents.values()) + carried == out.total_tips_cents
    for bucket, info in out.pools.items():
        assert (sum(info["shares"].values()) + info["returned_cents"]
                + info.get("carried_cents", 0)) == info["contributed_cents"]


NORMAL_ROLES = {
    "s1": "SERVER", "s2": "SERVER", "s3": "SERVER",
    "b1": "BUSSER", "b2": "BUSSER",
    "h1": "HOST",
    "k1": "BOH", "k2": "BOH",
}
NORMAL_HOURS = {"s1": 6.0, "s2": 7.5, "s3": 5.25, "b1": 6.0, "b2": 4.0,
                "h1": 5.0, "k1": 8.0, "k2": 8.0}


class TestNormalDay:
    """1 host, 2 bussers, 3 servers, 2 BOH — even splits."""

    def out(self):
        return compute_day_percent_tipout(
            server_tips={"s1": 20000, "s2": 30000, "s3": 10000},
            roles=NORMAL_ROLES, hours=NORMAL_HOURS)

    def test_per_server_keep_65(self):
        out = self.out()
        assert out.keep_cents == {"s1": 13000, "s2": 19500, "s3": 6500}
        assert out.returned_cents == {"s1": 0, "s2": 0, "s3": 0}

    def test_pools_20_10_and_boh_carried(self):
        out = self.out()
        # 20% of 600.00 = 120.00 -> 60.00 each busser
        assert out.pools["busser"]["contributed_cents"] == 12000
        assert out.pools["busser"]["shares"] == {"b1": 6000, "b2": 6000}
        # 10% -> single host gets all 60.00
        assert out.pools["host"]["shares"] == {"h1": 6000}
        # 5% = 30.00 carries to the monthly kitchen payout — no daily shares
        assert out.pools["boh"]["carried_cents"] == 3000
        assert out.pools["boh"]["shares"] == {}
        conserve(out)

    def test_no_flags_on_clean_day(self):
        out = self.out()
        assert not any(out.flags.values())

    def test_payout_composition(self):
        out = self.out()
        assert out.payout_cents["s2"] == 19500          # keep only
        assert out.payout_cents["b1"] == 6000           # pool share only
        assert out.payout_cents["h1"] == 6000
        assert out.payout_cents["k1"] == 0  # kitchen is paid monthly


class TestNoHostResplit:
    """Owner ruling 2026-07-06: the host share goes entirely to the busser
    pool (an extra busser covers host duties) -> effective 65/30/5. Flagged."""

    def out(self, tips=10000):
        roles = {k: v for k, v in NORMAL_ROLES.items() if v != "HOST"}
        return compute_day_percent_tipout(
            server_tips={"s1": tips}, roles=roles, hours=NORMAL_HOURS)

    def test_effective_percentages(self):
        out = self.out()
        assert out.flags["no_host_resplit"]
        assert out.percentages_used["server"] == "65"
        assert out.percentages_used["busser"] == "30"
        assert out.percentages_used["boh"] == "5"
        assert out.percentages_used["host"] == "0"

    def test_cents_exact_on_100(self):
        # $100.00: 65 -> 6500, 30 -> 3000, 5 -> 500
        out = self.out(10000)
        assert out.keep_cents["s1"] == 6500
        assert out.pools["busser"]["contributed_cents"] == 3000
        assert out.pools["boh"]["contributed_cents"] == 500
        conserve(out)

    def test_odd_cents_conserve(self):
        out = self.out(12345)
        conserve(out)
        assert out.keep_cents["s1"] + out.pools["busser"]["contributed_cents"] \
            + out.pools["boh"]["contributed_cents"] == 12345

    def test_flag_gated_by_busser_threshold(self):
        """Owner ruling 2026-07-06 (2nd update): the re-split itself is
        routine (low season runs thin) — only FLAG when busser coverage is
        below the venue threshold."""
        roles = {k: v for k, v in NORMAL_ROLES.items() if v != "HOST"}
        # 2 bussers on the roster; threshold 3 -> flagged
        out = compute_day_percent_tipout(
            server_tips={"s1": 10000}, roles=roles, hours=NORMAL_HOURS,
            no_host_flag_min_bussers=3)
        assert out.flags["no_host_resplit"]        # informational, always on
        assert out.flags["no_host_low_bussers"]    # 2 < 3
        # threshold 2 -> enough bussers, not flagged
        out = compute_day_percent_tipout(
            server_tips={"s1": 10000}, roles=roles, hours=NORMAL_HOURS,
            no_host_flag_min_bussers=2)
        assert out.flags["no_host_resplit"]
        assert not out.flags["no_host_low_bussers"]
        # default 0 -> never flags
        out = self.out()
        assert not out.flags["no_host_low_bussers"]

    def test_server_keep_unchanged_on_no_host_day(self):
        # servers still keep exactly 65% — the re-split touches bussers only
        out = self.out(10000)
        assert out.keep_cents["s1"] == 6500
        assert out.returned_cents["s1"] == 0


class TestEmptyPoolReturns:
    def test_no_bussers_pro_rata_return(self):
        roles = {"s1": "SERVER", "s2": "SERVER", "h1": "HOST", "k1": "BOH"}
        out = compute_day_percent_tipout(
            server_tips={"s1": 20000, "s2": 10000}, roles=roles,
            hours={"s1": 6, "s2": 6, "h1": 5, "k1": 8})
        assert out.flags["busser_pool_returned_to_servers"]
        # each server gets exactly their own 20% back
        assert out.returned_cents == {"s1": 4000, "s2": 2000}
        assert out.payout_cents["s1"] == 13000 + 4000
        conserve(out)

    def test_boh_pool_carries_even_with_no_boh_on_roster(self):
        # ruling 2026-07-06: the BOH slice NEVER returns to servers — it
        # accumulates for the month-end kitchen payout regardless of who
        # (if anyone) appears on the daily roster
        roles = {"s1": "SERVER", "b1": "BUSSER", "h1": "HOST"}
        out = compute_day_percent_tipout(
            server_tips={"s1": 10000}, roles=roles, hours={"s1": 6, "b1": 6, "h1": 5})
        assert out.pools["boh"]["carried_cents"] == 500
        assert out.returned_cents["s1"] == 0
        assert "boh_pool_returned_to_servers" not in out.flags
        conserve(out)

    def test_cascade_no_host_then_no_busser(self):
        """Cascade order: re-split first (host 10% -> busser pool), then the
        busser pool (now 30%) returns because no bussers worked either."""
        roles = {"s1": "SERVER", "k1": "BOH"}
        out = compute_day_percent_tipout(
            server_tips={"s1": 10000}, roles=roles, hours={"s1": 6, "k1": 8})
        assert out.flags["no_host_resplit"]
        assert out.flags["busser_pool_returned_to_servers"]
        # keep 65% + returned 30% = 95%; BOH 5% carries to month-end
        assert out.keep_cents["s1"] == 6500
        assert out.returned_cents["s1"] == 3000
        assert out.payout_cents["s1"] == 9500
        assert out.pools["boh"]["carried_cents"] == 500
        conserve(out)


class TestRoundingAndEdges:
    def test_single_busser_gets_whole_pool(self):
        roles = {"s1": "SERVER", "b1": "BUSSER", "h1": "HOST", "k1": "BOH"}
        out = compute_day_percent_tipout(
            server_tips={"s1": 33333}, roles=roles,
            hours={"s1": 6, "b1": 6, "h1": 5, "k1": 8})
        assert out.pools["busser"]["shares"]["b1"] == out.pools["busser"]["contributed_cents"]
        conserve(out)

    def test_zero_tip_server_participates_without_contributing(self):
        out = compute_day_percent_tipout(
            server_tips={"s1": 10000, "s2": 0}, roles=NORMAL_ROLES, hours=NORMAL_HOURS)
        assert out.keep_cents["s2"] == 0
        assert out.payout_cents["s2"] == 0
        conserve(out)

    def test_residual_cents_across_even_split(self):
        # 20% of $1.01 = 20.2c busser pool... use awkward totals
        roles = {"s1": "SERVER", "b1": "BUSSER", "b2": "BUSSER", "b3": "BUSSER",
                 "h1": "HOST", "k1": "BOH"}
        out = compute_day_percent_tipout(
            server_tips={"s1": 10001}, roles=roles,
            hours={"s1": 6, "b1": 6, "b2": 6, "b3": 6, "h1": 5, "k1": 8})
        pool = out.pools["busser"]
        assert sum(pool["shares"].values()) == pool["contributed_cents"]
        assert max(pool["shares"].values()) - min(pool["shares"].values()) <= 1
        conserve(out)

    def test_per_server_split_conserves_on_awkward_tips(self):
        for cents in [1, 3, 99, 12347, 55555]:
            out = compute_day_percent_tipout(
                server_tips={"s1": cents}, roles=NORMAL_ROLES, hours=NORMAL_HOURS)
            conserve(out)

    def test_declared_cash_added_to_server_tips(self):
        out = compute_day_percent_tipout(
            server_tips={"s1": 10000, "s2": 0, "s3": 0},
            server_cash_tips={"s1": 2000},
            roles=NORMAL_ROLES, hours=NORMAL_HOURS)
        assert out.total_tips_cents == 12000
        assert out.keep_cents["s1"] == 7800  # 65% of 120.00
        conserve(out)

    def test_no_tips_day(self):
        out = compute_day_percent_tipout(server_tips={}, roles=NORMAL_ROLES,
                                         hours=NORMAL_HOURS)
        assert out.total_tips_cents == 0
        assert not any(out.flags.values())
        conserve(out)


class TestGratuity:
    def test_even_split_front_of_house_only(self):
        """Owner ruling 2026-07-06: LF doesn't track hours — gratuity splits
        evenly among front-of-house who worked. BOH never shares."""
        roles = {"s1": "SERVER", "b1": "BUSSER", "h1": "HOST", "k1": "BOH"}
        out = compute_day_percent_tipout(
            server_tips={"s1": 0}, auto_gratuity_cents=9000, roles=roles,
            hours={"s1": 5, "b1": 3, "h1": 2, "k1": 8})
        assert out.gratuity_cents["s1"] == 3000
        assert out.gratuity_cents["b1"] == 3000
        assert out.gratuity_cents["h1"] == 3000
        assert out.gratuity_cents["k1"] == 0
        assert sum(out.gratuity_cents.values()) == 9000

    def test_even_split_ignores_hours(self):
        # someone with no recorded hours still gets an equal share
        roles = {"s1": "SERVER", "b1": "BUSSER"}
        out = compute_day_percent_tipout(
            server_tips={"s1": 0}, auto_gratuity_cents=10000, roles=roles,
            hours={"s1": 9})
        assert out.gratuity_cents == {"s1": 5000, "b1": 5000}

    def test_gratuity_without_front_of_house_flagged(self):
        out = compute_day_percent_tipout(
            server_tips={}, auto_gratuity_cents=5000,
            roles={"k1": "BOH"}, hours={"k1": 8})
        assert out.flags["undistributed_gratuity"]


class TestPoolSplitToggle:
    def test_hours_proportional_toggle(self):
        roles = {"s1": "SERVER", "b1": "BUSSER", "b2": "BUSSER", "h1": "HOST", "k1": "BOH"}
        out = compute_day_percent_tipout(
            server_tips={"s1": 50000}, roles=roles,
            hours={"s1": 6, "b1": 6, "b2": 2, "h1": 5, "k1": 8},
            pool_split_mode={"busser": "HOURS_PROPORTIONAL"})
        # 20% of 500 = 100.00 split 6:2 -> 75/25
        assert out.pools["busser"]["shares"] == {"b1": 7500, "b2": 2500}
        conserve(out)

    def test_default_is_even(self):
        roles = {"s1": "SERVER", "b1": "BUSSER", "b2": "BUSSER", "h1": "HOST", "k1": "BOH"}
        out = compute_day_percent_tipout(
            server_tips={"s1": 50000}, roles=roles,
            hours={"s1": 6, "b1": 6, "b2": 2, "h1": 5, "k1": 8})
        assert out.pools["busser"]["shares"] == {"b1": 5000, "b2": 5000}


class TestValidation:
    def test_percentages_must_sum_100(self):
        with pytest.raises(ValueError):
            validate_percentages({"server": "65", "busser": "20", "host": "10", "boh": "6"})
        with pytest.raises(ValueError):
            validate_percentages({"server": "65", "busser": "20", "host": "10"})
        ok = validate_percentages({"server": "65", "busser": "20", "host": "10", "boh": "5"})
        assert sum(ok.values()) == 1

    def test_manager_hard_block(self):
        with pytest.raises(ManagerInPoolError):
            compute_day_percent_tipout(
                server_tips={"s1": 100}, roles={"s1": "SERVER", "boss": "BOH"},
                excluded={"boss"})

    def test_tips_for_non_server_rejected(self):
        with pytest.raises(ValueError):
            compute_day_percent_tipout(
                server_tips={"b1": 100}, roles={"b1": "BUSSER", "s1": "SERVER"})

    def test_negative_tips_rejected(self):
        with pytest.raises(ValueError):
            compute_day_percent_tipout(server_tips={"s1": -100}, roles={"s1": "SERVER"})

    def test_unknown_role_rejected(self):
        with pytest.raises(ValueError):
            compute_day_percent_tipout(server_tips={}, roles={"x": "SOMMELIER"})
