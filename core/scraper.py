"""Safe, session-aware access to the UTaipei student portal.

The portal frequently returns an HTML login page with status code 200. This
module therefore treats transport status and page identity as separate
contracts. The application intentionally imports only the transcript path:
login and the validated AG102 PDF are the only live portal data used for
graduation analysis.
"""

import logging
import math
import multiprocessing
import re
import secrets
import time
import sys
from enum import Enum
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Windows can resolve the university's certificate chain with its system store.
# Keep certificate and hostname verification enabled in each spawned worker.
if sys.platform == 'win32':
    import truststore
    truststore.inject_into_ssl()

LOGIN_URL = "https://my.utaipei.edu.tw/utaipei/login_check.jsp"
PERCHK_URL = "https://my.utaipei.edu.tw/utaipei/perchk.jsp"
FNC_URL = "https://my.utaipei.edu.tw/utaipei/fnc.jsp"
BASE_URL = "https://my.utaipei.edu.tw/utaipei/"
PORTAL_HOST = "my.utaipei.edu.tw"
ALLOWED_PORTAL_HOSTS = frozenset({PORTAL_HOST})
MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_REDIRECT_HOPS = 3
MAX_PDF_CANDIDATES = 3
DEFAULT_OPERATION_TIMEOUT = 45.0
HARD_UI_TIMEOUT = 50.0
PROCESS_CLEANUP_GRACE = 2.0

# These aliases make the production budgets explicit for integration callers
# without coupling them to the implementation names above.
PORTAL_OPERATION_TIMEOUT_SECONDS = DEFAULT_OPERATION_TIMEOUT
PORTAL_HARD_DEADLINE_SECONDS = HARD_UI_TIMEOUT
PORTAL_CLEANUP_GRACE_SECONDS = PROCESS_CLEANUP_GRACE

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://my.utaipei.edu.tw/utaipei/index_main.html",
}

_MAINTENANCE_MARKERS = (
    "維護中",
    "暫停服務",
    "系統忙碌",
    "系統維護",
    "maintenance",
    "service unavailable",
    "temporarily unavailable",
    "system busy",
    "under maintenance",
)
_LOGIN_FORM_IDS = {"login", "loginform", "login_form", "login-form"}
_LOGIN_CONTEXT_MARKERS = ("登入", "sign in", "log in", "login page", "login form", "login portal")
_LOGGER = logging.getLogger(__name__)


class _OperationDeadline:
    """Monotonic deadline shared by every request and response reader."""

    def __init__(self, seconds, *, clock=None):
        try:
            duration = float(seconds)
        except (TypeError, ValueError):
            duration = 0.0
        if not math.isfinite(duration):
            duration = 0.0
        self._clock = clock or time.monotonic
        self.started_at = self._clock()
        self.deadline = self.started_at + max(0.0, duration)

    def elapsed(self):
        return max(0.0, self._clock() - self.started_at)

    def remaining(self):
        return self.deadline - self._clock()

    def ensure(self):
        remaining = self.remaining()
        if remaining <= 0:
            raise PortalError(PortalErrorCode.UPSTREAM_TIMEOUT)
        return remaining

    def request_timeout(self, requested):
        """Return bounded connect/read timeouts no longer than remaining time."""

        remaining = self.ensure()
        if isinstance(requested, (tuple, list)) and len(requested) >= 2:
            connect, read = requested[0], requested[1]
        else:
            connect = read = requested
        try:
            connect = float(connect)
        except (TypeError, ValueError):
            connect = 5.0
        try:
            read = float(read)
        except (TypeError, ValueError):
            read = 30.0
        connect = max(0.001, min(5.0, connect, remaining))
        read = max(0.001, min(10.0, read, remaining))
        return connect, read


def _log_portal_event(
    stage,
    *,
    result=None,
    code=None,
    elapsed=None,
    hop=None,
    candidate=None,
    correlation_id=None,
):
    """Emit only stable operational fields; never include request data."""

    if isinstance(code, PortalErrorCode):
        code = code.value
    elif code is not None:
        try:
            code = PortalErrorCode(str(code)).value
        except ValueError:
            code = PortalErrorCode.PORTAL_CHANGED.value
    fields = {
        "stage": str(stage),
        "result": str(result) if result is not None else "",
        "code": code or "",
        "elapsed_ms": int(max(0.0, float(elapsed or 0.0)) * 1000),
        "hop": int(hop) if hop is not None else "",
        "candidate": int(candidate) if candidate is not None else "",
        "correlation_id": str(correlation_id or ""),
    }
    _LOGGER.info(
        "portal stage=%(stage)s result=%(result)s code=%(code)s elapsed_ms=%(elapsed_ms)s "
        "hop=%(hop)s candidate=%(candidate)s correlation_id=%(correlation_id)s",
        fields,
    )


def _clean_page_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def is_maintenance_page(html_content):
    """Return true for known maintenance/interstitial page language."""

    if not html_content:
        return False
    soup = BeautifulSoup(str(html_content), "html.parser")
    text = _clean_page_text(soup.get_text(" ", strip=True)).lower()
    return any(marker.lower() in text for marker in _MAINTENANCE_MARKERS)


def _has_login_context(text):
    normalized = _clean_page_text(text).lower()
    if "登入" in normalized:
        return True
    return any(marker in normalized for marker in _LOGIN_CONTEXT_MARKERS[1:])


