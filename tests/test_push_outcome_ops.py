from datetime import datetime, timedelta, timezone

import push_outcome_ops


def _outcome(chat_id, outcome, at):
    push_outcome_ops.record_push_outcome(chat_id, outcome, at)


def test_push_delivery_ratio_counts_only_cycles_that_called_the_model(isolated_subscribers_db):
    """The denominator is 'digests we paid to generate', not 'subscribers we
    looked at'. A subscriber with nothing new costs nothing and must not
    dilute the ratio -- otherwise a crowd of idle subscribers hides a
    collapsed delivery rate, which is precisely what criterion 3 exists to
    catch."""
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    _outcome(1, push_outcome_ops.PUSH_DELIVERED, now)
    _outcome(2, push_outcome_ops.PUSH_CHAT_NOT_FOUND, now)
    _outcome(3, push_outcome_ops.PUSH_CHAT_NOT_FOUND, now)
    _outcome(4, push_outcome_ops.PUSH_NOTHING_NEW, now)
    _outcome(5, push_outcome_ops.PUSH_NO_INTERESTS, now)

    delivered, generated = push_outcome_ops.push_delivery_ratio(now - timedelta(hours=24))

    assert (delivered, generated) == (1, 3)


def test_push_delivery_ratio_is_zero_zero_when_nothing_was_generated(isolated_subscribers_db):
    """(0, 0) means 'no opinion', not '0% delivered'. A quiet window is not
    an outage, and a caller that divides without checking would page on
    every idle night."""
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    _outcome(1, push_outcome_ops.PUSH_NOTHING_NEW, now)

    assert push_outcome_ops.push_delivery_ratio(now - timedelta(hours=24)) == (0, 0)


def test_push_outcome_counts_respects_the_window(isolated_subscribers_db):
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    _outcome(1, push_outcome_ops.PUSH_DELIVERED, now - timedelta(hours=1))
    _outcome(1, push_outcome_ops.PUSH_DELIVERED, now - timedelta(hours=48))

    assert push_outcome_ops.push_outcome_counts(now - timedelta(hours=24)) == {push_outcome_ops.PUSH_DELIVERED: 1}


def test_recent_outcomes_for_is_newest_first_and_per_subscriber(isolated_subscribers_db):
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    _outcome(1, push_outcome_ops.PUSH_DELIVERED, now - timedelta(hours=2))
    _outcome(1, push_outcome_ops.PUSH_CHAT_NOT_FOUND, now - timedelta(hours=1))
    _outcome(2, push_outcome_ops.PUSH_BLOCKED, now)

    assert push_outcome_ops.recent_outcomes_for(1) == [
        push_outcome_ops.PUSH_CHAT_NOT_FOUND,
        push_outcome_ops.PUSH_DELIVERED,
    ]
    assert push_outcome_ops.recent_outcomes_for(2) == [push_outcome_ops.PUSH_BLOCKED]


def test_recent_outcomes_for_breaks_same_timestamp_ties_by_insertion_order(isolated_subscribers_db):
    """Two outcomes in one tick share `now` exactly, so recorded_at alone
    cannot order them -- the id tiebreak is what keeps 'most recent' from
    depending on SQLite's row order."""
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    _outcome(1, push_outcome_ops.PUSH_DELIVERED, now)
    _outcome(1, push_outcome_ops.PUSH_BLOCKED, now)

    assert push_outcome_ops.recent_outcomes_for(1)[0] == push_outcome_ops.PUSH_BLOCKED


def test_prune_push_outcomes_drops_only_rows_past_the_window(isolated_subscribers_db):
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    _outcome(1, push_outcome_ops.PUSH_DELIVERED, now - timedelta(days=200))
    _outcome(1, push_outcome_ops.PUSH_DELIVERED, now - timedelta(days=1))

    assert push_outcome_ops.prune_push_outcomes(now) == 1
    assert push_outcome_ops.recent_outcomes_for(1) == [push_outcome_ops.PUSH_DELIVERED]


def test_record_push_outcome_assumes_utc_for_a_naive_timestamp(isolated_subscribers_db):
    """Mirrors record_push. A naive datetime silently stored as-is would
    compare wrongly against the tz-aware cutoffs every query above uses."""
    naive = datetime(2026, 8, 21, 12, 0)
    push_outcome_ops.record_push_outcome(1, push_outcome_ops.PUSH_DELIVERED, naive)

    aware = naive.replace(tzinfo=timezone.utc)
    assert push_outcome_ops.push_outcome_counts(aware - timedelta(minutes=1)) == {push_outcome_ops.PUSH_DELIVERED: 1}


def test_consecutive_chat_not_found_counts_a_run(isolated_subscribers_db):
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    for i in range(3):
        _outcome(1, push_outcome_ops.PUSH_CHAT_NOT_FOUND, now + timedelta(minutes=i))

    assert push_outcome_ops.consecutive_chat_not_found(1) == 3


def test_consecutive_chat_not_found_stops_at_the_last_delivery(isolated_subscribers_db):
    """Delivery is the only positive proof the chat is reachable, so it is
    the only thing that clears the record."""
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    _outcome(1, push_outcome_ops.PUSH_CHAT_NOT_FOUND, now)
    _outcome(1, push_outcome_ops.PUSH_DELIVERED, now + timedelta(minutes=1))
    _outcome(1, push_outcome_ops.PUSH_CHAT_NOT_FOUND, now + timedelta(minutes=2))

    assert push_outcome_ops.consecutive_chat_not_found(1) == 1


def test_consecutive_chat_not_found_skips_cycles_that_attempted_no_send(isolated_subscribers_db):
    """A quiet cycle is evidence of nothing -- it never tried to deliver. If
    it reset the count, a dead chat with an occasional quiet cycle would
    bill digests forever without ever striking out."""
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    _outcome(1, push_outcome_ops.PUSH_CHAT_NOT_FOUND, now)
    _outcome(1, push_outcome_ops.PUSH_NOTHING_NEW, now + timedelta(minutes=1))
    _outcome(1, push_outcome_ops.PUSH_NOT_RELEVANT, now + timedelta(minutes=2))
    _outcome(1, push_outcome_ops.PUSH_CHAT_NOT_FOUND, now + timedelta(minutes=3))

    assert push_outcome_ops.consecutive_chat_not_found(1) == 2


def test_consecutive_chat_not_found_is_zero_for_a_healthy_subscriber(isolated_subscribers_db):
    now = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
    _outcome(1, push_outcome_ops.PUSH_DELIVERED, now)

    assert push_outcome_ops.consecutive_chat_not_found(1) == 0


def test_disabled_is_not_counted_as_a_generated_digest(isolated_subscribers_db):
    """Striking a subscriber out costs no LLM call, so it must not land in
    the denominator of the delivery ratio."""
    assert push_outcome_ops.PUSH_DISABLED not in push_outcome_ops.PUSH_GENERATED_OUTCOMES
