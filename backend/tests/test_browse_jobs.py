from app.services.browse_jobs import BrowseGenerationJobStore
from app.services.browse_jobs import browse_generation_request_fingerprint


def test_browse_generation_request_fingerprint_is_order_independent() -> None:
    first = browse_generation_request_fingerprint(
        dataset_id="dataset-1",
        variables=["B5", "B4", "B4"],
        time_indices=[1, 0, 1],
        zoom_levels=[2, 0, 1, 2],
        overwrite=False,
    )
    second = browse_generation_request_fingerprint(
        dataset_id="dataset-1",
        variables=["B4", "B5"],
        time_indices=[0, 1],
        zoom_levels=[0, 1, 2],
        overwrite=False,
    )

    assert first == second


def test_browse_generation_store_reuses_active_duplicate_jobs() -> None:
    store = BrowseGenerationJobStore()
    first, first_created = store.create_or_get_active_job(
        dataset_id="dataset-1",
        variables=["B4"],
        time_indices=[0],
        zoom_levels=[0, 1],
        overwrite=False,
    )
    duplicate, duplicate_created = store.create_or_get_active_job(
        dataset_id="dataset-1",
        variables=["B4"],
        time_indices=[0],
        zoom_levels=[1, 0],
        overwrite=False,
    )

    assert first_created is True
    assert duplicate_created is False
    assert duplicate.job_id == first.job_id

    store.mark_failed(first.job_id, "build failed")
    retry, retry_created = store.create_or_get_active_job(
        dataset_id="dataset-1",
        variables=["B4"],
        time_indices=[0],
        zoom_levels=[0, 1],
        overwrite=False,
        retry_of_job_id=first.job_id,
    )

    assert retry_created is True
    assert retry.job_id != first.job_id
    assert retry.attempt == 2