def is_login_page(html_content):
    """Identify a login page without mistaking authenticated hidden controls."""

    if not html_content:
        return False
    soup = BeautifulSoup(str(html_content), "html.parser")
    visible_text = _clean_page_text(soup.get_text(" ", strip=True))
    for form in soup.find_all("form"):
        action = str(form.get("action") or "").lower()
        form_id = str(form.get("id") or "").lower()
        if "login_check" in action or form_id in _LOGIN_FORM_IDS or form_id.startswith("login"):
            return True
        has_password_input = any(
            str(control.get("type") or "").lower() == "password" for control in form.find_all("input")
        )
        form_text = _clean_page_text(form.get_text(" ", strip=True))
        if has_password_input and (_has_login_context(form_text) or _has_login_context(visible_text)):
            return True
    return False

class PortalErrorCode(str, Enum):
    """Stable, non-sensitive failure categories exposed to the UI."""

    NETWORK_BLOCKED = "NETWORK_BLOCKED"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    SESSION_REJECTED = "SESSION_REJECTED"
    PORTAL_CHANGED = "PORTAL_CHANGED"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    PDF_NOT_FOUND = "PDF_NOT_FOUND"
    AUTH_REJECTED = "AUTH_REJECTED"
    RATE_LIMITED = "RATE_LIMITED"
    TRANSCRIPT_IDENTITY_MISMATCH = "TRANSCRIPT_IDENTITY_MISMATCH"
    TRANSCRIPT_VALIDATION_FAILED = "TRANSCRIPT_VALIDATION_FAILED"


_PORTAL_MESSAGES = {
    PortalErrorCode.NETWORK_BLOCKED: "校務系統目前無法連線，請改用 PDF 或稍後重試。",
    PortalErrorCode.CAPTCHA_REQUIRED: "校務系統要求人工驗證，請改用 PDF 上傳。",
    PortalErrorCode.SESSION_REJECTED: "校務系統登入狀態已失效，請重新登入或改用 PDF。",
    PortalErrorCode.PORTAL_CHANGED: "校務系統頁面結構或網址已變更，請改用 PDF 並通知維護者。",
    PortalErrorCode.UPSTREAM_TIMEOUT: "校務系統回應逾時，請稍後重試或改用 PDF。",
    PortalErrorCode.PDF_NOT_FOUND: "校務系統未提供可驗證的成績單 PDF，請改用 PDF 上傳。",
    PortalErrorCode.AUTH_REJECTED: "校務系統拒絕登入，請確認帳號密碼或改用 PDF。",
    PortalErrorCode.RATE_LIMITED: "校務系統暫時限制請求，請稍後再試。",
    PortalErrorCode.TRANSCRIPT_IDENTITY_MISMATCH: "成績單學號與登入帳號不一致，資料未套用；請確認登入帳號。",
    PortalErrorCode.TRANSCRIPT_VALIDATION_FAILED: "成績單內容或學分核對未完成，資料未套用；請改用正確的歷年成績單 PDF。",
}


class PortalError(RuntimeError):
    """An intentionally safe portal error with no response or credential text."""

    def __init__(self, code, message=None):
        try:
            normalized = code if isinstance(code, PortalErrorCode) else PortalErrorCode(str(code))
        except ValueError:
            normalized = PortalErrorCode.PORTAL_CHANGED
        self.code = normalized
        self.error_code = normalized
        self.message = message if message in _PORTAL_MESSAGES.values() else _PORTAL_MESSAGES[normalized]
        super().__init__(self.message)

    def __str__(self):
        return self.message


def _safe_portal_url(target, base_url=BASE_URL):
    """Resolve a portal URL and reject scheme/host changes before requesting."""

    candidate = urljoin(str(base_url or BASE_URL), str(target or ""))
    try:
        parsed = urlparse(candidate)
        port = parsed.port
    except ValueError:
        raise PortalError(PortalErrorCode.PORTAL_CHANGED) from None
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() not in ALLOWED_PORTAL_HOSTS
        or parsed.username
        or parsed.password
        or port not in (None, 443)
    ):
        raise PortalError(PortalErrorCode.PORTAL_CHANGED)
    return candidate


def _response_headers(response):
    headers = getattr(response, "headers", None)
    return headers if hasattr(headers, "get") else {}


def _response_url(response, fallback):
    value = getattr(response, "url", None)
    return str(value or fallback)


def _check_response_origin(response, request_url):
    """Reject a cross-domain redirect or final response, even when HTTP 200."""

    final_url = _safe_portal_url(_response_url(response, request_url), request_url)
    for previous in getattr(response, "history", ()) or ():
        _safe_portal_url(_response_url(previous, request_url), request_url)
        location = _response_headers(previous).get("Location") or _response_headers(previous).get("location")
        if location:
            _safe_portal_url(location, _response_url(previous, request_url))
    location = _response_headers(response).get("Location") or _response_headers(response).get("location")
    if location:
        _safe_portal_url(location, final_url)
    return final_url


def _map_request_exception(exc):
    if isinstance(exc, (requests.Timeout, TimeoutError)):
        return PortalError(PortalErrorCode.UPSTREAM_TIMEOUT)
    return PortalError(PortalErrorCode.NETWORK_BLOCKED)


def _is_login_page(html_content):
    return is_login_page(html_content)


def _is_captcha_page(html_content):
    """Detect human-verification interstitials without returning their markup."""

    if not html_content:
        return False
    soup = BeautifulSoup(str(html_content), "html.parser")
    visible_text = " ".join(soup.get_text(" ", strip=True).split()).lower()
    compact_html = str(html_content).lower()
    if any(marker in visible_text for marker in ("captcha", "recaptcha", "hcaptcha", "驗證碼", "challenge")):
        return True
    return any(
        marker in compact_html
        for marker in (
            "captcha",
            "recaptcha",
            "hcaptcha",
            'name="challenge"',
            "name='challenge'",
            'id="challenge"',
            "id='challenge'",
        )
    )


