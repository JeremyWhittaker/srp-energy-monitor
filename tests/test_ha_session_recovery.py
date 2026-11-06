"""The Home Assistant client must survive a lost SRP session on its own.

On 2026-09-02 it did not. Home Assistant restarted, the integration's first
refresh drew a 401 from ``usage/dailyusagedetail``, the client called that a
credential failure, the coordinator escalated it to ``ConfigEntryAuthFailed``,
and Home Assistant stopped polling permanently. Six sensors read ``unavailable``
for eighteen hours. The password was correct throughout -- what had actually
expired was the ``.AspNet.MyAccount`` session cookie.

The fix is a classification rule and a retry, and both are easy to undo by
accident, because the wrong behaviour looks perfectly reasonable in a diff: a
401 *is* an authentication status code. What these tests pin down is that no
status code, on any path, is a credential failure by itself. The verdict is
reached only when SRP says ``invalid_grant`` on ``login/authorize`` -- its own
words, on the one request in the API that submits a password.

That is narrower than "a 4xx from login/authorize", and the difference is the
whole point of half the file below. A login can fail 400 or 401 for reasons that
have nothing to do with the password -- a rate limit, a WAF rule, a maintenance
window, a temporary lockout -- and on a fresh Home Assistant start the very
first thing the integration does is log in, with no data request in front of it.
Calling that a bad password reproduces the outage one layer down.

Everything here runs against a scripted stub session. No network, no Home
Assistant, no event loop plugin -- ``asyncio.run`` inside a synchronous test,
matching ``tests/test_service.py``.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
from datetime import date
from pathlib import Path

import pytest

pytest.importorskip("aiohttp", reason="the component uses Home Assistant's aiohttp")

COMPONENT = Path(__file__).parent.parent / "custom_components" / "srp_energy_monitor"

DAILY_PATH = "usage/dailyusagedetail"
PROJECTION_PATH = "payments/billprojection"
HOURLY_PATH = "usage/hourlydetail"

# The bodies below are the real ones, copied from live responses during the
# diagnosis. Asserting against invented text would prove only that the code
# agrees with the test author.
UNAUTHORIZED_BODY = '{"message":"Authorization has been denied for this request."}'
INVALID_GRANT_BODY = (
    '{"error":"invalid_grant","error_description":"The user name or password is incorrect."}'
)
CLOUDFLARE_BODY = "<html><title>Access denied</title><body>error code: 1020</body></html>"

# A login refused for a reason that is not the password. SRP has not been
# observed sending this exact body, and that is the point: the rule has to cope
# with a login failure it does not recognise, and the safe reading of an
# unrecognised failure is "try again later", not "wake the user up".
THROTTLED_BODY = '{"error":"temporarily_unavailable"}'

DAILY_PAYLOAD = {"dailyConsumptionList": [], "highDemandInfoList": []}


@pytest.fixture(scope="module")
def srp_api():
    """Import the component's module without needing Home Assistant installed.

    Same trick as ``tests/test_component_parity.py``: the component is not a
    package on sys.path, it is a directory Home Assistant loads by hand.
    """
    sys.path.insert(0, str(COMPONENT))
    try:
        import srp_api

        return srp_api
    finally:
        sys.path.remove(str(COMPONENT))


# ------------------------------------------------------------- stub transport


class _Cookie:
    """Just enough of ``http.cookies.Morsel`` for ``_login`` to read a value."""

    def __init__(self, value: str) -> None:
        self.value = value


#: Distinct on purpose. A jar that answered with the same token forever made
#: ``test_the_retried_request_carries_the_refreshed_token`` vacuous: the
#: assertion held whether the retry carried the new token or the dead one it had
#: just been 401'd for, so the test proved nothing about the thing it named.
DEFAULT_TOKENS = ("xsrf-first", "xsrf-second", "xsrf-third")


class _CookieJar:
    """Issues a different token on each login, in order.

    ``SRPClient._login`` reads the jar exactly once per handshake, so "one call
    to :meth:`filter_cookies`" and "one login" are the same event, and the
    sequence position is a reliable stand-in for "how many times have we logged
    in". The last token repeats once the sequence runs out, so a test that does
    not care about rotation need not count logins. Pass ``()`` for a jar with no
    token cookie at all.
    """

    def __init__(self, tokens=DEFAULT_TOKENS) -> None:
        self._tokens = tuple(tokens or ())
        self.issued = 0

    def filter_cookies(self, _url):
        if not self._tokens:
            return {}
        value = self._tokens[min(self.issued, len(self._tokens) - 1)]
        self.issued += 1
        return {"xsrf-token": _Cookie(value)}


class _Response:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def json(self, content_type=None):
        return json.loads(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class StubSession:
    """An aiohttp session that replays a script and records what it was asked.

    ``script`` maps a path to a list of ``(status, body)`` pairs consumed in
    order; the last pair repeats once exhausted, so a test that wants "401
    forever" writes one entry rather than guessing how many calls will happen.
    """

    def __init__(self, script: dict[str, list[tuple[int, str]]], tokens=DEFAULT_TOKENS) -> None:
        self._script = {path: list(pairs) for path, pairs in script.items()}
        self.cookie_jar = _CookieJar(tokens)
        self.calls: list[str] = []
        self.headers_seen: list[dict[str, str]] = []
        self.params_seen: list[dict[str, str]] = []

    def request(self, method, url, *, data=None, params=None, headers=None, timeout=None):
        path = str(url).split("/myaccountapi/api/", 1)[-1]
        self.calls.append(path)
        self.headers_seen.append(dict(headers or {}))
        self.params_seen.append(dict(params or {}))
        pairs = self._script.get(path)
        if not pairs:
            raise AssertionError(f"stub session has no script for {path}")
        status, body = pairs[0] if len(pairs) == 1 else pairs.pop(0)
        return _Response(status, body)

    def count(self, path: str) -> int:
        return self.calls.count(path)

    def headers_for(self, path: str) -> list[dict[str, str]]:
        """Headers of every request to one path, in order."""
        return [h for seen, h in zip(self.calls, self.headers_seen, strict=True) if seen == path]

    def params_for(self, path: str) -> list[dict[str, str]]:
        """Query parameters of every request to one path, in order."""
        return [p for seen, p in zip(self.calls, self.params_seen, strict=True) if seen == path]


def _ok_login() -> dict[str, list[tuple[int, str]]]:
    """A login handshake that always succeeds, for scripts to build on."""
    return {
        "login/authorize": [(200, '{"success":true}')],
        "login/antiforgerytoken": [(200, '""')],
    }


def _client(srp_api, session):
    return srp_api.SRPClient(session, "Whittakerj", "correct-horse", "885789006")


def _run(coro):
    """Run a coroutine with a deadline.

    The deadline is load-bearing, not decoration. The re-login path runs while
    ``SRPClient._lock`` is already held by the public method that started the
    call, and ``asyncio.Lock`` is not reentrant -- so the specific way this fix
    breaks is a deadlock, which without a timeout is a hung suite rather than a
    failing test.
    """

    async def _bounded():
        return await asyncio.wait_for(coro, timeout=5)

    return asyncio.run(_bounded())


async def _noop_sleep(_seconds):
    """Skip the politeness delay between interval windows."""


# ---------------------------------------------------- the recovery, end to end


def test_data_401_relogs_in_once_and_the_retry_succeeds(srp_api):
    """The outage, replayed: a stale cookie must heal without human help."""
    session = StubSession(
        _ok_login() | {DAILY_PATH: [(401, UNAUTHORIZED_BODY), (200, json.dumps(DAILY_PAYLOAD))]}
    )
    client = _client(srp_api, session)

    assert _run(client.daily_detail()) == DAILY_PAYLOAD

    # Exactly one re-login: the handshake at the start of the call, and one more
    # provoked by the 401. A third would mean the retry guard is not holding.
    assert session.count("login/authorize") == 2
    assert session.count("login/antiforgerytoken") == 2
    assert session.count(DAILY_PATH) == 2
    assert session.calls == [
        "login/authorize",
        "login/antiforgerytoken",
        DAILY_PATH,
        "login/authorize",
        "login/antiforgerytoken",
        DAILY_PATH,
    ]


def test_a_second_consecutive_401_is_retryable_not_a_credential_failure(srp_api):
    """The important negative. Retryable means Home Assistant keeps polling.

    ``SRPAuthError`` would become ``ConfigEntryAuthFailed`` and end polling for
    good; a bare ``SRPApiError`` becomes ``UpdateFailed`` and heals by itself.
    """
    session = StubSession(_ok_login() | {DAILY_PATH: [(401, UNAUTHORIZED_BODY)]})
    client = _client(srp_api, session)

    with pytest.raises(srp_api.SRPApiError) as caught:
        _run(client.daily_detail())

    assert not isinstance(caught.value, srp_api.SRPAuthError)
    # Still only one repair attempt; a 401 loop must not become a login loop.
    assert session.count("login/authorize") == 2
    assert session.count(DAILY_PATH) == 2


def test_the_retry_is_transparent_on_the_projection_endpoint_too(srp_api):
    """Recovery belongs to the transport, not to one lucky call site."""
    session = StubSession(
        _ok_login()
        | {PROJECTION_PATH: [(401, UNAUTHORIZED_BODY), (200, '{"hasProjection":false}')]}
    )
    client = _client(srp_api, session)

    assert _run(client.projection()) == {"hasProjection": False}
    assert session.count("login/authorize") == 2


def test_the_retry_works_inside_the_chunked_interval_loop(srp_api, monkeypatch):
    """The statistics backfill is the highest-volume path and the likeliest to
    outlive a session, since it makes one request per month of history.

    The range has to be wider than :data:`srp_api.HOURLY_MAX_DAYS`, and by more
    than a rounding error. An earlier version of this test asked for three days,
    which ``chunk_date_range`` turns into a single window -- so the loop ran once
    and the test could not tell the chunked path from the unchunked one it
    claimed to be exercising. Sixty-five days is three windows, and the 401 is
    scripted to land on the second, mid-loop.
    """
    # Patch the module's own indirection, not asyncio.sleep. srp_api.asyncio is
    # the real stdlib module object, shared with everything else in the process;
    # monkeypatching through it reached far outside this test.
    monkeypatch.setattr(srp_api, "_SLEEP", _noop_sleep)

    start, end = date(2026, 7, 1), date(2026, 9, 3)
    windows = srp_api.chunk_date_range(start, end)
    # Pin the premise rather than trusting it: if HOURLY_MAX_DAYS ever grows past
    # the range, this test would silently stop testing a loop.
    assert len(windows) == 3, windows

    session = StubSession(
        _ok_login()
        | {
            HOURLY_PATH: [
                (200, '{"demandList":[]}'),  # first window: fine
                (401, UNAUTHORIZED_BODY),  # second window: the cookie dies here
                (200, '{"demandList":[]}'),  # the replay, and the third window
            ]
        }
    )
    client = _client(srp_api, session)

    assert _run(client.intervals(start, end)) == []

    # Three windows plus one replay, and exactly one repair login.
    assert session.count(HOURLY_PATH) == 4
    assert session.count("login/authorize") == 2

    # The replay must re-ask for the window that failed -- not restart the loop,
    # and not skip ahead. Silently dropping a month of a backfill is the failure
    # this whole chunking scheme exists to prevent.
    asked = [(p["beginDate"], p["endDate"]) for p in session.params_for(HOURLY_PATH)]
    assert asked == [
        ("07-01-2026", "07-31-2026"),
        ("08-01-2026", "08-31-2026"),
        ("08-01-2026", "08-31-2026"),
        ("09-01-2026", "09-03-2026"),
    ]


# ------------------------------------------------- classification, rule by rule


def test_invalid_grant_on_the_login_endpoint_is_still_an_auth_error(srp_api):
    """A wrong password must still stop the integration and ask for a new one."""
    session = StubSession({"login/authorize": [(400, INVALID_GRANT_BODY)]})
    client = _client(srp_api, session)

    with pytest.raises(srp_api.SRPAuthError) as caught:
        _run(client.validate())

    assert "invalid_grant" in str(caught.value)
    # A credential failure is not retried: retrying a wrong password is how
    # accounts get locked. This is also the answer to the worry that gated the
    # old, broader rule -- narrowing it did not create a wrong-password retry
    # loop, because SRP names the failure and the client believes it.
    assert session.count("login/authorize") == 1


def test_invalid_grant_with_a_401_is_an_auth_error_too(srp_api):
    """The status half of the rule is 400 *or* 401; only the body is decisive.

    SRP has been seen returning 400 for this, but the rule does not depend on
    which of the two it picks, and pinning both halves keeps a future
    simplification honest.
    """
    session = StubSession({"login/authorize": [(401, INVALID_GRANT_BODY)]})
    client = _client(srp_api, session)

    with pytest.raises(srp_api.SRPAuthError):
        _run(client.validate())


def test_401_on_the_login_endpoint_is_not_a_credential_failure(srp_api):
    """A 401 at login/authorize does not mean the password is wrong.

    This test used to assert the opposite, using the body below -- the one
    proven to mean "no session", which is what an unauthenticated request draws
    from ``login/antiforgerytoken`` as well. Asserting ``SRPAuthError`` for it
    cemented the last route by which a transient failure could stop polling
    forever, and it passed against the pre-fix code, which is what a test
    pinning a bug looks like.

    Nothing about a bare 401 identifies a password. A wrong one says
    ``invalid_grant`` and says it out loud.
    """
    session = StubSession({"login/authorize": [(401, UNAUTHORIZED_BODY)]})
    client = _client(srp_api, session)

    with pytest.raises(srp_api.SRPApiError) as caught:
        _run(client.validate())

    assert not isinstance(caught.value, srp_api.SRPAuthError)
    # Still no retry loop: validate() makes one attempt and reports.
    assert session.count("login/authorize") == 1


def test_400_on_the_login_endpoint_without_invalid_grant_is_not_a_credential_failure(srp_api):
    """The other half of the narrowed rule.

    A rate limit, a WAF rule or a maintenance window can answer a login with a
    400 that says nothing about credentials. Retrying that costs one request
    every four hours. Calling it a bad password costs every sensor until a human
    notices -- which, last time, took eighteen hours.
    """
    session = StubSession({"login/authorize": [(400, THROTTLED_BODY)]})
    client = _client(srp_api, session)

    with pytest.raises(srp_api.SRPApiError) as caught:
        _run(client.validate())

    assert not isinstance(caught.value, srp_api.SRPAuthError)


def test_a_login_failure_on_a_cold_start_does_not_stop_polling(srp_api):
    """The outage shape one layer down, and the reason the login path was narrowed.

    A fresh Home Assistant start has no cached token, so ``_ensure_session``
    goes straight to a login -- there is no data request in front of it to draw
    the 401 that the earlier fix taught the client to repair. Whatever that
    login returns is the integration's whole first impression, and under the old
    rule a bare 4xx there was terminal.
    """
    session = StubSession({"login/authorize": [(400, THROTTLED_BODY)]})
    client = _client(srp_api, session)

    # No session yet: exactly the state Home Assistant restarts into.
    assert client._xsrf is None

    with pytest.raises(srp_api.SRPApiError) as caught:
        _run(client.daily_detail())

    assert not isinstance(caught.value, srp_api.SRPAuthError)
    # It never reached the data endpoint, which is what makes this distinct from
    # the 401-on-a-data-endpoint case above.
    assert session.calls == ["login/authorize"]


def test_403_access_denied_is_still_a_cloudflare_block(srp_api):
    """Unchanged behaviour, pinned. Cloudflare varies by host and by moment, and
    telling that user to retype their password wastes their evening."""
    session = StubSession(_ok_login() | {DAILY_PATH: [(403, CLOUDFLARE_BODY)]})
    client = _client(srp_api, session)

    with pytest.raises(srp_api.SRPAccessBlocked):
        _run(client.daily_detail())

    assert not isinstance(srp_api.SRPAccessBlocked(), srp_api.SRPAuthError)
    # No re-login: a Cloudflare block is not a session problem, and hammering
    # the login endpoint through one helps nobody.
    assert session.count("login/authorize") == 1


def test_403_without_access_denied_is_not_reclassified(srp_api):
    """A 403 lacking Cloudflare's wording is a missing x-xsrf-token, i.e. a
    plain API error. It must not become a credential failure either."""
    session = StubSession(_ok_login() | {DAILY_PATH: [(403, '{"message":"Forbidden"}')]})
    client = _client(srp_api, session)

    with pytest.raises(srp_api.SRPApiError) as caught:
        _run(client.daily_detail())
    assert not isinstance(caught.value, srp_api.SRPAuthError)


def test_400_on_a_data_endpoint_is_no_longer_a_credential_failure(srp_api):
    """The other half of the old over-broad rule.

    ``status in (400, 401)`` used to mean SRPAuthError on every path. A 400 from
    a usage endpoint is a malformed request -- a bad account number, say -- and
    no amount of retyping a password fixes it.
    """
    session = StubSession(_ok_login() | {DAILY_PATH: [(400, '{"message":"Bad Request"}')]})
    client = _client(srp_api, session)

    with pytest.raises(srp_api.SRPApiError) as caught:
        _run(client.daily_detail())
    assert not isinstance(caught.value, srp_api.SRPAuthError)


def test_a_500_is_retryable(srp_api):
    session = StubSession(_ok_login() | {DAILY_PATH: [(500, "upstream exploded")]})
    client = _client(srp_api, session)

    with pytest.raises(srp_api.SRPApiError) as caught:
        _run(client.daily_detail())
    assert not isinstance(caught.value, srp_api.SRPAuthError)


# ------------------------------------------------------------ structural rules


def test_session_expiry_is_retryable_by_construction(srp_api):
    """The exception hierarchy is what decides the coordinator's behaviour.

    ``SRPCoordinator._async_update_data`` catches SRPAuthError first and
    SRPApiError second, so making SRPSessionExpired a subclass of the wrong one
    would silently restore the outage.
    """
    assert issubclass(srp_api.SRPSessionExpired, srp_api.SRPApiError)
    assert not issubclass(srp_api.SRPSessionExpired, srp_api.SRPAuthError)
    assert not issubclass(srp_api.SRPAccessBlocked, srp_api.SRPAuthError)


def test_login_clears_the_cached_token_before_using_it(srp_api):
    """A failed login must leave "no session", not a token without a cookie.

    That stale-token state is what made the original outage permanent:
    ``_ensure_session`` only logs in when the token is None, so a token that
    outlived its cookie meant the client never tried to log in again.
    """
    session = StubSession({"login/authorize": [(500, "boom")]})
    client = _client(srp_api, session)
    client._xsrf = "stale-token-from-a-dead-session"

    with pytest.raises(srp_api.SRPApiError):
        _run(client.validate())

    assert client._xsrf is None


def test_a_login_without_an_xsrf_cookie_fails_loudly(srp_api):
    """Without the token every usage endpoint answers 403, which reads exactly
    like the Cloudflare block -- so catch it here where it is unambiguous."""
    session = StubSession(_ok_login(), tokens=())
    client = _client(srp_api, session)

    with pytest.raises(srp_api.SRPApiError, match="no xsrf-token cookie"):
        _run(client.validate())


def test_login_refuses_to_run_without_the_lock(srp_api):
    """``_login`` is lock-free by contract, and the contract is now checked.

    It used to be public-named and documented-only, which is a poor combination:
    the way you find out you broke it is a hang, because ``asyncio.Lock`` is not
    reentrant and a second acquire from the same task simply never returns. A
    hang has no traceback and no failing line. The assertion turns the worst
    failure mode in this file into the most ordinary one.
    """
    session = StubSession(_ok_login())
    client = _client(srp_api, session)

    with pytest.raises(AssertionError, match="requires the caller to hold"):
        _run(client._login())

    # It refused before touching the network, so nothing half-happened.
    assert session.calls == []


def test_the_retried_request_carries_the_refreshed_token(srp_api):
    """Replaying the request with the dead token would just 401 again.

    The stub jar hands out a different token per login precisely so this can be
    asserted. With a jar that repeated one value, the assertion below held for a
    correct implementation and a broken one alike.
    """
    session = StubSession(
        _ok_login() | {DAILY_PATH: [(401, UNAUTHORIZED_BODY), (200, json.dumps(DAILY_PAYLOAD))]}
    )
    client = _client(srp_api, session)
    _run(client.daily_detail())

    first_attempt, retry = session.headers_for(DAILY_PATH)
    assert first_attempt["x-xsrf-token"] == "xsrf-first"
    assert retry["x-xsrf-token"] == "xsrf-second"
    # Two logins, two tokens: the jar was actually re-read rather than the
    # cached value reused.
    assert session.cookie_jar.issued == 2


def test_the_client_is_usable_again_after_a_recovery(srp_api):
    """Proves the lock was released rather than leaked on the re-login path."""
    session = StubSession(
        _ok_login() | {DAILY_PATH: [(401, UNAUTHORIZED_BODY), (200, json.dumps(DAILY_PAYLOAD))]}
    )
    client = _client(srp_api, session)

    _run(client.daily_detail())
    # The script's last pair repeats, so this second call is served 200.
    assert _run(client.daily_detail()) == DAILY_PAYLOAD
    # ...and it did not log in a third time: the recovered session is reused.
    assert session.count("login/authorize") == 2


def test_no_component_module_uses_home_assistants_shared_session(srp_api):
    """Home Assistant's shared session means a shared cookie jar, and SRP's
    whole session is one cookie in it.

    Swept over the whole component rather than one file. The earlier version of
    this guard read only ``coordinator.py``, so it passed green the entire time
    ``config_flow.py`` was still calling ``async_get_clientsession`` -- putting
    SRP's cookie in the shared jar on every setup and every reauth, which is
    exactly the arrangement the coordinator's comment exists to forbid. A rule
    enforced on one file is not a rule.

    Read as source text because importing these modules would require Home
    Assistant to be installed.
    """
    # Match the call, not the name: the comments in both files name the helper
    # they reject, and a test that forbade the word would forbid the explanation.
    offenders = sorted(
        path.name
        for path in COMPONENT.rglob("*.py")
        if re.search(r"\basync_get_clientsession\s*\(", path.read_text())
    )
    assert offenders == []


def test_the_integration_builds_its_own_session_everywhere_it_needs_one(srp_api):
    """The positive half: both places that construct a client use the private
    session helper, so the sweep above cannot be satisfied by deleting the call.
    """
    for name in ("coordinator.py", "config_flow.py"):
        source = (COMPONENT / name).read_text()
        assert "import async_create_clientsession" in source, name
        assert re.search(r"\basync_create_clientsession\s*\(\s*(self\.)?hass", source), name


def test_the_reauth_flow_can_actually_take_a_new_password(srp_api):
    """The reauth prompt is the only exit from ``ConfigEntryAuthFailed``.

    It was unreachable in practice: ``async_step_reauth`` delegated to
    ``async_step_user``, which ends in ``_abort_if_unique_id_configured()``, and
    the account being reauthenticated is by definition already configured -- so
    the flow aborted with "That account is already set up." before the user
    could type anything. Both the README and the troubleshooting guide tell
    people to use this prompt, so it has to exist.

    Source text again, for the same reason as the sweep above. The helper names
    are the ones Home Assistant 2026.8.3 actually defines
    (``homeassistant/config_entries.py``); this asserts we call those rather
    than something remembered.
    """
    source = (COMPONENT / "config_flow.py").read_text()
    config_class = next(node for node in ast.parse(source).body
                        if isinstance(node, ast.ClassDef) and node.name == "SRPEnergyMonitorConfigFlow")
    reauth_methods = [node for node in config_class.body if isinstance(node, ast.AsyncFunctionDef)
                      and node.name in {"async_step_reauth", "async_step_reauth_confirm"}]
    assert {node.name for node in reauth_methods} == {"async_step_reauth", "async_step_reauth_confirm"}
    reauth = "\n".join(ast.get_source_segment(source, node) for node in reauth_methods)

    assert re.search(r"async def async_step_reauth_confirm\b", reauth)
    assert re.search(r"\bself\._get_reauth_entry\s*\(\s*\)", reauth)
    assert re.search(r"\bself\.async_update_reload_and_abort\s*\(", reauth)
    # The step must not fall back into the create-entry path that aborted.
    assert "async_create_entry" not in reauth
    assert "_abort_if_unique_id_configured" not in reauth
    assert "self.async_step_user(" not in reauth


def test_every_flow_step_and_abort_reason_has_translated_text(srp_api):
    """A step with no strings entry renders as a raw key in the dialog.

    Checked against both files, and that they agree: ``strings.json`` is what
    Home Assistant's own tooling reads and ``translations/en.json`` is what the
    frontend actually shows, and a reauth prompt nobody can read is barely
    better than one that aborts.
    """
    source = (COMPONENT / "config_flow.py").read_text()
    classes = {node.name: ast.get_source_segment(source, node) for node in ast.parse(source).body
               if isinstance(node, ast.ClassDef)}
    config_source = classes["SRPEnergyMonitorConfigFlow"]
    options_source = classes["SRPEnergyMonitorOptionsFlow"]
    steps = set(re.findall(r'step_id="([a-z_]+)"', config_source))
    assert steps == {"user", "reauth_confirm"}, steps
    option_steps = set(re.findall(r'step_id="([a-z_]+)"', options_source))
    assert option_steps == {"init"}, option_steps
    option_fields = set(re.findall(r'vol\.Required\(\s*"([a-z_]+)"', options_source))
    assert option_fields == {"demand_audit_plan"}
    option_errors = set(re.findall(r'errors\[[^\]]+\]\s*=\s*"([a-z_]+)"', options_source))
    assert option_errors == {"invalid_plan"}

    files = {}
    for name in ("strings.json", "translations/en.json"):
        translations = json.loads((COMPONENT / name).read_text())
        config = translations["config"]
        options = translations["options"]
        files[name] = translations
        assert steps <= set(config["step"]), (name, config["step"].keys())
        # async_update_reload_and_abort defaults its reason to this.
        assert "reauth_successful" in config["abort"], name
        # Every error key the flow can hand back must resolve to a sentence.
        errors = set(re.findall(r'return "([a-z_]+)"', config_source)) | {"invalid_account"}
        assert errors <= set(config["error"]), (name, errors - set(config["error"]))
        assert option_steps <= set(options["step"]), name
        assert option_errors <= set(options["error"]), name
        assert all(options["error"][key].strip() for key in option_errors), name
        for step in option_steps:
            translated = options["step"][step]
            assert translated["title"].strip() and translated["description"].strip(), name
            assert option_fields <= set(translated["data"]), name
            assert all(translated["data"][key].strip() for key in option_fields), name

    # Every {placeholder} in the reauth text must be one the flow supplies, or
    # the dialog renders the literal braces at the user.
    supplied = set(re.findall(r'description_placeholders=\{"([a-z_]+)"', config_source))
    for name, translations in files.items():
        config = translations["config"]
        used = set(re.findall(r"\{([a-z_]+)\}", config["step"]["reauth_confirm"]["description"]))
        assert used <= supplied, (name, used - supplied)

    assert files["strings.json"] == files["translations/en.json"]
