import pytest
from SmartApi.smartExceptions import TokenException

from app.auth import AngelSession, AuthError, NetworkError
from app.config import Config

CFG = Config("key", "C123", "1234", "JBSWY3DPEHPK3PXP")  # valid base32 TOTP secret


class FakeApi:
    def __init__(self, login_resp, calls):
        self.login_resp, self.calls = login_resp, calls

    def generateSession(self, client_id, password, totp):
        assert client_id == "C123" and password == "1234" and len(totp) == 6
        if isinstance(self.login_resp, Exception):
            raise self.login_resp
        return self.login_resp

    def getfeedToken(self):
        return "feed-token"

    def ltpData(self, *a):
        return self.calls.pop(0)


def factory(login_resp, call_results=()):
    """Each api object created gets the next list of ltpData results."""
    made, queue = [], list(call_results)

    def make(api_key):
        assert api_key == "key"
        made.append(FakeApi(login_resp, queue.pop(0) if queue else []))
        return made[-1]

    make.made = made
    return make


OK = {"status": True, "data": {"jwtToken": "jwt", "refreshToken": "r"}}


def test_login_success():
    s = AngelSession(CFG, factory(OK))
    s.ensure()
    assert (s.jwt, s.feed_token) == ("jwt", "feed-token")


def test_login_rejected_is_an_auth_error_and_a_network_failure_is_not():
    with pytest.raises(AuthError, match="rejected"):
        AngelSession(CFG, factory({"status": False, "message": "Invalid TOTP"})).login()
    with pytest.raises(NetworkError, match="login failed"):  # no network is not a wrong password
        AngelSession(CFG, factory(ConnectionError("down"))).login()
    assert not issubclass(NetworkError, AuthError)  # an `except AuthError` can never swallow a network outage


def test_ensure_does_not_login_twice():
    f = factory(OK)
    s = AngelSession(CFG, f)
    s.ensure()
    s.ensure()
    assert len(f.made) == 1


def test_call_relogs_in_when_token_expired():
    expired = [{"status": False, "errorcode": "AG8002"}]
    fresh = [{"status": True, "data": {"ltp": 1}}]
    f = factory(OK, [expired, fresh])
    s = AngelSession(CFG, f)
    assert s.call("ltpData", "NSE", "x", "y")["data"]["ltp"] == 1
    assert len(f.made) == 2  # logged in twice: once at the start, once after the expiry


def test_call_handles_token_exception_then_gives_up():
    class Bad(FakeApi):
        def ltpData(self, *a):
            raise TokenException("expired")

    s = AngelSession(CFG, lambda api_key: Bad(OK, []))
    with pytest.raises(AuthError):
        s.call("ltpData", "NSE", "x", "y")