def _has_session_identity(html_content):
    soup = BeautifulSoup(str(html_content or ""), "html.parser")
    text = " ".join(soup.get_text(" ", strip=True).split()).lower()
    return any(marker in text for marker in ("校務系統", "主選單", "學生", "utaipei", "成績"))


def _has_fnc_identity(html_content, expected_fncid):
    soup = BeautifulSoup(str(html_content or ""), "html.parser")
    expected = str(expected_fncid or "").lower()
    if expected and expected in str(html_content or "").lower():
        return bool(soup.find("form"))
    for form in soup.find_all("form"):
        action = str(form.get("action") or "").lower()
        if expected and expected in action and form.get("id") == "thisform":
            return True
    return False


def _validate_transport(response, request_url):
    _check_response_origin(response, request_url)
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 403:
        raise PortalError(PortalErrorCode.NETWORK_BLOCKED)
    if status in {401, 419}:
        raise PortalError(PortalErrorCode.SESSION_REJECTED)
    if status == 429:
        raise PortalError(PortalErrorCode.RATE_LIMITED)
    if status in {408, 504}:
        raise PortalError(PortalErrorCode.UPSTREAM_TIMEOUT)
    if status != 200:
        raise PortalError(PortalErrorCode.NETWORK_BLOCKED)
    return response


def _header_value(response, name):
    """Read a response header from both normal and case-insensitive mappings."""

    wanted = str(name).lower()
    headers = _response_headers(response)
    items = getattr(headers, "items", None)
    if callable(items):
        for key, value in items():
            if str(key).lower() == wanted:
                return value
    else:
        value = headers.get(name)
        if value is not None:
            return value
    return ""


def _is_binary_pdf_response(response):
    """Identify response metadata that permits the capped PDF reader."""

    content_type = str(_header_value(response, "Content-Type") or "").lower()
    disposition = str(_header_value(response, "Content-Disposition") or "").lower()
    return (
        "pdf" in content_type
        or content_type in {"application/octet-stream", "binary/octet-stream"}
        or "attachment" in disposition
        or "filename=" in disposition
    )


def _read_response_text(response, *, deadline=None, correlation_id=None):
    """Read deferred response text without leaking transport exceptions."""

    try:
        if deadline is not None:
            deadline.ensure()
        text = getattr(response, "text", "") or ""
        if deadline is not None:
            deadline.ensure()
        return str(text)
    except PortalError:
        raise
    except (requests.Timeout, TimeoutError) as exc:
        del exc
        error = PortalError(PortalErrorCode.UPSTREAM_TIMEOUT)
        _log_portal_event(
            "response_text",
            result="error",
            code=error.code,
            elapsed=deadline.elapsed() if deadline is not None else 0,
            correlation_id=correlation_id,
        )
        raise error from None
    except requests.ConnectionError as exc:
        del exc
        error = PortalError(PortalErrorCode.NETWORK_BLOCKED)
        _log_portal_event(
            "response_text",
            result="error",
            code=error.code,
            elapsed=deadline.elapsed() if deadline is not None else 0,
            correlation_id=correlation_id,
        )
        raise error from None
    except Exception as exc:
        del exc
        error = PortalError(PortalErrorCode.PORTAL_CHANGED)
        _log_portal_event(
            "response_text",
            result="error",
            code=error.code,
            elapsed=deadline.elapsed() if deadline is not None else 0,
            correlation_id=correlation_id,
        )
        raise error from None


_SENSITIVE_REDIRECT_KEYS = frozenset(
    {
        "account",
        "cookie",
        "csrf",
        "idno",
        "password",
        "passwd",
        "pwd",
        "secret",
        "session",
        "student",
        "student_id",
        "token",
        "uid",
    }
)


def _contains_sensitive_post_data(data):
    """Return whether redirect replay could expose credentials or a token."""

    if data is None:
        return False
    if isinstance(data, dict):
        return any(str(key).strip().lower() in _SENSITIVE_REDIRECT_KEYS for key in data)
    # Unknown encoded request bodies are conservatively treated as sensitive.
    return bool(data)


