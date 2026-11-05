"""A Python SDK client for one test API caller."""

from maestro_sdk import (
    MaestroClient,
)


def sdk(api, http=None) -> MaestroClient:
    """An SDK client on the test application, authenticated by the caller's PAT."""
    return MaestroClient("http://testserver", api.token, http=http or api.client)
