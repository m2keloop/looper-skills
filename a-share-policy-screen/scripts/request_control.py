"""One pooled HTTP client with a global per-process request-start budget."""
import threading
import time
from collections import Counter
from urllib.parse import urlsplit


class HTTPClient:
    def __init__(self, interval=0.25, session=None, clock=time.monotonic, sleep=time.sleep):
        if session is None:
            import requests
            session = requests.Session()
        self.session, self.clock, self.sleep = session, clock, sleep
        self.interval = interval
        self.last_start = None
        self.lock = threading.Lock()
        self.requests = self.failures = 0
        self.wait_seconds = self.http_seconds = 0.0
        self.endpoints = Counter()

    def request(self, method, url, **kwargs):
        with self.lock:
            now = self.clock()
            wait = max(0.0, self.interval - (now-self.last_start)) if self.last_start is not None else 0.0
            if wait:
                self.sleep(wait)
            self.wait_seconds += wait
            self.last_start = self.clock()
            self.requests += 1
            parsed = urlsplit(url)
            path = '/q=<batch>' if parsed.path.startswith('/q=') else parsed.path
            self.endpoints[parsed.netloc+path] += 1
        started = self.clock()
        try:
            response = self.session.request(method, url, **kwargs)
            if response.status_code >= 400:
                self.failures += 1
            return response
        except Exception:
            self.failures += 1
            raise
        finally:
            self.http_seconds += self.clock()-started

    def metrics(self):
        return {'http_requests':self.requests, 'http_failures':self.failures,
                'request_wait_seconds':round(self.wait_seconds,3),
                'http_seconds':round(self.http_seconds,3), 'endpoints':dict(self.endpoints)}