def _close_response(response):
    """Release a redirect response before opening its next hop."""

    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _request_with_safe_redirects(
    session,
    method,
    url,
    *,
    referer=None,
    data=None,
    timeout=15,
    stream=False,
    deadline=None,
    correlation_id=None,
):
    """Issue one request while validating every redirect before following it."""

    target = _safe_portal_url(url)
    current_method = str(method or "GET").upper()
    current_data = data
    current_referer = referer
    for hop in range(MAX_REDIRECT_HOPS + 1):
        if deadline is not None:
            deadline.ensure()
        headers = {**HEADERS}
        if current_referer:
            headers["Referer"] = _safe_portal_url(current_referer)
        request_kwargs = {
            "headers": headers,
            "data": current_data,
            "timeout": deadline.request_timeout(timeout) if deadline is not None else timeout,
            "allow_redirects": False,
        }
        if stream:
            request_kwargs["stream"] = True
        try:
            response = getattr(session, current_method.lower())(target, **request_kwargs)
        except PortalError:
            raise
        except Exception as exc:
            raise _map_request_exception(exc) from None

        response_closed = False
        try:
            if deadline is not None:
                deadline.ensure()

            # Validate Location and final URL before inspecting status or issuing
            # another request.  This guarantees an evil origin receives no call.
            _check_response_origin(response, target)
            status = int(getattr(response, "status_code", 0) or 0)
            if status in {301, 302, 303, 307, 308}:
                try:
                    if hop >= MAX_REDIRECT_HOPS:
                        raise PortalError(PortalErrorCode.PORTAL_CHANGED)
                    location = _header_value(response, "Location")
                    if not location:
                        raise PortalError(PortalErrorCode.PORTAL_CHANGED)
                    next_target = _safe_portal_url(location, _response_url(response, target))
                    if status in {307, 308}:
                        if current_method in {"POST", "PUT", "PATCH"} and _contains_sensitive_post_data(current_data):
                            raise PortalError(PortalErrorCode.PORTAL_CHANGED)
                        next_method = current_method
                        next_data = current_data
                    else:
                        # Match browser/requests semantics for form POST redirects.
                        next_method = "GET" if current_method == "POST" else current_method
                        next_data = None if next_method == "GET" else current_data
                    current_referer = _response_url(response, target)
                    target = next_target
                    current_method = next_method
                    current_data = next_data
                finally:
                    _close_response(response)
                    response_closed = True
                continue
            return _validate_transport(response, target)
        except PortalError as exc:
            if not response_closed:
                _close_response(response)
            _log_portal_event(
                "request",
                result="error",
                code=exc.code,
                elapsed=deadline.elapsed() if deadline is not None else 0,
                hop=hop,
                correlation_id=correlation_id,
            )
            raise
        except Exception:
            if not response_closed:
                _close_response(response)
            raise PortalError(PortalErrorCode.PORTAL_CHANGED) from None
    raise PortalError(PortalErrorCode.PORTAL_CHANGED)


def _validate_html_identity(
    response,
    request_url,
    *,
    role,
    expected_fncid=None,
    deadline=None,
    correlation_id=None,
):
    """Apply the page identity guard to each HTTP 200 HTML response."""

    _validate_transport(response, request_url)
    headers = _response_headers(response)
    disposition = str(headers.get("Content-Disposition", "")).lower()
    if _is_binary_pdf_response(response) or "attachment" in disposition:
        return response
    html = _read_response_text(response, deadline=deadline, correlation_id=correlation_id)
    if not html.strip():
        raise PortalError(PortalErrorCode.PORTAL_CHANGED)
    if _is_captcha_page(html):
        raise PortalError(PortalErrorCode.CAPTCHA_REQUIRED)
    if role != "entry" and _is_login_page(html):
        raise PortalError(PortalErrorCode.SESSION_REJECTED)
    if is_maintenance_page(html):
        raise PortalError(PortalErrorCode.PORTAL_CHANGED)
    if role == "login":
        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form", id="thisform")
        error = soup.find("input", id="err")
        if error and str(error.get("value", "")).upper() == "Y":
            raise PortalError(PortalErrorCode.AUTH_REJECTED)
        if not form:
            raise PortalError(PortalErrorCode.AUTH_REJECTED)
    elif role == "session" and not _has_session_identity(html):
        raise PortalError(PortalErrorCode.PORTAL_CHANGED)
    elif role == "fnc" and not _has_fnc_identity(html, expected_fncid):
        raise PortalError(PortalErrorCode.PORTAL_CHANGED)
    return response


def _form_controls(form):
    """Collect named hidden/select/button controls without trusting markup."""

    values = {}
    for control in form.find_all(["input", "select", "button"]):
        if control.has_attr("disabled"):
            continue
        name = str(control.get("name") or "").strip()
        if not name:
            continue
        tag = control.name.lower()
        if tag == "select":
            options = [option for option in control.find_all("option") if not option.has_attr("disabled")]
            option = next((item for item in options if item.has_attr("selected")), None) or (options[0] if options else None)
            values[name] = str((option or {}).get("value", "") if option else "")
        elif tag == "button":
            values[name] = str(control.get("value", ""))
        else:
            input_type = str(control.get("type") or "hidden").lower()
            if input_type in {"submit", "button", "reset", "file"}:
                continue
            if input_type in {"checkbox", "radio"} and not control.has_attr("checked"):
                continue
            values[name] = str(control.get("value", ""))
    return values


def _find_form(response, *, form_id="thisform", deadline=None, correlation_id=None):
    soup = BeautifulSoup(
        _read_response_text(response, deadline=deadline, correlation_id=correlation_id),
        "html.parser",
    )
    return soup.find("form", id=form_id) or soup.find("form")


def _find_login_form(response, *, deadline=None, correlation_id=None):
    """Return the first form that explicitly accepts both login controls."""

    soup = BeautifulSoup(
        _read_response_text(response, deadline=deadline, correlation_id=correlation_id),
        "html.parser",
    )
    for form in soup.find_all("form"):
        names = {
            str(control.get("name") or "").strip().lower()
            for control in form.find_all(["input", "select", "button"])
        }
        if {"uid", "pwd"}.issubset(names):
            return form
    return None


