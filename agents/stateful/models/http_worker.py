"""Stdlib-only bounded HTTP worker; separate from the untrusted code worker.

The parent can terminate DNS, TLS or slow header/body reads at the total request
deadline. The selected auth header travels over stdin, never on the command line.
"""

import base64
import json
import sys
import time
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_http(endpoint, headers, body, timeout_s, max_response_bytes, *, opener=None):
    opener = opener or urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    start = time.monotonic()
    try:
        with opener.open(request, timeout=timeout_s) as response:
            chunks, size = [], 0
            while True:
                if time.monotonic() - start >= timeout_s:
                    return {"error": "MODEL_TIMEOUT", "retryable": True}
                chunk = response.read1(min(8192, max_response_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > max_response_bytes:
                    return {"error": "MODEL_RESPONSE_TOO_LARGE", "retryable": False}
            return {"status": response.status, "body": base64.b64encode(b"".join(chunks)).decode()}
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        return {"status": status, "body": ""}
    except (TimeoutError, urllib.error.URLError, OSError):
        return {"error": "MODEL_TRANSPORT_ERROR", "retryable": True}


def main():
    try:
        request = json.loads(sys.stdin.buffer.read(4 * 1024 * 1024))
        request["body"] = base64.b64decode(request["body"])
        result = request_http(**request)
    except Exception:
        result = {"error": "MODEL_TRANSPORT_ERROR", "retryable": False}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":
    main()
