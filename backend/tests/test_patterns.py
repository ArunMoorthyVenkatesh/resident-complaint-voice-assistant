"""
Tests for patterns.py — cross-complaint pattern detection.

Timestamps are generated relative to "now" (rather than hard-coded dates) so the
suite keeps passing regardless of when it's run.
"""

from datetime import timedelta

from patterns import SGT, _location_key, detect_patterns


def ts(days_ago: float) -> str:
    """Build a timestamp string in the same format dynamodb.py writes."""
    from datetime import datetime
    t = datetime.now(SGT) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%d %H:%M:%S SGT")


def complaint(complaint_id, complaint_type, location, days_ago):
    return {
        "complaint_id": complaint_id,
        "complaint_type": complaint_type,
        "location": location,
        "timestamp": ts(days_ago),
    }


class TestLocationKey:
    def test_extracts_block_number(self):
        assert _location_key("Block 204, near the lift") == "block 204"

    def test_extracts_abbreviated_block(self):
        assert _location_key("blk 12") == "block 12"

    def test_falls_back_to_first_words(self):
        assert _location_key("5th floor toilet, near stairs") == "5th floor toilet"

    def test_empty_location_is_unspecified(self):
        assert _location_key("") == "unspecified"
        assert _location_key(None) == "unspecified"


class TestClusterDetection:
    def test_three_same_type_same_area_triggers_cluster(self):
        complaints = [
            complaint("CMP-1", "Air-conditioning", "Block 204", days_ago=1),
            complaint("CMP-2", "Air-conditioning", "Block 204", days_ago=3),
            complaint("CMP-3", "Air-conditioning", "Block 204", days_ago=5),
        ]
        alerts = detect_patterns(complaints)
        clusters = [a for a in alerts if a["type"] == "cluster"]
        assert len(clusters) == 1
        assert clusters[0]["count"] == 3
        assert clusters[0]["location_key"] == "block 204"

    def test_below_threshold_does_not_trigger(self):
        complaints = [
            complaint("CMP-1", "Air-conditioning", "Block 204", days_ago=1),
            complaint("CMP-2", "Air-conditioning", "Block 204", days_ago=3),
        ]
        alerts = detect_patterns(complaints)
        assert not [a for a in alerts if a["type"] == "cluster"]

    def test_unspecified_location_never_clusters(self):
        complaints = [
            complaint("CMP-1", "Air-conditioning", "", days_ago=1),
            complaint("CMP-2", "Air-conditioning", "", days_ago=2),
            complaint("CMP-3", "Air-conditioning", "", days_ago=3),
        ]
        alerts = detect_patterns(complaints)
        assert not [a for a in alerts if a["type"] == "cluster"]


class TestRecurringDetection:
    def test_same_spot_spread_over_time_triggers_recurring(self):
        complaints = [
            complaint("CMP-1", "Water and Plumbing", "Block 12", days_ago=60),
            complaint("CMP-2", "Water and Plumbing", "Block 12", days_ago=5),
        ]
        alerts = detect_patterns(complaints)
        recurring = [a for a in alerts if a["type"] == "recurring"]
        assert len(recurring) == 1
        assert recurring[0]["count"] == 2

    def test_tight_cluster_is_not_also_flagged_recurring(self):
        # All within the 14-day cluster window -> should show up as a cluster,
        # not double-counted as "recurring" too.
        complaints = [
            complaint("CMP-1", "Electrical", "Block 5", days_ago=1),
            complaint("CMP-2", "Electrical", "Block 5", days_ago=2),
            complaint("CMP-3", "Electrical", "Block 5", days_ago=3),
        ]
        alerts = detect_patterns(complaints)
        assert [a for a in alerts if a["type"] == "cluster"]
        assert not [a for a in alerts if a["type"] == "recurring"]


class TestSpikeDetection:
    def test_month_over_month_spike_is_detected(self):
        complaints = [
            complaint("CMP-1", "Lighting", "Block 1", days_ago=0),
            complaint("CMP-2", "Lighting", "Block 2", days_ago=1),
            complaint("CMP-3", "Lighting", "Block 3", days_ago=2),
            complaint("CMP-4", "Lighting", "Block 4", days_ago=90),
        ]
        alerts = detect_patterns(complaints)
        spikes = [a for a in alerts if a["type"] == "spike"]
        assert len(spikes) == 1
        assert spikes[0]["complaint_type"] == "Lighting"
        assert spikes[0]["count"] == 3

    def test_no_prior_history_does_not_spike(self):
        complaints = [
            complaint("CMP-1", "Security", "Block 1", days_ago=0),
            complaint("CMP-2", "Security", "Block 2", days_ago=1),
            complaint("CMP-3", "Security", "Block 3", days_ago=2),
        ]
        alerts = detect_patterns(complaints)
        assert not [a for a in alerts if a["type"] == "spike"]


class TestDetectPatternsRobustness:
    def test_unparsable_timestamp_is_skipped_not_raised(self):
        complaints = [
            {"complaint_id": "CMP-1", "complaint_type": "Cleaning", "location": "Block 1", "timestamp": "not-a-date"},
        ]
        assert detect_patterns(complaints) == []

    def test_empty_input_returns_empty_list(self):
        assert detect_patterns([]) == []