class PortalClient:
    """One authenticated portal client; callers own its close lifecycle."""

    def __init__(
        self,
        uid,
        pwd,
        *,
        session_factory=None,
        session=None,
        timeout=15,
        deadline=None,
        operation_timeout=DEFAULT_OPERATION_TIMEOUT,
        correlation_id=None,
    ):
        self.uid = str(uid or "").strip()
        self.pwd = str(pwd or "")
        if session is not None:
            self._session_factory = lambda: session
        else:
            self._session_factory = session_factory or requests.Session
        self.timeout = timeout
        self.deadline = deadline or _OperationDeadline(operation_timeout)
        self.correlation_id = str(correlation_id or "")
        self.session = self._session_factory()
        self._logged_in = False
        self._last_response_url = None

    def close(self):
        session = self.session
        self.session = None
        self.uid = ""
        self.pwd = ""
        self._logged_in = False
        self._last_response_url = None
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
        return False

    def _request(self, method, url, *, referer=None, data=None, timeout=None, stream=False):
        self.deadline.ensure()
        return _request_with_safe_redirects(
            self.session,
            method,
            url,
            referer=referer,
            data=data,
            timeout=timeout or self.timeout,
            stream=stream,
            deadline=self.deadline,
            correlation_id=self.correlation_id,
        )

    def _post_form(self, response, *, fallback_url, data=None, referer=None, role="portal", stream=False):
        form = _find_form(response, deadline=self.deadline, correlation_id=self.correlation_id)
        if not form:
            raise PortalError(PortalErrorCode.PORTAL_CHANGED)
        action = _safe_portal_url(form.get("action") or fallback_url, _response_url(response, fallback_url))
        payload = _form_controls(form)
        payload.update(data or {})
        result = self._request(
            "POST",
            action,
            referer=referer or _response_url(response, fallback_url),
            data=payload,
            stream=stream,
        )
        self._last_response_url = _response_url(result, action)
        try:
            return _validate_html_identity(
                result,
                _response_url(result, action),
                role=role,
                deadline=self.deadline,
                correlation_id=self.correlation_id,
            )
        except Exception:
            _close_response(result)
            raise

    def login(self):
        if self._logged_in:
            return self
        if not self.uid or not self.pwd:
            self.close()
            raise PortalError(PortalErrorCode.AUTH_REJECTED)
        entry_response = None
        response = None
        try:
            entry_url = BASE_URL + "index_main.html"
            entry_response = self._request("GET", entry_url, referer=BASE_URL, timeout=self.timeout)
            _validate_html_identity(
                entry_response,
                entry_url,
                role="entry",
                deadline=self.deadline,
                correlation_id=self.correlation_id,
            )
            login_data = {
                "uid": self.uid,
                "pwd": self.pwd,
                "myway": "yes",
                "check_choice": "",
                "teach_roll": "",
                "std_dorm": "",
                "std_vote": "",
                "std_choice": "",
            }
            login_form = _find_login_form(
                entry_response,
                deadline=self.deadline,
                correlation_id=self.correlation_id,
            )
            if login_form is None:
                if BeautifulSoup(
                    _read_response_text(
                        entry_response,
                        deadline=self.deadline,
                        correlation_id=self.correlation_id,
                    ),
                    "html.parser",
                ).find("form"):
                    raise PortalError(PortalErrorCode.PORTAL_CHANGED)
                login_action = LOGIN_URL
            else:
                login_action = _safe_portal_url(
                    login_form.get("action") or LOGIN_URL,
                    _response_url(entry_response, entry_url),
                )
                login_data = _form_controls(login_form)
                login_data.update({"uid": self.uid, "pwd": self.pwd})
            entry_response_url = _response_url(entry_response, entry_url)
            response = self._request("POST", login_action, referer=entry_response_url, data=login_data)
            _close_response(entry_response)
            entry_response = None
            login_response_url = _response_url(response, login_action)
            _validate_html_identity(
                response,
                login_response_url,
                role="login",
                deadline=self.deadline,
                correlation_id=self.correlation_id,
            )
            # The successful login response supplies the actual per-session
            # action. A hard-coded perchk endpoint would break on a portal
            # change and could post to an attacker-controlled action.
            session_response = self._post_form(
                response,
                fallback_url=PERCHK_URL,
                referer=login_response_url,
                role="session",
            )
            _close_response(response)
            response = None
            _close_response(session_response)
            self._logged_in = True
            return self
        except PortalError:
            self.close()
            raise
        except Exception:
            self.close()
            raise PortalError(PortalErrorCode.PORTAL_CHANGED) from None
        finally:
            _close_response(entry_response)
            _close_response(response)

    def _create_fnc(self, fncid):
        self.deadline.ensure()
        response = self._request(
            "POST",
            FNC_URL,
            referer=self._last_response_url or PERCHK_URL,
            data={
                "fncid": fncid,
                "hid_type": "S",
                "hid_choice_check": "",
                "hid_teach_roll": "",
                "hid_dorm_room": "",
                "hid_std_vote": "",
                "hid_std_choice": "",
                "hid_atd": "0",
            },
        )
        response_url = _response_url(response, FNC_URL)
        self._last_response_url = response_url
        return _validate_html_identity(
            response,
            response_url,
            role="fnc",
            expected_fncid=fncid,
            deadline=self.deadline,
            correlation_id=self.correlation_id,
        )

    def fetch_transcript_pdf(self):
        if not self._logged_in:
            raise PortalError(PortalErrorCode.SESSION_REJECTED)
        fnc_response = None
        try:
            self.deadline.ensure()
            response = self._create_fnc("AG102")
            fnc_response = response
            fnc_response_url = _response_url(response, FNC_URL)
            result = self._post_form(
                response,
                fallback_url=BASE_URL + "ag_pro/ag102.jsp",
                referer=fnc_response_url,
                role="portal",
                stream=True,
            )
            return self._extract_pdf(result)
        except PortalError:
            raise
        except Exception:
            raise PortalError(PortalErrorCode.PDF_NOT_FOUND) from None
        finally:
            _close_response(fnc_response)

    def _extract_pdf(self, response):
        if _is_binary_pdf_response(response):
            return _read_pdf_response(
                response,
                deadline=self.deadline,
                correlation_id=self.correlation_id,
            )
        try:
            self.deadline.ensure()
            html = _read_response_text(
                response,
                deadline=self.deadline,
                correlation_id=self.correlation_id,
            )
            if _is_captcha_page(html):
                raise PortalError(PortalErrorCode.CAPTCHA_REQUIRED)
            if _is_login_page(html):
                raise PortalError(PortalErrorCode.SESSION_REJECTED)
            soup = BeautifulSoup(html, "html.parser")
            candidates = []
            seen_candidates = set()
            for element, attribute in (
                ("a", "href"),
                ("iframe", "src"),
                ("embed", "src"),
                ("object", "data"),
            ):
                for node in soup.find_all(element):
                    self.deadline.ensure()
                    value = str(node.get(attribute) or "").strip()
                    if not value:
                        continue
                    if value in seen_candidates:
                        continue
                    hint = " ".join(
                        [value, str(node.get_text(" ", strip=True)), str(node.get("title") or "")]
                    ).lower()
                    if ".pdf" in hint or "下載" in hint or "transcript" in hint or "download" in hint:
                        if len(candidates) >= MAX_PDF_CANDIDATES:
                            break
                        seen_candidates.add(value)
                        candidates.append(value)
                if len(candidates) >= MAX_PDF_CANDIDATES:
                    break
            if not candidates:
                raise PortalError(PortalErrorCode.PDF_NOT_FOUND)
            for candidate_index, candidate in enumerate(candidates, start=1):
                self.deadline.ensure()
                _log_portal_event(
                    "candidate",
                    result="attempt",
                    elapsed=self.deadline.elapsed(),
                    candidate=candidate_index,
                    correlation_id=self.correlation_id,
                )
                pdf_url = _safe_portal_url(candidate, _response_url(response, BASE_URL))
                pdf_response = None
                try:
                    pdf_response = self._request(
                        "GET",
                        pdf_url,
                        referer=_response_url(response, BASE_URL),
                        timeout=25,
                        stream=True,
                    )
                    if _is_binary_pdf_response(pdf_response):
                        return _read_pdf_response(
                            pdf_response,
                            deadline=self.deadline,
                            correlation_id=self.correlation_id,
                        )
                    linked_html = _read_response_text(
                        pdf_response,
                        deadline=self.deadline,
                        correlation_id=self.correlation_id,
                    )
                    self.deadline.ensure()
                    if _is_login_page(linked_html):
                        raise PortalError(PortalErrorCode.SESSION_REJECTED)
                finally:
                    # Binary responses are already owned and closed by
                    # _read_pdf_response.  This branch owns only the
                    # non-binary candidate response, so it closes exactly
                    # once even when the candidate is an error/login page.
                    if pdf_response is not None and not _is_binary_pdf_response(pdf_response):
                        _close_response(pdf_response)
            raise PortalError(PortalErrorCode.PDF_NOT_FOUND)
        finally:
            # A non-binary AG102 wrapper belongs to this extraction call.  It
            # must not remain open while linked candidates are inspected.
            _close_response(response)

