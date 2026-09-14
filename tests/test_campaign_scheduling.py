from repo2rlenv.campaigns.scheduling import ActiveBatch, available_task_slots


def test_inflight_allocations_prevent_parallel_batches_exceeding_collection_target():
    inventory = {f"old-{i}": "old" for i in range(60)}
    active = [ActiveBatch("r2e", "first", 25, frozenset())]
    assert available_task_slots("r2e", "second", inventory, active) == 15
    active.append(ActiveBatch("r2e", "second", 15, frozenset()))
    assert available_task_slots("r2e", "third", inventory, active) == 0


def test_receipt_ahead_of_inventory_does_not_free_unaccounted_slots():
    inventory = {f"old-{i}": "old" for i in range(60)}
    active = [ActiveBatch("r2e", "first", 25, frozenset({"new-1", "new-2"}))]
    assert available_task_slots("r2e", "second", inventory, active) == 15
    inventory["new-1"] = "first"
    assert available_task_slots("r2e", "second", inventory, active) == 15


def test_source_cap_and_other_recipes_are_independent():
    inventory = {f"old-{i}": "first" for i in range(20)}
    active = [ActiveBatch("swe_gen", "first", 25, frozenset())]
    assert available_task_slots("r2e", "first", inventory, active) == 5
    active.append(ActiveBatch("r2e", "first", 5, frozenset()))
    assert available_task_slots("r2e", "first", inventory, active) == 0


def test_partial_finished_batch_releases_only_unfilled_slots():
    inventory = {f"old-{i}": "old" for i in range(60)}
    inventory.update({f"new-{i}": "first" for i in range(10)})
    active = [ActiveBatch("r2e", "first", 25, frozenset(f"new-{i}" for i in range(10)))]
    assert available_task_slots("r2e", "second", inventory, active) == 15
    assert available_task_slots("r2e", "second", inventory, []) == 25
