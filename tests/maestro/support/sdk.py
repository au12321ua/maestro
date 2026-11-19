"""A Python SDK client for one test API caller."""

import httpx
from maestro_sdk import (
    MaestroClient,
)


def sdk(api, http=None) -> MaestroClient:
    """An SDK client on the test application, authenticated by the caller's PAT."""
    return MaestroClient("http://testserver", api.token, http=http or api.client)


class LoseFirstAnswer(httpx.BaseTransport):
    """Delivers the request, then drops the first answer to a matching one."""

    def __init__(self, inner, method, path):
        self.inner, self.method, self.path = inner, method, path
        self.lost = 0

    def handle_request(self, request):
        response = self.inner.handle_request(request)
        if request.method == self.method and request.url.path == self.path and not self.lost:
            self.lost += 1
            response.read()
            raise httpx.ReadError("connection reset", request=request)
        return response