def _validated_pdf(content):
    if not content.startswith(b"%PDF-"):
        raise PortalError(PortalErrorCode.PDF_NOT_FOUND)
    if len(content) > MAX_PDF_BYTES:
        raise PortalError(PortalErrorCode.PDF_NOT_FOUND)
    return content


def _read_pdf_response(response, *, deadline=None, correlation_id=None):
    """Read a PDF response with a hard cap before materializing its bytes."""
    try:
        if deadline is not None:
            deadline.ensure()
        content_length = str(_header_value(response, "Content-Length") or "").strip()
        try:
            declared_length = int(content_length) if content_length else None
        except (TypeError, ValueError):
            declared_length = None
        if declared_length is not None and declared_length > MAX_PDF_BYTES:
            raise PortalError(PortalErrorCode.PDF_NOT_FOUND)

        iterator = getattr(response, "iter_content", None)
        if callable(iterator):
            chunks = []
            total = 0
            try:
                stream = iterator(chunk_size=64 * 1024)
            except TypeError:
                # Keep compatibility with small response doubles and old clients;
                # the requests product path accepts chunk_size above.
                stream = iterator()
            for chunk in stream:
                if deadline is not None:
                    deadline.ensure()
                if not chunk:
                    continue
                piece = bytes(chunk)
                total += len(piece)
                if total > MAX_PDF_BYTES:
                    raise PortalError(PortalErrorCode.PDF_NOT_FOUND)
                chunks.append(piece)
            if deadline is not None:
                deadline.ensure()
            return _validated_pdf(b"".join(chunks))

        # Legacy response doubles may expose only ``content``.  This fallback is
        # deliberately unreachable for the streaming requests product path.
        content = bytes(getattr(response, "content", b"") or b"")
        if deadline is not None:
            deadline.ensure()
        return _validated_pdf(content)
    except PortalError:
        raise
    except Exception as exc:
        mapped = _map_request_exception(exc)
        if mapped.code == PortalErrorCode.UPSTREAM_TIMEOUT:
            _log_portal_event(
                "pdf_read",
                result="error",
                code=mapped.code,
                elapsed=deadline.elapsed() if deadline is not None else 0,
                correlation_id=correlation_id,
            )
            raise mapped from None
        raise
    finally:
        _close_response(response)


