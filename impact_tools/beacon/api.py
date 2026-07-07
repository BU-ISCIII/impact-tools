"""Direct HTTP operations against the Beacon API."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator

import httpx

from impact_tools.beacon.config import BeaconApiConfig


LOGGER = logging.getLogger(__name__)


@contextmanager
def managed_beacon_api(
    config: BeaconApiConfig,
) -> Iterator[httpx.Client]:
    """Open and close a direct Beacon API client."""

    with httpx.Client(
        base_url=config.base_url.rstrip("/"),
        timeout=config.timeout_seconds,
        verify=config.verify_tls,
        headers={
            "Accept": "application/json",
            "User-Agent": "impact-tools",
        },
    ) as client:
        yield client


def _read_json_object(
    response: httpx.Response,
) -> dict[str, Any]:
    """Validate an HTTP response and return its JSON object."""

    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "Beacon API returned an invalid JSON response.\n"
            f"URL: {response.request.url}\n"
            f"Response: {response.text[:1000]}"
        ) from exc

    if not isinstance(payload, dict):
        raise RuntimeError(
            "Unexpected Beacon API response: expected a JSON object.\n"
            f"URL: {response.request.url}"
        )

    return payload


def get_service_info(
    client: httpx.Client,
) -> dict[str, Any]:
    """Return the Beacon service-info response."""

    response = client.get("/api/service-info")
    return _read_json_object(response)


def _contains_id(
    value: Any,
    target_id: str,
) -> bool:
    """Recursively check whether a JSON structure contains an object ID."""

    if isinstance(value, dict):
        if value.get("id") == target_id:
            return True

        return any(
            _contains_id(child, target_id)
            for child in value.values()
        )

    if isinstance(value, list):
        return any(
            _contains_id(child, target_id)
            for child in value
        )

    return False


def verify_dataset_via_api(
    client: httpx.Client,
    dataset_id: str,
) -> bool:
    """Check whether a dataset is exposed by the Beacon API."""

    LOGGER.info(
        "Verifying dataset '%s' via Beacon API...",
        dataset_id,
    )

    response = client.get("/api/datasets")

    payload = _read_json_object(response)
    found = _contains_id(payload, dataset_id)

    if found:
        LOGGER.info(
            "Dataset '%s' is visible via Beacon API.",
            dataset_id,
        )
    else:
        LOGGER.warning(
            "Dataset '%s' is NOT visible via Beacon API.",
            dataset_id,
        )

    return found


def get_variant_count_via_api(
    client: httpx.Client,
    dataset_id: str,
) -> int:
    """Return the number of variants exposed for a dataset."""

    response = client.get(
        "/api/g_variants",
        params={
            "datasets": dataset_id,
            "requestedGranularity": "count",
            "limit": 0,
        },
    )

    payload = _read_json_object(response)

    response_summary = payload.get("responseSummary")

    if not isinstance(response_summary, dict):
        raise RuntimeError(
            "Beacon API response does not contain a valid "
            f"responseSummary for dataset {dataset_id!r}."
        )

    observed_count = response_summary.get("numTotalResults")

    if (
        isinstance(observed_count, bool)
        or not isinstance(observed_count, int)
    ):
        raise RuntimeError(
            "Beacon API response does not contain a valid "
            f"numTotalResults for dataset {dataset_id!r}: "
            f"{observed_count!r}"
        )

    return observed_count


def verify_variant_count_via_api(
    client: httpx.Client,
    dataset_id: str,
    expected_count: int,
) -> bool:
    """Check whether the Beacon API exposes the expected variant count."""

    LOGGER.info(
        "Verifying variant count for dataset '%s' via Beacon API...",
        dataset_id,
    )

    observed_count = get_variant_count_via_api(
        client,
        dataset_id,
    )

    if observed_count != expected_count:
        LOGGER.warning(
            "Variant count mismatch via API for %s: "
            "observed=%d expected=%d",
            dataset_id,
            observed_count,
            expected_count,
        )
        return False

    LOGGER.info(
        "Variant count verified via API for %s: %d variants",
        dataset_id,
        expected_count,
    )

    return True