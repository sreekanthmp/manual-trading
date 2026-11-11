"""Angel One login and session upkeep."""

import logging
import threading

import pyotp
import requests
from SmartApi import SmartConnect
from SmartApi.smartExceptions import TokenException

from .ratelimit import is_refusal

log = logging.getLogger(__name__)

# AG8001 invalid token, AG8002 token expired, AG8003 token missing
TOKEN_ERROR_CODES = {"AG8001", "AG8002", "AG8003"}


class AuthError(Exception):
    """Angel One answered and refused the credentials, or refused the session even after a fresh login."""


class NetworkError(Exception):
    """Angel One could not be reached or answered with something unusable (no network, DNS, a timeout, a gateway error, the rate limit).
    Temporary: the caller retries, and it is never counted as a rejected login."""


def is_transient(e: Exception) -> bool:
    """A failure of the connection or of the service, not of the account."""
    return isinstance(e, (requests.exceptions.RequestException, OSError)) or type(e).__name__ in ("NetworkException", "DataException") or is_refusal(e)


class AngelSession:
    def __init__(self, cfg, connect=SmartConnect, limiter=None):
        self.cfg = cfg
        self._connect = connect
        self.limiter = limiter  # a CandleLimiter, shared by every caller of getCandleData
        self._lock = threading.RLock()
        self.api = None
        self.jwt = None
        self.feed_token = None

    def login(self) -> None:
        with self._lock:
            try:
                api = self._connect(api_key=self.cfg.api_key)
                totp = pyotp.TOTP(self.cfg.totp_secret).now()
                data = api.generateSession(self.cfg.client_id, self.cfg.password, totp)
                if data and data.get("status"):
                    feed_token = api.getfeedToken()
            except Exception as e:
                if is_transient(e):  # no network, DNS, a timeout, a gateway error: the account is not at fault
                    raise NetworkError(f"login failed (Angel One not reachable): {type(e).__name__}: {e}") from e
                raise AuthError(f"login failed: {type(e).__name__}: {e}") from e  # e.g. a TOTP secret that is not valid base32
            if not data or not data.get("status"):
                message = (data or {}).get("message", "no response")
                if is_refusal(message):
                    raise NetworkError(f"login refused for its rate, will retry: {message}")
                raise AuthError(f"login rejected: {message}")
            self.api = api
            self.jwt = data["data"]["jwtToken"]
            self.feed_token = feed_token
            log.info("Angel One login ok")

    def ensure(self) -> None:
        with self._lock:
            if self.api is None:
                self.login()

    def invalidate(self) -> None:
        """Forget the session; the next ensure() logs in again."""
        with self._lock:
            self.api = None

    def call(self, method: str, *args):
        """Call a SmartConnect method; if the token has expired, log in again and retry once."""
        for _ in range(2):
            self.ensure()
            limited = self.limiter is not None and method == "getCandleData"
            if limited:
                self.limiter.wait()  # candle requests share one allowance
            try:
                resp = getattr(self.api, method)(*args)
                expired = isinstance(resp, dict) and resp.get("errorcode") in TOKEN_ERROR_CODES
            except TokenException:
                expired = True
            except Exception as e:
                if limited and is_refusal(e):
                    self.limiter.refused(e)
                raise
            if not expired:
                if limited:
                    self.limiter.ok()
                return resp
            log.warning("session expired during %s, re-authenticating", method)
            self.invalidate()
        raise AuthError(f"{method}: session still rejected after re-login")