def _start_session(uid, pwd):
    """Compatibility helper returning a logged-in Session owned by the caller."""

    client = PortalClient(uid, pwd)
    try:
        client.login()
    except Exception:
        client.close()
        raise
    return client.session


def _create_fnc_session(session, fncid):
    """Compatibility helper for callers that already own a verified Session."""

    target = _safe_portal_url(FNC_URL)
    response = _request_with_safe_redirects(
        session,
        "POST",
        target,
        referer=PERCHK_URL,
        data={
            "fncid": fncid,
            "hid_type": "S",
            "hid_choice_check": "",
            "hid_teach_roll": "",
            "hid_dorm_room": "",
            "hid_std_vote": "",
            "hid_std_choice": "",
            "hid_atd": "0",
        },
        timeout=15,
    )
    _validate_html_identity(response, _response_url(response, target), role="fnc", expected_fncid=fncid)
    return response


def _submit_portal_form(session, action_path, form_data, referer):
    """Compatibility helper with same-host action and response checks."""

    target = _safe_portal_url(action_path, referer or BASE_URL)
    response = _request_with_safe_redirects(
        session,
        "POST",
        target,
        referer=referer or BASE_URL,
        data=form_data,
        timeout=20,
    )
    _validate_html_identity(response, _response_url(response, target), role="portal")
    return response


def _fetch_transcript_inline(
    uid,
    pwd,
    *,
    operation_timeout=DEFAULT_OPERATION_TIMEOUT,
    correlation_id=None,
    session_factory=None,
    session=None,
):
    """Run one portal operation inside the caller's already-supervised process."""

    deadline = _OperationDeadline(operation_timeout)
    client = PortalClient(
        uid,
        pwd,
        deadline=deadline,
        operation_timeout=operation_timeout,
        correlation_id=correlation_id,
        session_factory=session_factory,
        session=session,
    )
    try:
        client.login()
        deadline.ensure()
        content = client.fetch_transcript_pdf()
        deadline.ensure()
        return _validated_pdf(bytes(content or b""))
    finally:
        client.close()


def _send_worker_result(connection, payload):
    """Best-effort stable result transfer; never expose an exception string."""

    try:
        connection.send(payload)
    except (BrokenPipeError, EOFError, OSError):
        pass


def _transcript_process_worker(connection, operation_timeout, correlation_id):
    """Spawn target; credentials arrive only through the private pipe."""

    account = None
    password = None
    try:
        deadline = _OperationDeadline(operation_timeout)
        wait_for_input = max(0.001, min(5.0, deadline.ensure()))
        if not connection.poll(wait_for_input):
            raise PortalError(PortalErrorCode.UPSTREAM_TIMEOUT)
        payload = connection.recv()
        if not isinstance(payload, tuple) or len(payload) != 3 or payload[0] != "credentials":
            raise PortalError(PortalErrorCode.PORTAL_CHANGED)
        account, password = payload[1], payload[2]
        if not isinstance(account, str) or not isinstance(password, str):
            raise PortalError(PortalErrorCode.AUTH_REJECTED)
        content = _fetch_transcript_inline(
            account,
            password,
            operation_timeout=operation_timeout,
            correlation_id=correlation_id,
        )
        _send_worker_result(connection, ("success", content))
        _log_portal_event(
            "worker",
            result="success",
            elapsed=deadline.elapsed(),
            correlation_id=correlation_id,
        )
    except PortalError as exc:
        _send_worker_result(connection, ("error", exc.code.value))
        _log_portal_event(
            "worker",
            result="error",
            code=exc.code,
            elapsed=deadline.elapsed() if "deadline" in locals() else 0,
            correlation_id=correlation_id,
        )
    except BaseException:
        _send_worker_result(connection, ("error", PortalErrorCode.PORTAL_CHANGED.value))
        _log_portal_event("worker", result="error", code=PortalErrorCode.PORTAL_CHANGED, correlation_id=correlation_id)
    finally:
        account = None
        password = None
        try:
            connection.close()
        except Exception:
            pass


def _process_entry(worker_target, connection, operation_timeout, correlation_id):
    """Contain even injected worker failures behind the stable result seam."""

    try:
        worker_target(connection, operation_timeout, correlation_id)
    except PortalError as exc:
        _send_worker_result(connection, ("error", exc.code.value))
    except BaseException:
        _send_worker_result(connection, ("error", PortalErrorCode.PORTAL_CHANGED.value))
    finally:
        try:
            connection.close()
        except Exception:
            pass


def _valid_operation_timeout(value):
    try:
        duration = float(value)
    except (TypeError, ValueError):
        duration = 0.0
    if not math.isfinite(duration) or duration <= 0:
        raise PortalError(PortalErrorCode.UPSTREAM_TIMEOUT)
    return duration


def _drain_worker_result(connection):
    try:
        if connection.poll(0):
            return connection.recv()
    except (EOFError, OSError):
        return None
    return None


def _terminate_worker(process, hard_deadline):
    """Terminate, join and (if needed) kill a child before closing its pipe."""

    try:
        if process.is_alive():
            process.terminate()
            remaining = max(0.0, hard_deadline - time.monotonic())
            process.join(timeout=min(0.25, remaining))
        if process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(timeout=0.25)
        if not process.is_alive():
            process.close()
    except (OSError, ValueError):
        pass


def _supervised_fetch_transcript(uid, pwd, *, operation_timeout, hard_timeout, worker_target=None):
    """Run the complete portal operation in a killable spawn child."""

    operation_timeout = _valid_operation_timeout(operation_timeout)
    try:
        hard_timeout = max(operation_timeout, float(hard_timeout))
    except (TypeError, ValueError):
        hard_timeout = operation_timeout
    if not math.isfinite(hard_timeout):
        hard_timeout = operation_timeout

    started_at = time.monotonic()
    soft_deadline = started_at + operation_timeout
    hard_deadline = started_at + hard_timeout
    correlation_id = secrets.token_hex(8)
    context = None
    parent_connection = None
    child_connection = None
    process = None
    child_result = None
    timed_out = False
    try:
        try:
            context = multiprocessing.get_context("spawn")
            parent_connection, child_connection = context.Pipe(duplex=True)
            target = worker_target or _transcript_process_worker
            process = context.Process(
                target=_process_entry,
                args=(target, child_connection, operation_timeout, correlation_id),
                daemon=True,
            )
            process.start()
            child_connection.close()
            child_connection = None
        except (AttributeError, OSError, RuntimeError, TypeError):
            _log_portal_event(
                "supervisor",
                result="startup_error",
                code=PortalErrorCode.NETWORK_BLOCKED,
                elapsed=time.monotonic() - started_at,
                correlation_id=correlation_id,
            )
            raise PortalError(PortalErrorCode.NETWORK_BLOCKED) from None

        _log_portal_event("supervisor", result="started", correlation_id=correlation_id)
        parent_connection.send(("credentials", str(uid or ""), str(pwd or "")))

        while child_result is None and time.monotonic() < soft_deadline:
            remaining = max(0.0, soft_deadline - time.monotonic())
            if parent_connection.poll(min(0.05, remaining)):
                child_result = parent_connection.recv()
                break
            if not process.is_alive():
                child_result = _drain_worker_result(parent_connection)
                break

        # Keep polling for the bounded cleanup grace.  This permits a worker
        # that completed at the inner deadline to transfer its stable result.
        while child_result is None and time.monotonic() < hard_deadline:
            remaining = max(0.0, hard_deadline - time.monotonic())
            if parent_connection.poll(min(0.05, remaining)):
                child_result = parent_connection.recv()
                break
            if not process.is_alive():
                child_result = _drain_worker_result(parent_connection)
                break

        if child_result is None:
            timed_out = True
            _log_portal_event(
                "supervisor",
                result="timeout",
                code=PortalErrorCode.UPSTREAM_TIMEOUT,
                elapsed=time.monotonic() - started_at,
                correlation_id=correlation_id,
            )
            raise PortalError(PortalErrorCode.UPSTREAM_TIMEOUT)
    except PortalError:
        raise
    except (BrokenPipeError, EOFError, OSError):
        raise PortalError(PortalErrorCode.NETWORK_BLOCKED) from None
    finally:
        # No join happens before result polling above.  Once polling ends, stop
        # the child and close both ends even when the worker ignored the pipe.
        if process is not None:
            if child_result is not None and process.is_alive():
                remaining = max(0.0, hard_deadline - time.monotonic())
                try:
                    process.join(timeout=min(PROCESS_CLEANUP_GRACE, remaining))
                except (OSError, ValueError):
                    pass
            _terminate_worker(process, hard_deadline)
        if child_connection is not None:
            try:
                child_connection.close()
            except Exception:
                pass
        if parent_connection is not None:
            try:
                parent_connection.close()
            except Exception:
                pass
        # Do not retain credentials in the supervisor frame after cleanup.
        uid = ""
        pwd = ""

    if timed_out:
        raise PortalError(PortalErrorCode.UPSTREAM_TIMEOUT)
    if not isinstance(child_result, (tuple, list)) or len(child_result) != 2:
        raise PortalError(PortalErrorCode.NETWORK_BLOCKED)
    result_kind, result_value = child_result
    if result_kind == "success":
        if not isinstance(result_value, bytes):
            raise PortalError(PortalErrorCode.PDF_NOT_FOUND)
        return _validated_pdf(result_value)
    if result_kind == "error":
        try:
            code = PortalErrorCode(result_value)
        except (TypeError, ValueError):
            code = PortalErrorCode.PORTAL_CHANGED
        raise PortalError(code)
    raise PortalError(PortalErrorCode.PORTAL_CHANGED)


def fetch_transcript(
    uid,
    pwd,
    *,
    operation_timeout=DEFAULT_OPERATION_TIMEOUT,
    hard_timeout=None,
    worker_target=None,
    timeout=None,
):
    """Authenticate once and return only validated transcript PDF bytes.

    Production calls always use a ``spawn`` child.  ``worker_target`` and the
    explicit timeout arguments are private testing seams; credentials are sent
    to the child only through its in-memory pipe.
    """

    if timeout is not None:
        operation_timeout = timeout
    operation_timeout = _valid_operation_timeout(operation_timeout)
    if hard_timeout is None:
        if operation_timeout == DEFAULT_OPERATION_TIMEOUT:
            hard_timeout = HARD_UI_TIMEOUT
        else:
            hard_timeout = operation_timeout + min(PROCESS_CLEANUP_GRACE, max(0.05, operation_timeout))
    return _supervised_fetch_transcript(
        uid,
        pwd,
        operation_timeout=operation_timeout,
        hard_timeout=hard_timeout,
        worker_target=worker_target,
    )


def crawl_transcript_pdf(uid, pwd, download_dir=None):
    """Backward-compatible one-shot transcript PDF fetch returning bytes.

    download_dir is retained for callers from the earlier API. The current
    implementation never writes credentials or transcript bytes to a shared
    filesystem, so the argument is intentionally ignored.
    """

    del download_dir
    return fetch_transcript(uid, pwd)
