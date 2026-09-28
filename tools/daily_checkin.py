#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""anyrouter.top / agentrouter.org 每日自动签到。

为什么不用纯 HTTP 方案
----------------------
历史上试过纯 HTTP（requests 直接打 API），实测（2026-08-24）走不通：

- ``anyrouter.top`` 挂在阿里云 ESA 后面，**任意路径**都返回 JS 挑战页
  （``var arg1='...'`` + ``acw_tc``/``cdn_sec_tc``），requests 根本到不了 API。
- ``agentrouter.org`` 的 API 可达，但 ``/api/user/self`` 对没有放行记录的
  上下文要人工滑块，纯 HTTP 过不去。
- 两个站都必须走代理（直连分别 SSLError / ConnectTimeout）。

而无头 Chromium 走代理访问后，两站的挑战都会自动通过（写入 ``acw_sc__v2``），
之后用 ``page.request`` 打 API 就能拿到结构化 JSON。两个站都是 new-api 系，
但签到动作不同：AnyRouter 调 ``POST /api/user/sign_in``，AgentRouter 调
``/api/user/self`` 即会触发签到。请求还需要前端使用的 ``New-Api-User`` 头。

所以这里的分工是：**浏览器负责过反爬 + 持有登录态，签到和校验走 API**。
刻意不去点页面按钮、不抓 DOM——登录后的 UI 会变，写死选择器等于赌。

AnyRouter 日常只需要配置每个账号的 ``session`` Cookie（可选 ``api_user``）。
WAF Cookie 由脚本每次用临时浏览器获取，不依赖账号 profile。
AgentRouter 用 ``username`` / ``password`` 表单登录；持久化 profile 保存
WAF 放行 Cookie 和上一次的会话（预检要用它确认今天是否已签到）。

用法
----
日常（任务计划每天跑一次）::

    python tools/daily_checkin.py
    python tools/daily_checkin.py --only agentrouter-1 --verbose

只有 AgentRouter 的 profile 丢失或要求人工滑块时，才需要手工登录一次::

    python tools/daily_checkin.py --login agentrouter-1

退出码：全部完成（成功 / 已签过 / 已提交未核对）0；有账号需要处理 1；
配置错误或使用了已废弃参数 2。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "tools" / "checkin_sites.json"
DEFAULT_PROFILE_ROOT = ROOT / "data" / "checkin_profiles"
DEFAULT_LOG = ROOT / "logs" / "daily_checkin.log"

_ANYROUTER_WAF_COOKIES = frozenset(("acw_tc", "cdn_sec_tc", "acw_sc__v2"))
_ANYROUTER_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"
)
_INTER_ACCOUNT_DELAY_SEC = 5
# 阿里云滑块是间歇性、会话级的：同一 IP 稍后重来常常就过了（实测当天手动重跑能过）。
# 撞上时重新预热再打几次，别一撞就判失败。次数克制：试太多只会给限流计数续命。
_WAF_REWARM_RETRIES = 2
# 单次预热最多导航几次。按时间预算循环时，settle 很短就会连着导航十几次，
# 这正是给 ESA 限流计数续命的做法。
_WAF_MAX_NAVIGATIONS = 3
# 没给 --notify-url 时从这个环境变量取 webhook，方便任务计划不在命令行里写 URL。
_NOTIFY_ENV = "CHECKIN_NOTIFY_URL"

# 结果分类。区分「已经签过」和「真失败」很重要：前者是每天第二次跑的正常结果，
# 不该让任务计划报红。
OK = "ok"
ALREADY = "already"
NEED_LOGIN = "need_login"
FAILED = "failed"
# 无法在保留现有登录态的前提下确认今天是否签到，不能冒险退出重登。
CHECKIN_UNKNOWN = "checkin_unknown"
# 表单登录已成功、签到通常也已由登录后控制台自发的 /api/user/self 触发，
# 只是用来核对的那次请求被阿里云滑块挡了，读不到余额。这既不是「需要重新登录」
# （凭据没问题），也不该当失败报红——单列一个「已提交但没核对上」的状态。
UNVERIFIED = "unverified"

_STATUS_LABEL = {
    OK: "签到成功",
    ALREADY: "今天已签过",
    NEED_LOGIN: "需要人工重新登录",
    CHECKIN_UNKNOWN: "签到状态未知（未执行重登）",
    UNVERIFIED: "签到已提交（未能核对）",
    FAILED: "失败",
}

# 站点把「今天已经签过」当成一种错误返回，措辞各家不同，这里按特征词归类。
_ALREADY_MARKERS = ("已签到", "已经签到", "已签", "重复签到", "already", "checked in")


@dataclass
class SiteConfig:
    name: str
    base_url: str
    kind: str = "refresh"          # refresh | relogin
    profile: str = ""
    proxy: str = ""
    checkin_path: str = "/api/user/sign_in"
    checkin_method: str = "POST"
    self_path: str = "/api/user/self"
    api_user_header: str = "New-Api-User"
    cookies: dict[str, str] | str | None = None
    # AgentRouter：表单登录凭据。这个站每天要退出重登才计签到，
    # 所以每次跑都重新登录一遍（profile 只留 WAF 放行 Cookie 和上次的会话）。
    username: str = ""
    password: str = ""
    api_user: str = ""
    session_cookie: str = ""
    waf_path: str = "/login"
    # ESA 的 JS 挑战通常几秒内跑完，但首次访问偶尔要十几秒；
    # 单独成字段是为了让测试能把它压到 0，而不是硬等 30 秒。
    waf_budget_ms: int = 30000
    waf_cookie_names: list[str] = field(
        default_factory=lambda: sorted(_ANYROUTER_WAF_COOKIES)
    )
    oauth_path: str = ""           # 历史字段（OAuth 入口），已不再使用，保留以兼容旧配置
    console_path: str = "/console"
    login_url: str = ""            # 留空则用 base_url + console_path
    # 用**真实安装的**浏览器而不是 Playwright 自带的 Chromium：
    # 自带的 Chromium 报 plugins=0、版本也旧，hCaptcha 之类的人机验证一眼就能认出来。
    # 登录与日常跑必须用同一个通道，否则 profile 格式不通。
    channel: str = "msedge"        # msedge | chrome
    enabled: bool = True
    timeout_ms: int = 45000
    settle_ms: int = 3500          # 留给 JS 挑战跑完的时间
    # 由 load_sites 填入，不是配置字段：提示信息里要写实际在用的配置路径。
    config_path: str = field(default="", repr=False, compare=False)

    @property
    def config_label(self) -> str:
        return self.config_path or str(DEFAULT_CONFIG)

    @property
    def profile_dir(self) -> Path:
        return DEFAULT_PROFILE_ROOT / (self.profile or self.name)

    @property
    def login_target(self) -> str:
        return self.login_url or (self.base_url.rstrip("/") + self.console_path)


@dataclass
class CheckinResult:
    name: str
    status: str
    detail: str = ""
    quota: str = ""

    @property
    def is_problem(self) -> bool:
        """状态未知也要报警，但不能因此破坏现有登录态。"""
        return self.status in (NEED_LOGIN, FAILED, CHECKIN_UNKNOWN)

    def line(self) -> str:
        label = _STATUS_LABEL.get(self.status, self.status)
        parts = [f"{self.name}: {label}"]
        if self.quota:
            parts.append(f"余额 {self.quota}")
        if self.detail:
            parts.append(self.detail)
        return " | ".join(parts)


# --------------------------------------------------------------------------- 配置

def normalize_login_config(
    username: str,
    password: str,
    oauth_path: str,
) -> tuple[str, str, str]:
    """纠正「把 邮箱/密码 填进了 oauth_path」这种误配。

    ``oauth_path`` 存的是 OAuth 入口路径（如 ``/oauth/linuxdo``），一定以 ``/``
    开头。实际配置时踩过一次：把 ``邮箱/密码`` 填了进去。凭据里必然有 ``@``、
    且不以 ``/`` 开头，据此认出来并搬到 ``username`` / ``password``。

    密码本身可能含 ``/``，所以按**第一个** ``/`` 切。
    已经填了 username/password 的不动，避免覆盖填对的配置。
    """
    if username or password:
        return username, password, oauth_path
    raw = (oauth_path or "").strip()
    if raw and not raw.startswith("/") and "@" in raw and "/" in raw:
        email, _, secret = raw.partition("/")
        if email.strip() and secret:
            return email.strip(), secret, ""
    return username, password, oauth_path


def load_sites(config_path: Path, *, allow_placeholders: bool = False) -> list[SiteConfig]:
    """读配置。用 utf-8-sig：Windows 记事本存的 JSON 常带 BOM，踩过一次。

    ``allow_placeholders`` 只给测试用：示例配置本身就是占位内容，
    要能单独校验它的结构。
    """
    if not config_path.is_file():
        raise SystemExit(
            f"找不到配置 {config_path}\n"
            f"先从 {config_path.with_name(config_path.stem + '.example.json')} 复制一份并填好"
        )
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, list):
        raise SystemExit("配置应当是一个数组，每个账号一条")

    sites: list[SiteConfig] = []
    # config_path 由这里填，不允许从 JSON 里设置
    known = {f.name for f in SiteConfig.__dataclass_fields__.values()} - {"config_path"}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SystemExit(f"第 {index + 1} 条配置不是对象")
        unknown = set(item) - known
        if unknown:
            # 拼错字段名却静默忽略，是最难查的一类配置问题
            raise SystemExit(f"第 {index + 1} 条配置有无法识别的字段: {sorted(unknown)}")
        for required in ("name", "base_url"):
            if not str(item.get(required) or "").strip():
                raise SystemExit(f"第 {index + 1} 条配置缺少 {required}")
        site = SiteConfig(**item)
        site.config_path = str(config_path)
        if site.enabled and not allow_placeholders:
            _reject_placeholders(site, item)
        fixed_user, fixed_pass, fixed_oauth = normalize_login_config(
            site.username, site.password, site.oauth_path
        )
        if (fixed_user, fixed_pass) != (site.username, site.password):
            print(f"  {site.name}: [配置提醒] 登录凭据应填在 username / password，"
                  f"oauth_path 只存 OAuth 路径；本次已自动纠正")
            site.username, site.password, site.oauth_path = (
                fixed_user, fixed_pass, fixed_oauth
            )
        if site.kind not in ("refresh", "relogin"):
            raise SystemExit(f"{site.name}: kind 只能是 refresh 或 relogin")
        if site.checkin_method.upper() not in ("GET", "POST"):
            raise SystemExit(f"{site.name}: checkin_method 只能是 GET 或 POST")
        sites.append(site)
    return sites


# 示例配置 / 文档里的占位说明。照抄模板忘了改，脚本会拿占位串当 session 或
# 用户 ID 去请求，报出来的是「会话已失效」「api_user 不匹配」，很难联想到没填。
_PLACEHOLDER_MARKERS = ("这里填",)
_PLACEHOLDER_VALUES = frozenset((
    "AgentRouter 用户 ID",
    "邮箱或用户名",
    "密码",
    "真实 session Cookie 值",
    "同上，第二个账号的 session 值",
    "SESSION_VALUE",
))


def _reject_placeholders(site: SiteConfig, item: dict[str, Any]) -> None:
    """启用的账号里还留着示例占位内容就直接报错，而不是带着它去请求。"""
    values: list[tuple[str, str]] = [
        (key, value) for key, value in item.items() if isinstance(value, str)
    ]
    values.extend(
        (f"cookies.{name}", value)
        for name, value in parse_cookies(item.get("cookies")).items()
    )
    for key, value in values:
        text = value.strip()
        if text in _PLACEHOLDER_VALUES or any(m in text for m in _PLACEHOLDER_MARKERS):
            raise SystemExit(
                f"{site.name}: {key} 还是示例占位内容（{text[:24]}），"
                f"请填入真实值，或把该账号设为 \"enabled\": false"
            )


# --------------------------------------------------------------------------- 判定

# new-api 鉴权中间件的 401 文案。两类原因的处置相反：会话失效 = 重登就好；
# New-Api-User 不匹配 = 会话其实有效、是 api_user 配错了，重登救不了还会丢登录态。
_API_USER_ERROR_MARKERS = ("new-api-user",)
_SESSION_EXPIRED_MARKERS = (
    "未登录", "access token 无效", "token 无效", "登录已过期",
    "not logged in", "invalid token", "token expired",
)
AUTH_EXPIRED = "expired"
AUTH_API_USER = "api_user"


def auth_failure_kind(message: str) -> str:
    """把鉴权失败的 message 归成 ``expired`` / ``api_user`` / ``""``（认不出）。"""
    lowered = str(message or "").lower()
    # 先认 api_user：它和会话失效的文案都以「无权进行此操作」开头，先判才不会混
    if any(marker in lowered for marker in _API_USER_ERROR_MARKERS):
        return AUTH_API_USER
    if any(marker in lowered for marker in _SESSION_EXPIRED_MARKERS):
        return AUTH_EXPIRED
    return ""


def _json_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    message = str(payload.get("message") or payload.get("msg") or "")
    if isinstance(payload.get("error"), dict):
        message = message or str(payload["error"].get("message") or "")
    return message


def classify_response(
    status_code: int,
    body: str,
    *,
    allow_already: bool = True,
) -> tuple[str, str]:
    """把一次签到请求的响应归类成 (状态, 说明)。

    单独抽出来是为了能脱离网络测试——签到脚本最容易错的就是这段判定。
    ``allow_already=False`` 给只读的 ``/api/user/self`` 用：那一步不是签到，
    响应里碰巧出现「already」之类的字样不能被当成「今天已签过」。
    """
    text = str(body or "").strip()

    payload: Any = None
    try:
        payload = json.loads(text)
    except Exception:
        payload = None
    is_json = payload is not None
    message = _json_message(payload)
    success = payload.get("success") if isinstance(payload, dict) else None

    if status_code == 401:
        if auth_failure_kind(message) == AUTH_API_USER:
            return FAILED, f"api_user 缺失或与登录用户不匹配，检查配置: {message[:60]}"
        return NEED_LOGIN, "会话已失效" + (f": {message[:60]}" if message else "")
    if status_code in (403, 429):
        return FAILED, "WAF/限流未通过，请稍后重试（不是重新登录）"
    if status_code >= 500:
        return FAILED, f"服务端错误 HTTP {status_code}"
    # ESA 会用 HTTP 200 返回验证页，不挡掉就会把整页 HTML 当成业务响应吐出来。
    # 放在状态码判定之后：403 的 body 里也有 aliyun_wa，那种情况上面已经处理过了。
    if is_waf_challenge_page(body):
        return FAILED, "阿里云 WAF 挑战未通过（返回的是验证页）"

    # 「已签过」只看 JSON 的 message：整段 body 里有用户名、分组名等任意内容，
    # 按原文匹配会被 "already_xxx" 这类字段值误伤。不是 JSON 时才退回看原文。
    probe = message if is_json else text
    if allow_already and any(marker in probe.lower() for marker in _ALREADY_MARKERS):
        return ALREADY, message[:60] if is_json else text[:60]

    if success is True:
        return OK, message[:60]
    if success is False:
        return FAILED, message[:60] or f"HTTP {status_code}"
    if status_code == 200 and payload is not None:
        # 有 JSON 但没有 success 字段：当成功处理，把原文带上便于人工核对
        return OK, text[:60]
    if status_code == 404:
        return FAILED, "签到端点不存在，检查 checkin_path"
    return FAILED, f"HTTP {status_code} {text[:60]}"


def extract_quota(body: str) -> str:
    """从 /api/user/self 里取一个能看出「签到有没有加上」的数字。"""
    try:
        data = json.loads(body).get("data") or {}
    except Exception:
        return ""
    for key in ("quota", "balance", "remain_quota"):
        if key in data:
            value = data[key]
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                # new-api 的 quota 是按 500000 = $1 存的。数值一律按美元带 $ 输出：
                # 外部的 claude-router/pick_key.ps1 按「余额 ([0-9.]+)\$」读日志，
                # 小额度不带 $ 会被当成「日志里查不到」。
                return f"{value / 500000:.2f}$"
            return str(value)
    return ""


def quota_amount(body: str) -> Optional[float]:
    """取 /api/user/self 里的原始额度数值，用来比对签到前后的变化。

    和 ``extract_quota`` 分开：那个是给人看的展示串（已经除过 500000 并带 $），
    这个要的是可比较的原值。
    """
    try:
        data = json.loads(body).get("data") or {}
    except Exception:
        return None
    for key in ("quota", "balance", "remain_quota"):
        value = data.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
    return None


def extract_api_user(body: str) -> str:
    """从 new-api 用户信息响应中提取 New-Api-User 请求头的值。"""
    try:
        payload = json.loads(body)
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""

    data = payload.get("data")
    candidates = [data]
    if isinstance(data, dict):
        candidates.extend((data.get("user"), data.get("profile")))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("id", "user_id", "userId"):
            value = candidate.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
    return ""


def parse_cookies(value: dict[str, str] | str | None) -> dict[str, str]:
    """Parse a Cookie header or a JSON cookie map without logging values."""
    if isinstance(value, dict):
        return {
            str(name).strip(): str(cookie_value)
            for name, cookie_value in value.items()
            if str(name).strip() and str(cookie_value)
        }
    if isinstance(value, str):
        cookies: dict[str, str] = {}
        for item in value.split(";"):
            if "=" not in item:
                continue
            name, cookie_value = item.strip().split("=", 1)
            if name.strip() and cookie_value:
                cookies[name.strip()] = cookie_value
        return cookies
    return {}


# 异常文本会进日志和 webhook。Playwright 的报错带多行 call log，
# 里面可能有 Cookie 头或 add_cookies 的参数，只留首行并把值打码。
_SECRET_PAIR = re.compile(
    r"((?:session|cookie|token|password|acw_\w+|cdn_sec_tc)[\"']?\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s;,&]+)",
    re.I,
)


def brief_error(exc: BaseException, limit: int = 80) -> str:
    """异常的一行摘要：只取首行、截断、打码 Cookie/session 值。"""
    lines = str(exc).strip().splitlines()
    text = _SECRET_PAIR.sub(r"\1***", lines[0].strip() if lines else "")
    return f"{type(exc).__name__}: {text[:limit]}" if text else type(exc).__name__


# --------------------------------------------------------------------------- 浏览器

def _launch(playwright, site: SiteConfig, headless: bool):
    site.profile_dir.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "user_data_dir": str(site.profile_dir),
        "headless": headless,
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        "viewport": {"width": 1280, "height": 900},
    }
    if site.channel:
        kwargs["channel"] = site.channel
    if site.proxy:
        kwargs["proxy"] = {"server": site.proxy}
    context = playwright.chromium.launch_persistent_context(**kwargs)
    # 抹掉 navigator.webdriver 之类的自动化痕迹。实测真实 Edge 也会把它置为 true，
    # 而 hCaptcha 正是认这个——虽然日常签到这条路不碰验证码，但站点随时可能加。
    try:
        from playwright_stealth import Stealth

        for page in context.pages or [context.new_page()]:
            Stealth().apply_stealth_sync(page)
    except Exception:
        pass
    return context


def _browser_api_user(page, site: SiteConfig) -> str:
    """读取前端保存的用户标识；只返回 id，不读取任何凭据。"""
    try:
        value = page.evaluate(
            """(keys) => {
                for (const storage of [localStorage, sessionStorage]) {
                    for (const key of keys) {
                        try {
                            const raw = storage.getItem(key);
                            if (!raw) continue;
                            const obj = JSON.parse(raw);
                            const value = obj?.id ?? obj?.user_id ?? obj?.userId
                                ?? obj?.user?.id ?? obj?.user?.user_id;
                            if (value !== undefined && value !== null && String(value).trim()) {
                                return String(value).trim();
                            }
                        } catch (_) {}
                    }
                }
                return "";
            }""",
            ["user", "userInfo", "user_info", "currentUser"],
        )
        return str(value or "").strip()
    except Exception:
        return ""


# Cookie 名字都很短（"session" 才 7 个字符）；几十个字符的一定是值填错了位置。
_MAX_COOKIE_NAME_LEN = 40


def normalize_session_config(
    cookies: dict[str, str] | str | None,
    session_cookie: str,
) -> tuple[dict[str, str], str]:
    """纠正「把 session 值填进了 session_cookie」这种误配。

    ``session_cookie`` 存的是 Cookie 的**名字**（默认 ``"session"``），值要放在
    ``cookies`` 里。两个字段挨在一起、名字又像，实际配置时踩过一次：444 字符的
    session 值填进了 ``session_cookie``，``cookies`` 仍是 ``{}``，脚本按「未配置」
    提前停止，看起来像没登录。与其静默失败，不如按长度认出来并纠正。
    """
    parsed = parse_cookies(cookies)
    name = (session_cookie or "").strip()
    if len(name) > _MAX_COOKIE_NAME_LEN:
        parsed.setdefault("session", name)
        name = "session"
    return parsed, name or "session"


def _site_cookies(context, site: SiteConfig) -> dict[str, str]:
    """Return cookie values for one site without logging any sensitive value."""
    host = (urlparse(site.base_url).hostname or "").lower()
    result: dict[str, str] = {}
    for cookie in context.cookies(site.base_url):
        domain = (cookie.get("domain") or "").lstrip(".").lower()
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if domain == host and name and value:
            result[name] = value
    return result


# ESA 的 JS 挑战页自己就会下发 acw_tc / acw_sc__v2，所以「cookie 拿到了」不等于
# 「挑战过了」。带着未通关的 cookie 去打 API 会稳定 403，看起来像账号问题，其实不是。
# 参考实现（millylee/anyrouter-check-in 的 wait_for_site_ready）也是先确认页面就绪、
# 再把 cookie 交给 HTTP 客户端。
_WAF_CHALLENGE_MARKERS = re.compile(
    r"请进行验证|为了更好的访问体验|访问受限|访问验证|滑块验证"
    r"|Access denied|verify you are human|aliyun_wa"
    r"|http_auto_ratelimit|http_ratelimit|sorry, you have been blocked"
    r"|denied by|ruleid\s*:|requestid\s*:",
    re.I,
)
_WAF_RATE_LIMIT_MARKERS = re.compile(
    r"http_auto_ratelimit|http_ratelimit|sorry, you have been blocked"
    r"|denied by http_|ruleid\s*:",
    re.I,
)

# 判「页面已就绪」而不是只判「挑战文案消失」：拦截页也可能没有上面那些文案。
_PAGE_READY_JS = """() => {
    const isVisible = (el) => {
        if (!el || !el.isConnected) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden'
            || parseFloat(style.opacity) === 0) return false;
        const rect = el.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    };
    const blocker = document.querySelector(
        'iframe[src*="captcha"], iframe[src*="verify"], iframe[src*="slide"],'
        + ' .nc-container, #nocaptcha'
    );
    if (isVisible(blocker)) return false;
    const countVisible = (selector) =>
        [...document.querySelectorAll(selector)].filter(isVisible).length;
    return countVisible('a') > 0 || countVisible('button') > 0;
}"""


@dataclass
class WafWarmup:
    """临时浏览器预热的产物。

    ``passed`` 单独存在，是为了把「Cookie 没下发」和「Cookie 下发了但挑战没过」
    分成两种可读的失败原因——两者的处置完全不同。
    """
    cookies: dict[str, str] = field(default_factory=dict)
    user_agent: str = ""
    passed: bool = False
    rate_limited: bool = False


def is_waf_challenge_page(body_text: str) -> bool:
    """页面文本是否还停在 ESA 的验证/拦截页。"""
    return bool(_WAF_CHALLENGE_MARKERS.search(body_text or ""))


def is_waf_rate_limited_page(body_text: str) -> bool:
    """页面是否被 ESA 的自动限流规则直接拦截。"""
    return bool(_WAF_RATE_LIMIT_MARKERS.search(body_text or ""))


def _page_passed_waf(page) -> bool:
    """在真实浏览器里判断挑战是否跑完。任何异常一律当作「没过」。"""
    try:
        if is_waf_challenge_page(page.inner_text("body")):
            return False
        return bool(page.evaluate(_PAGE_READY_JS))
    except Exception:
        return False


def _browser_user_agent(page) -> str:
    """拿浏览器真实 UA 去发 API 请求，避免 UA 和 JS/TLS 指纹自相矛盾。"""
    try:
        return str(page.evaluate("() => navigator.userAgent") or "")
    except Exception:
        return ""


def _body_text(page) -> str:
    try:
        return page.inner_text("body") or ""
    except Exception:
        return ""


def _get_waf_cookies(page, site: SiteConfig) -> WafWarmup:
    """Warm ESA in a real browser and return only the required WAF cookies.

    每轮只导航一次（原来 reload 完紧接着又 goto，等于连续两跳），最多
    ``_WAF_MAX_NAVIGATIONS`` 轮；每次读页面都先判限流页，撞上立刻停。
    """
    required = set(site.waf_cookie_names or _ANYROUTER_WAF_COOKIES)
    target = site.base_url.rstrip("/") + site.waf_path
    deadline = time.monotonic() + max(site.timeout_ms, site.waf_budget_ms) / 1000
    user_agent = ""
    found: dict[str, str] = {}

    for _ in range(_WAF_MAX_NAVIGATIONS):
        if time.monotonic() >= deadline:
            break
        try:
            page.goto(
                target,
                wait_until="domcontentloaded",
                timeout=site.timeout_ms,
            )
        except Exception:
            # ESA can close the first navigation while issuing its challenge.
            pass

        if not user_agent:
            user_agent = _browser_user_agent(page)

        wait_until = time.monotonic() + site.settle_ms / 1000
        while True:
            cookies = _site_cookies(page.context, site)
            found = {name: cookies[name] for name in required if cookies.get(name)}
            if is_waf_rate_limited_page(_body_text(page)):
                # ESA 已明确返回硬阻断时不要继续导航；连续导航只会延长限流窗口。
                return WafWarmup(found, user_agent, False, True)
            if required.issubset(found) and _page_passed_waf(page):
                return WafWarmup(found, user_agent, True)
            if time.monotonic() >= wait_until:
                break
            page.wait_for_timeout(1000)

    return WafWarmup(found, user_agent, False)


def _is_anyrouter(site: SiteConfig) -> bool:
    return (urlparse(site.base_url).hostname or "").lower() == "anyrouter.top"


def _open_temp_page(playwright, site: SiteConfig, headless: bool = True):
    """开一个干净的临时浏览器。

    AnyRouter 靠注入 session 恢复登录态；AgentRouter 每次重新表单登录。
    两个站都不需要持久化 profile。
    """
    kwargs: dict[str, Any] = {
        "headless": headless,
        "args": ["--no-sandbox", "--disable-dev-shm-usage"],
    }
    if site.channel:
        kwargs["channel"] = site.channel
    if site.proxy:
        kwargs["proxy"] = {"server": site.proxy}

    browser = playwright.chromium.launch(**kwargs)
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    try:
        from playwright_stealth import Stealth

        Stealth().apply_stealth_sync(page)
    except Exception:
        pass
    return browser, context, page


def _inject_session_cookie(context, site: SiteConfig, cookies: dict[str, str]) -> None:
    """把配置里的登录 Cookie 塞进临时上下文。"""
    host = (urlparse(site.base_url).hostname or "")
    context.add_cookies([
        {
            "name": name,
            "value": value,
            "domain": host,
            "path": "/",
            "secure": True,
        }
        for name, value in cookies.items()
    ])


def _checkin_anyrouter(
    playwright,
    site: SiteConfig,
    verbose: bool = False,
) -> CheckinResult:
    """AnyRouter：session Cookie 认证 + 浏览器 WAF 预热 + **页内 fetch** 调 API。

    刻意不用 httpx：实测同一上下文、同一组 Cookie、同一代理出口下，
    页内 fetch 拿到 401（正常的未登录），httpx 拿到 403——ESA 认的是
    Chrome 的 TLS/HTTP2 指纹，换 header 没用。所以网络栈必须是浏览器自己的。
    """
    required = set(site.waf_cookie_names or _ANYROUTER_WAF_COOKIES)
    user_cookies, session_cookie = normalize_session_config(
        site.cookies, site.session_cookie
    )
    if session_cookie != (site.session_cookie or "session"):
        print(f"  {site.name}: [配置提醒] session 值应放在 cookies.session，"
              f"session_cookie 只存名字；本次已自动纠正")
    if not user_cookies.get(session_cookie):
        return CheckinResult(
            site.name,
            NEED_LOGIN,
            f"配置中未发现 session Cookie，请在 {site.config_label} 填入 cookies.session",
        )

    browser = context = None
    try:
        browser, context, page = _open_temp_page(playwright, site)

        if verbose:
            print(f"  {site.name}: 先用临时浏览器过 ESA WAF")
        warmup = _get_waf_cookies(page, site)
        # 限流要先报：被硬阻断时 Cookie 本来就不全，报「Cookie 不完整」会把人带偏。
        if warmup.rate_limited:
            return CheckinResult(
                site.name,
                FAILED,
                "ESA 自动限流（http_auto_ratelimit），请更换代理出口或等待解封；"
                "本次不再重试",
            )
        missing = sorted(required - set(warmup.cookies))
        if missing:
            return CheckinResult(
                site.name,
                FAILED,
                f"WAF Cookie 获取不完整: {','.join(missing)}",
            )
        if not warmup.passed:
            return CheckinResult(
                site.name,
                FAILED,
                "WAF 挑战未通过（Cookie 已下发但页面仍是验证页），请稍后重试",
            )

        # 先过 WAF 再注入 session：反过来会把登录态暴露给挑战页那一跳。
        _inject_session_cookie(context, site, user_cookies)

        self_code, self_body = _call(page, site, site.self_path,
                                     api_user=site.api_user)
        api_user = site.api_user or extract_api_user(self_body)
        # 这一步只是读用户信息，不是签到，不能判出「今天已签过」
        status, detail = classify_response(self_code, self_body, allow_already=False)
        if status != OK:
            return CheckinResult(site.name, status, detail)

        sign_code, sign_body = _call(page, site, site.checkin_path,
                                     method=site.checkin_method,
                                     api_user=api_user)
        status, detail = classify_response(sign_code, sign_body)
        if status not in (OK, ALREADY):
            return CheckinResult(site.name, status, detail)

        quota_code, quota_body = _call(page, site, site.self_path,
                                       api_user=api_user)
        quota = extract_quota(quota_body) if quota_code == 200 else ""

        # 首次签到 message 是「签到成功，获得 $25 额度」，重复签到是空串——
        # 但空串也可能只是站点没给文案，所以不单凭它下结论，只用它做**否决**：
        # message 非空即确定是本次签到成功，不再看额度。
        # 额度差是主判据，且只在「完全相等」时判已签到：额度变少说明期间有消耗，
        # 那种情况下签到到底成没成不好说，保留 OK 这个更安全的默认值。
        if status == OK and not detail:
            before = quota_amount(self_body)
            after = quota_amount(quota_body)
            if before is not None and after is not None and after == before:
                status, detail = ALREADY, "今日已签到（额度未变化）"

        return CheckinResult(site.name, status, detail, quota)
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()


_USERNAME_SELECTORS = (
    "#username",
    'input[name="username"]',
    'input[name="email"]',
    'input[type="email"]',
)
_PASSWORD_SELECTORS = (
    "#password",
    'input[name="password"]',
    'input[type="password"]',
)
_SUBMIT_SELECTORS = (
    'form.semi-form button[type="submit"]',
    'button[type="submit"]',
)
# 登录页默认显示的是 OAuth 按钮，邮箱表单要先点开。
_EMAIL_ENTRY_SELECTORS = (
    '.semi-card button:has(.semi-icon-mail):not(form.semi-form button)',
    'button:has(.semi-icon-mail):not(form.semi-form button)',
)
_EMAIL_ENTRY_TEXTS = (
    "Sign in with Email or Username",
    "Email or Username",
    "使用邮箱",
    "邮箱或用户名",
)


def _first_visible(page, selectors):
    """返回第一个可见的元素，没有就 None。"""
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count() and locator.is_visible():
                return locator
        except Exception:
            continue
    return None


def _open_email_login_form(page, site: SiteConfig) -> bool:
    """把邮箱/用户名表单点出来。已经显示的话直接返回。"""
    if _first_visible(page, _USERNAME_SELECTORS) is not None:
        return True

    entry = _first_visible(page, _EMAIL_ENTRY_SELECTORS)
    if entry is None:
        for text in _EMAIL_ENTRY_TEXTS:
            locator = page.get_by_text(text, exact=False).first
            try:
                if locator.count() and locator.is_visible():
                    entry = locator
                    break
            except Exception:
                continue
    if entry is None:
        return False

    try:
        entry.click()
    except Exception:
        return False
    page.wait_for_timeout(1500)
    return _first_visible(page, _USERNAME_SELECTORS) is not None


def _warm_site(page, site: SiteConfig) -> bool:
    """访问站点根路径，等 ESA 的 JS 挑战跑完。

    AgentRouter 的登录页和 API 都在 WAF 后面，不先过挑战的话
    ``/api/user/self`` 会以 HTTP 200 返回一整页验证 HTML。
    导航次数有上限，撞上限流页立刻放弃：连着导航只会延长限流窗口。
    """
    deadline = time.monotonic() + max(site.timeout_ms, site.waf_budget_ms) / 1000
    for _ in range(_WAF_MAX_NAVIGATIONS):
        if time.monotonic() >= deadline:
            break
        try:
            page.goto(site.base_url.rstrip("/") + "/",
                      wait_until="domcontentloaded", timeout=site.timeout_ms)
        except Exception:
            pass
        page.wait_for_timeout(site.settle_ms)
        if is_waf_rate_limited_page(_body_text(page)):
            return False
        if _page_passed_waf(page):
            return True
    return False


def _open_login_page(page, site: SiteConfig, attempts: int = 3) -> tuple[bool, str]:
    """打开登录页并把邮箱表单点出来，失败重试。

    实测这两步都会因网络抖动偶发失败：``/login`` 导航 ERR_TIMED_OUT，
    或页面没渲染完导致找不到表单。一次不成不代表账号有问题。
    """
    target = site.base_url.rstrip("/") + "/login"
    last = "未知原因"
    for attempt in range(1, attempts + 1):
        try:
            page.goto(target, wait_until="domcontentloaded",
                      timeout=site.timeout_ms)
        except Exception as e:
            last = f"登录页打不开: {type(e).__name__}"
            continue
        page.wait_for_timeout(site.settle_ms)
        if _open_email_login_form(page, site):
            return True, ""
        last = "登录页找不到邮箱/用户名表单"
    return False, f"{last}（重试 {attempts} 次）"


# 登录接口拒绝凭据时，前端 toast 里的文案。只有看到这些才算「凭据有问题」；
# 网络、WAF、页面改版导致的失败另算 FAILED，否则会把临时故障报成要人管的账号问题。
_CREDENTIAL_ERROR_MARKERS = re.compile(
    r"用户名或密码错误|密码错误|用户不存在|用户已被封禁|账号或密码"
    r"|invalid (?:username|password|credentials)|incorrect (?:username|password)"
    r"|wrong password",
    re.I,
)


def _login_with_password(page, site: SiteConfig) -> tuple[str, str]:
    """用用户名/密码登录。返回 (状态, 失败原因)。密码不进日志。

    状态：``OK`` 登录成功；``NEED_LOGIN`` 仍停在登录页且页面报了凭据错误；
    其余（导航超时、找不到表单、没等到跳转）一律 ``FAILED``。
    """
    opened, reason = _open_login_page(page, site)
    if not opened:
        return FAILED, reason

    username = _first_visible(page, _USERNAME_SELECTORS)
    password = _first_visible(page, _PASSWORD_SELECTORS)
    if username is None or password is None:
        return FAILED, "登录表单缺少用户名或密码输入框"

    username.fill(site.username)
    password.fill(site.password)

    submit = _first_visible(page, _SUBMIT_SELECTORS)
    if submit is None:
        return FAILED, "登录表单找不到提交按钮"
    submit.click()
    page.wait_for_timeout(site.settle_ms)

    host = (urlparse(site.base_url).hostname or "").lower()
    deadline = time.monotonic() + max(site.timeout_ms, 20_000) / 1000
    # toast 几秒就消失，所以边等边看；认出凭据错误就不必再等满超时。
    credential_error = False
    while time.monotonic() < deadline:
        cookies = {
            c["name"] for c in page.context.cookies(site.base_url)
            if (c.get("domain") or "").lstrip(".").lower() == host
        }
        if "session" in cookies and "/login" not in page.url:
            return OK, ""
        if _CREDENTIAL_ERROR_MARKERS.search(_body_text(page)):
            credential_error = True
            break
        page.wait_for_timeout(1000)

    text = " ".join(_body_text(page)[:120].split())
    reason = f"登录未完成（仍停在登录页）{(': ' + text) if text else ''}"
    if credential_error and "/login" in page.url:
        return NEED_LOGIN, reason
    return FAILED, reason


# 系统日志（type=4）里有「每日签到成功」这条记录，是判断今天签没签过的唯一
# 可靠依据：这个站查 /api/user/self 就等于签到，拿不到「签到前」的余额来对比。
_LOG_PATH = "/api/log/self"
_LOG_TYPE_SYSTEM = 4


def parse_checkin_log(body: str) -> list[int]:
    """从系统日志里挑出签到记录的时间戳。"""
    try:
        items = (json.loads(body).get("data") or {}).get("items") or []
    except Exception:
        return []
    stamps = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if "签到" in str(item.get("content") or ""):
            try:
                stamps.append(int(item.get("created_at") or 0))
            except (TypeError, ValueError):
                continue
    return [s for s in stamps if s > 0]


def _today_range(now: Optional[float] = None) -> tuple[int, int]:
    """本地时区当天的起止时间戳。"""
    now = time.time() if now is None else now
    local = time.localtime(now)
    start = int(time.mktime((local.tm_year, local.tm_mon, local.tm_mday,
                             0, 0, 0, 0, 0, -1)))
    return start, start + 86399


def _read_checkin_log(
    page,
    site: SiteConfig,
    api_user: str,
) -> tuple[Optional[list[int]], str, str]:
    """查今天的签到系统日志，返回 ``(时间戳列表, 鉴权失败类型, 服务端 message)``。

    读不到时列表为 ``None``；这时第二项说明是不是鉴权问题（``AUTH_EXPIRED`` /
    ``AUTH_API_USER`` / ``""``），预检据此决定能不能重登。
    """
    start, end = _today_range()
    path = (f"{_LOG_PATH}?p=1&page_size=20&type={_LOG_TYPE_SYSTEM}"
            f"&start_timestamp={start}&end_timestamp={end}")
    try:
        code, body = _call(page, site, path, api_user=api_user)
    except Exception:
        return None, "", ""
    if is_waf_challenge_page(body):
        return None, "", ""
    try:
        payload = json.loads(body)
    except Exception:
        payload = None
    message = _json_message(payload)
    # new-api 对「未登录」回 401，对「access token 无效」却回 200 + success:false
    if code == 401 or (isinstance(payload, dict) and payload.get("success") is False):
        return None, auth_failure_kind(message), message
    if code != 200 or not isinstance(payload, dict):
        return None, "", message
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None, "", message
    return parse_checkin_log(body), "", ""


def _today_checkin_stamps(page, site: SiteConfig,
                          api_user: str) -> Optional[list[int]]:
    """查今天的签到系统日志，返回签到记录的时间戳列表。

    读不到（WAF 挡了 / 未登录 / 出错）返回 ``None``——和「读到了、但今天还没签
    （空列表）」区分开：前者是「不知道」，后者是「确定没签」。两者的处置不同。
    """
    return _read_checkin_log(page, site, api_user)[0]


def _checked_in_before(page, site: SiteConfig, api_user: str,
                       run_start: int) -> Optional[bool]:
    """今天的签到是不是在本次运行**之前**就完成了。拿不到日志返回 None。"""
    stamps = _today_checkin_stamps(page, site, api_user)
    if not stamps:
        return None
    return min(stamps) < run_start


# 登录态 Cookie。清掉它就等于「退出」，而同域的 WAF 放行 Cookie 要留着。
_LOGIN_COOKIE_NAMES = frozenset(("session",))


def _is_login_cookie(cookie: dict[str, Any], host: str) -> bool:
    return ((cookie.get("domain") or "").lstrip(".").lower() == host
            and cookie.get("name") in _LOGIN_COOKIE_NAMES)


def _has_login_cookie(page, site: SiteConfig) -> bool:
    """profile 里有没有本站的登录 Cookie。没有就没有需要保护的会话。"""
    return bool(_LOGIN_COOKIE_NAMES & set(_site_cookies(page.context, site)))


def _logout_keep_waf(context, site: SiteConfig) -> list[dict[str, Any]]:
    """只清登录 Cookie，保留同域的 WAF 放行 Cookie。返回被清掉的 Cookie。

    这个站要「退出后重新登录」才计签到，但阿里云 WAF 的放行 Cookie 和登录
    Cookie 同域。整域清掉会把人工滑块换来的放行一起清掉，下次又得人工，
    自动化就断了。所以只精确清掉登录 Cookie。

    返回值留给调用方在重登失败时原样 ``add_cookies`` 回去。
    """
    host = (urlparse(site.base_url).hostname or "").lower()
    dropped = [c for c in context.cookies() if _is_login_cookie(c, host)]
    # 按名字 + 域精确清（Playwright>=1.58）；域名可能带前导点，用正则一并覆盖
    domain = re.compile(r"^\.?" + re.escape(host) + r"$", re.I)
    for name in sorted(_LOGIN_COOKIE_NAMES):
        context.clear_cookies(name=name, domain=domain)
    return dropped


def _restore_login_cookies(context, cookies: list[dict[str, Any]]) -> None:
    """重登没成功时把原会话放回去：一次失败的重登不该把能用的会话变成要人工登录。"""
    if cookies:
        context.add_cookies(cookies)


def _precheck_agentrouter(
    page,
    site: SiteConfig,
) -> tuple[Optional[bool], str, str]:
    """在任何破坏登录态的操作前确认今天是否已经签到。

    返回 ``(True, api_user, "")`` 表示今天已签到；``(False, api_user, 说明)``
    表示明确读到今天还没有签到，或者根本没有可保护的会话——只有这种情况允许
    继续退出重登。返回 ``(None, api_user, reason)`` 表示状态无法确认；调用方
    必须保留当前 ``session``，不能把网络/WAF/api_user 问题放大成一次危险的重登。
    """
    # profile 里没有 session（首次运行、profile 被清过）：没有要保护的登录态，
    # 预检也读不到日志，直接去登录。
    if not _has_login_cookie(page, site):
        return False, site.api_user, "profile 中没有登录 Cookie，跳过预检直接登录"

    if not _warm_site(page, site):
        return None, "", "预检无法通过 WAF，未执行退出重登"

    api_user = site.api_user or _browser_api_user(page, site)
    if not api_user:
        return (
            None,
            "",
            f"预检未取得 api_user，请在 {site.config_label} 的 AgentRouter "
            "账号中填写 api_user；未执行退出重登",
        )

    stamps, auth, message = _read_checkin_log(page, site, api_user)
    if stamps is not None:
        return bool(stamps), api_user, ""
    if auth == AUTH_EXPIRED:
        # 服务端明确说会话已失效：旧 session 本来就没用了，重登不会损失什么。
        return False, api_user, f"预检发现会话已失效（{message[:60]}），直接重新登录"
    if auth == AUTH_API_USER:
        return (
            None,
            api_user,
            f"预检被拒: {message[:60]}；api_user 配置有误（应为该账号的用户 ID），"
            "未执行退出重登",
        )
    return None, api_user, "预检无法读取今日签到记录，未执行退出重登"


def _checkin_agentrouter(
    playwright,
    site: SiteConfig,
    verbose: bool = False,
) -> CheckinResult:
    """AgentRouter：确认未签到后再全新表单登录，查 /api/user/self 触发签到。

    这个站的规则是「退出后重新进入才计签到」；但只有预检明确显示今天未签到
    （或 profile 里根本没有会话）时，才退出当前会话并重新登录。

    用持久化 profile 而不是临时浏览器：``/api/user/self`` 被阿里云 WAF 的
    **人工滑块**挡着，实测代理/直连、有头/无头都一样。放行 Cookie 只能靠人工
    滑一次换来，必须留在 profile 里；每天只精确清掉登录 Cookie 来实现「退出」。

    签到不需要单独的接口：查用户信息本身就会触发（配置里 checkin_path 为空）。
    """
    if not site.username or not site.password:
        return CheckinResult(
            site.name,
            NEED_LOGIN,
            f"配置中未发现 username/password，请在 {site.config_label} 填入",
        )

    run_start = int(time.time())
    context = None
    try:
        context = _launch(playwright, site, headless=True)
        page = context.pages[0] if context.pages else context.new_page()

        # 幂等守卫：profile 里通常还留着上次跑完的有效会话。退出重登会触发滑块，
        # 所以先用现有会话查只读的系统日志。只有明确读到「今天还没签」才允许清
        # session；WAF、api_user 或日志接口任何一项无法确认，都保留原登录态。
        precheck, precheck_user, precheck_reason = _precheck_agentrouter(page, site)
        if precheck is None:
            return CheckinResult(site.name, CHECKIN_UNKNOWN, precheck_reason)
        if precheck:
            if verbose:
                print(f"  {site.name}: 今日已签到（运行前系统日志已有记录），"
                      f"跳过退出重登")
            quota_code, quota_body = _call(page, site, site.self_path,
                                           api_user=precheck_user)
            quota = extract_quota(quota_body) if quota_code == 200 else ""
            return CheckinResult(
                site.name, ALREADY,
                "今日已签到（运行前系统日志已有记录，跳过退出重登）", quota)
        if verbose and precheck_reason:
            print(f"  {site.name}: {precheck_reason}")

        # 清掉的登录 Cookie 留一份：后面任何一步没走通，都把原会话放回去。
        dropped = _logout_keep_waf(context, site)
        if verbose:
            print(f"  {site.name}: 已退出（清掉 {len(dropped)} 个登录 Cookie，保留 WAF 放行）")
            print(f"  {site.name}: 先过 ESA WAF")
        restored = "（已恢复原登录态）" if dropped else ""
        try:
            if not _warm_site(page, site):
                # WAF 没过，登录页多半也打不开。别再导航，免得给限流续命。
                _restore_login_cookies(context, dropped)
                return CheckinResult(
                    site.name, FAILED,
                    f"退出后 WAF 预热未通过，未尝试登录{restored}")
            if verbose:
                print(f"  {site.name}: 重新登录（该站每天需重新登录才计签到）")
            login_status, reason = _login_with_password(page, site)
        except Exception:
            _restore_login_cookies(context, dropped)
            raise
        if login_status != OK:
            _restore_login_cookies(context, dropped)
            return CheckinResult(site.name, login_status, reason + restored)

        # 登录后再走一次 settle：既过应用侧 WAF，也顺便抓 New-Api-User。
        api_user = site.api_user or _settle(page, site)
        self_code, self_body = _call(page, site, site.self_path,
                                     api_user=api_user)
        # 滑块是会话级、间歇性的：重新预热后多打几次，别一撞就判失败。
        for attempt in range(_WAF_REWARM_RETRIES):
            if not is_waf_challenge_page(self_body):
                break
            if verbose:
                print(f"  {site.name}: 命中 WAF 验证页，重新预热后重试"
                      f"（{attempt + 1}/{_WAF_REWARM_RETRIES}）")
            _warm_site(page, site)
            self_code, self_body = _call(page, site, site.self_path,
                                         api_user=api_user)
        if is_waf_challenge_page(self_body):
            # 走到这里，表单登录**已经成功**（上面 login 失败会先行 return），
            # 只是用来核对的这次 fetch 被滑块挡了。签到本身通常已由登录后控制台
            # 自己发的 /api/user/self 触发——实测撞滑块的号，稍后查系统日志能看到
            # 本次运行时段已签上。同一会话被挡时连系统日志也读不到，没法当场坐实，
            # 所以既不判 NEED_LOGIN（凭据没问题，重登也没用），也不谎报已签过：
            # 记为「已提交、未核对」，不报红，如实标注。
            return CheckinResult(
                site.name,
                UNVERIFIED,
                "登录成功、签到已触发，核对被滑块拦截（未读到余额）；"
                f"若余额长期不涨再人工滑一次：python tools/daily_checkin.py --login {site.name}",
            )
        api_user = api_user or extract_api_user(self_body)
        status, detail = classify_response(self_code, self_body)
        if status not in (OK, ALREADY):
            return CheckinResult(site.name, status, detail)

        quota = extract_quota(self_body)
        if not quota:
            quota_code, quota_body = _call(page, site, site.self_path,
                                           api_user=api_user)
            quota = extract_quota(quota_body) if quota_code == 200 else ""

        # 站点对重复签到也返回成功，余额也没法对比（查 self 就等于签到），
        # 所以用系统日志兜底：今天的签到记录早于本次运行 = 之前就签过了。
        if status == OK and _checked_in_before(page, site, api_user, run_start):
            status, detail = ALREADY, "今日已签到（系统日志显示本次运行前已签过）"

        return CheckinResult(site.name, status, detail, quota)
    finally:
        if context is not None:
            context.close()


def _settle(page, site: SiteConfig) -> str:
    """先过根路径 WAF，再访问控制台并捕获 New-Api-User。"""
    observed: dict[str, str] = {}
    header_name = site.api_user_header.lower()

    def capture_request(request) -> None:
        try:
            headers = request.headers
            value = headers.get(header_name) or headers.get("new-api-user")
            if value:
                observed["api_user"] = value
        except Exception:
            pass

    page.on("request", capture_request)
    try:
        page.goto(site.base_url.rstrip("/") + "/", wait_until="domcontentloaded",
                  timeout=site.timeout_ms)
        page.wait_for_timeout(site.settle_ms)
        if site.login_target.rstrip("/") != site.base_url.rstrip("/"):
            page.goto(site.login_target, wait_until="domcontentloaded",
                      timeout=site.timeout_ms)
            page.wait_for_timeout(site.settle_ms)
    finally:
        page.remove_listener("request", capture_request)

    return observed.get("api_user") or _browser_api_user(page, site)


def _request_headers(site: SiteConfig, api_user: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Referer": site.base_url.rstrip("/") + "/",
        "Origin": site.base_url.rstrip("/"),
        "X-Requested-With": "XMLHttpRequest",
    }
    if api_user:
        headers[site.api_user_header] = api_user
    return headers


# 页内 fetch 途中页面跳转（ESA 挑战页自己 reload、登录后前端路由跳转）会让
# evaluate 所在的执行上下文被销毁——只有这种情况才退回 page.request。
# page.request 走的是 Playwright 自己的网络栈，不是浏览器的 TLS/HTTP2 指纹，
# ESA 会稳定 403；把其他异常也吞下来改走它，只会把真正的错误伪装成 WAF 问题。
_CONTEXT_DESTROYED = re.compile(
    r"execution context was destroyed|cannot find context with specified id"
    r"|frame was detached",
    re.I,
)


def _call(
    page,
    site: SiteConfig,
    path: str,
    *,
    method: str = "GET",
    api_user: str = "",
) -> tuple[int, str]:
    request_headers = _request_headers(site, api_user)
    try:
        result = page.evaluate(
            """async ({url, method, headers}) => {
                const response = await fetch(url, {
                    method,
                    cache: "no-store",
                    credentials: "include",
                    headers,
                    body: method === "POST" ? null : undefined,
                });
                return {status: response.status, body: await response.text()};
            }""",
            {
                "url": site.base_url.rstrip("/") + path,
                "method": method.upper(),
                "headers": request_headers,
            },
        )
        return int(result["status"]), str(result.get("body") or "")
    except Exception as e:
        if not _CONTEXT_DESTROYED.search(str(e)):
            raise
        response = page.request.fetch(
            site.base_url.rstrip("/") + path,
            method=method.upper(),
            headers=request_headers,
            timeout=site.timeout_ms,
        )
        return response.status, response.text()


def checkin_site(playwright, site: SiteConfig, verbose: bool = False) -> CheckinResult:
    # 每条分支各自兜住异常。踩过一次：agentrouter-3 的 /login 导航
    # ERR_TIMED_OUT 直接把整轮打断，后面的账号一个都没跑，
    # 前面已经成功的结果也因为没走到 write_log 而全部丢失。
    if _is_anyrouter(site):
        try:
            return _checkin_anyrouter(playwright, site, verbose=verbose)
        except Exception as e:
            return CheckinResult(site.name, FAILED, brief_error(e))
    # relogin 一律走密码登录流程；没配 username/password 的由它报「需要人工处理」。
    # 原来没密码时会落到 OAuth 重登路径，那条路先整域清 Cookie（连 session 和
    # WAF 放行一起清），异常时也不恢复，正好违背「状态未知时不破坏会话」。
    if site.kind == "relogin":
        try:
            return _checkin_agentrouter(playwright, site, verbose=verbose)
        except Exception as e:
            return CheckinResult(site.name, FAILED, brief_error(e))

    # 其余 refresh 站点：profile 持有登录态，查 self 后调签到端点，不做任何重登。
    context = None
    try:
        context = _launch(playwright, site, headless=True)
        page = context.pages[0] if context.pages else context.new_page()

        api_user = _settle(page, site)
        self_code, self_body = _call(
            page, site, site.self_path, api_user=api_user
        )
        api_user = api_user or extract_api_user(self_body)
        status, detail = classify_response(self_code, self_body, allow_already=False)

        if status == OK and site.checkin_path:
            status_code, body = _call(
                page,
                site,
                site.checkin_path,
                method=site.checkin_method,
                api_user=api_user,
            )
            status, detail = classify_response(status_code, body)

        quota = ""
        if status in (OK, ALREADY):
            self_code, self_body = _call(
                page, site, site.self_path, api_user=api_user
            )
            if self_code == 200:
                quota = extract_quota(self_body)

        return CheckinResult(site.name, status, detail, quota)
    except Exception as e:
        return CheckinResult(site.name, FAILED, brief_error(e))
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


_BROWSER_PATHS = {
    "msedge": (
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ),
    "chrome": (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ),
}


def find_browser(channel: str) -> str:
    for candidate in _BROWSER_PATHS.get(channel, ()):
        if os.path.isfile(candidate):
            return candidate
    return ""


def _plain_browser_args(exe: str, site: SiteConfig, target: str) -> list[str]:
    args = [
        exe,
        f"--user-data-dir={site.profile_dir}",
        "--profile-directory=Default",
        "--new-window",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if site.proxy:
        args.append(f"--proxy-server={site.proxy}")
    args.append(target)
    return args


def interactive_login(site: SiteConfig) -> int:
    """用**真实浏览器普通启动**让人工登录，全程不带任何自动化开关。

    为什么不用 Playwright 开有头浏览器：linux.do 的登录页有 hCaptcha，
    而 Playwright 启的浏览器 `navigator.webdriver` 为 true（真实 Edge 也一样），
    hCaptcha 会直接拒绝，报「Your browser or network settings are blocking hCaptcha」。

    普通 subprocess 启动的浏览器没有这些痕迹，验证码能正常过。
    登录态照样写进 profile——实测普通 Edge 写的 profile，
    Playwright 用同一 channel 能完整读出 cookie（前提是**优雅关闭**，
    强杀会来不及落盘）。所以这里等你自己关窗，不去 kill 它。
    """
    exe = find_browser(site.channel)
    if not exe:
        print(f"[{site.name}] 找不到 {site.channel} 的可执行文件，"
              f"请把 channel 改成已安装的浏览器（msedge / chrome）")
        return 1

    site.profile_dir.mkdir(parents=True, exist_ok=True)
    args = _plain_browser_args(exe, site, site.login_target)

    print(f"[{site.name}] 正在打开 {site.channel}，请在浏览器里完成登录。")
    print(f"  profile: {site.profile_dir}")
    print(f"  代理:    {site.proxy or '（不用代理）'}")
    print("  登录完成后**把浏览器窗口关掉**（别强制结束进程，那样 cookie 来不及落盘）。")
    print("  脚本会等它退出，然后自动校验登录态…")

    try:
        subprocess.run(args)
    except KeyboardInterrupt:
        print("  已中断")
        return 1

    if site.kind == "relogin":
        challenge_url = site.base_url.rstrip("/") + site.self_path
        print("  AgentRouter 的 API 可能需要一次阿里云滑块验证。")
        print(f"  请在新窗口完成验证：{challenge_url}")
        print("  验证成功后关闭该窗口，脚本会继续校验登录态…")
        try:
            subprocess.run(_plain_browser_args(exe, site, challenge_url))
        except KeyboardInterrupt:
            print("  已中断")
            return 1

    print("  浏览器已退出，正在校验…")
    return _verify_login(site)


def _verify_login(site: SiteConfig) -> int:
    """无头复用刚写好的 profile，调 /api/user/self 确认真的登上了。"""
    from playwright.sync_api import sync_playwright

    context = None
    cookie_names: list[str] = []
    try:
        with sync_playwright() as playwright:
            context = _launch(playwright, site, headless=True)
            page = context.pages[0] if context.pages else context.new_page()
            api_user = _settle(page, site)
            host = (urlparse(site.base_url).hostname or "").lower()
            cookie_names = sorted(
                {
                    cookie["name"]
                    for cookie in context.cookies()
                    if (cookie.get("domain") or "").lstrip(".").lower() == host
                    and not cookie["name"].startswith(("acw_", "cdn_"))
                }
            )
            status_code, body = _call(
                page, site, site.self_path, api_user=api_user
            )
            context.close()
            context = None
    except Exception as e:
        print(f"  ✗ 校验时出错: {brief_error(e, 90)}")
        print("    若提示 profile 被占用，请确认浏览器窗口已全部关闭后重试")
        return 1
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass

    if status_code == 200:
        print(f"  ✓ 登录态已保存（余额 {extract_quota(body) or '未知'}）")
        return 0
    print(f"  ✗ 仍未登录（{site.self_path} 返回 {status_code}）")
    print(f"    已保存的站点 Cookie: {', '.join(cookie_names) or '无'}")
    print("    若这里显示“无”，说明没有完成回跳或浏览器没有使用该 profile")
    return 1


# --------------------------------------------------------------------------- 输出

def write_log(results: list[CheckinResult], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a", encoding="utf-8") as handle:
        for result in results:
            handle.write(f"{stamp} {result.line()}\n")


class _RejectRedirect(HTTPRedirectHandler):
    """webhook 返回 3xx 时不跟随。

    urllib 跟随 301/302/303 会把 POST 改成不带 body 的 GET，
    结果是「推送看起来成功、其实什么都没送到」。直接当失败报出来。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve_notify_url(cli_value: str) -> str:
    """--notify-url 优先，没给就读环境变量 CHECKIN_NOTIFY_URL。"""
    return (cli_value or os.environ.get(_NOTIFY_ENV, "")).strip()


def push_to_webhook(results: list[CheckinResult], target: str) -> bool:
    """把结果 POST 到可选 webhook，不依赖宿主项目。返回是否送达。

    请求体是 UTF-8 JSON：``{"content": 文本, "text": 文本}``（两个键内容相同，
    照顾不同机器人的字段名），文本第一行是「每日签到 MM-DD」，之后每个账号一行，
    格式与日志行相同。推送失败只打印，不影响签到结果和退出码。
    """
    if not target:
        return False
    if urlparse(target).scheme.lower() not in ("http", "https"):
        # file:// 之类的地址 urllib 也能「打开」，不挡掉就成了读本地文件
        print("  [推送跳过] webhook 地址只支持 http/https")
        return False
    lines = [f"每日签到 {time.strftime('%m-%d')}"]
    lines.extend(result.line() for result in results)
    content = "\n".join(lines)
    payload = json.dumps(
        {"content": content, "text": content},
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        target,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        # 非 2xx 由 urllib 抛 HTTPError；3xx 被 _RejectRedirect 拦下后同样走 HTTPError
        with build_opener(_RejectRedirect).open(request, timeout=10):
            pass
        return True
    except HTTPError as e:
        try:
            e.close()
        except Exception:
            pass
        if 300 <= e.code < 400:
            reason = f"webhook 返回重定向 HTTP {e.code}，未跟随（跟随会把 POST 变成 GET）"
        else:
            reason = f"webhook 返回 HTTP {e.code}"
        print(f"  [推送失败，不影响签到结果] {reason}")
    except Exception as e:
        print(f"  [推送失败，不影响签到结果] {brief_error(e)}")
    return False


# --------------------------------------------------------------------------- 入口

# 不属于某个账号的结果（配置错误、整轮崩溃、废弃参数）用这个名字记日志。
# 不带 anyrouter-/agentrouter- 前缀，外部按前缀读余额的脚本不会误读它。
_RUN_NAME = "daily_checkin"


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="anyrouter / agentrouter 每日签到")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--login",
        metavar="NAME",
        default="",
        help="用有头浏览器登录 AgentRouter；AnyRouter 请直接配置 cookies.session",
    )
    parser.add_argument("--only", metavar="NAME", default="", help="只跑指定账号")
    parser.add_argument("--notify-url", metavar="URL", default="",
                        help=f"把结果 POST 到 webhook URL（留空则读环境变量 "
                             f"{_NOTIFY_ENV}，都没有则不推送）")
    # wxbot 时代的参数（按微信 ID 推送）。隐藏但保留：旧的任务计划还带着它时，
    # 要明确失败并留下日志，而不是被 argparse 当成未知参数、只在控制台报一句。
    parser.add_argument("--notify", metavar="WXID", default=None,
                        help=argparse.SUPPRESS)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def _fail_run(detail: str, log_path: Path, notify_url: str = "") -> int:
    """整轮跑不起来时：打印、记日志、尽量推送，返回退出码 2。"""
    result = CheckinResult(_RUN_NAME, FAILED, detail)
    print(result.line())
    try:
        write_log([result], log_path)
    except Exception as e:
        print(f"  [写日志失败] {brief_error(e)}")
    push_to_webhook([result], notify_url)
    return 2


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    if args.notify is not None:
        return _fail_run(
            "--notify 已废弃（那是 wxbot 的微信推送）；请改用 --notify-url URL "
            f"或环境变量 {_NOTIFY_ENV}",
            args.log,
        )
    notify_url = resolve_notify_url(args.notify_url)

    try:
        sites = load_sites(args.config)
    except (SystemExit, ValueError, OSError) as e:
        # SystemExit 是 load_sites 的可读报错；ValueError 覆盖 JSON 语法错误
        message = e.code if isinstance(e, SystemExit) else brief_error(e)
        first_line = str(message).strip().splitlines()[0] if str(message).strip() else ""
        # --login 是人在跟前交互，不推送
        return _fail_run(f"配置加载失败（{args.config}）: {first_line}",
                         args.log, "" if args.login else notify_url)
    by_name = {site.name: site for site in sites}

    if args.login:
        site = by_name.get(args.login)
        if not site:
            print(f"没有名为 {args.login} 的账号，可用: {', '.join(by_name)}")
            return 1
        if _is_anyrouter(site):
            print(
                "AnyRouter 不使用 profile 登录态，请把 session Cookie 填入 "
                f"{args.config} 的 cookies.session"
            )
            return 1
        return interactive_login(site)

    targets = [s for s in sites if s.enabled and (not args.only or s.name == args.only)]
    if args.only and not targets:
        print(f"没有名为 {args.only} 的账号（或它被 enabled:false 关掉了）")
        return 1
    if not targets:
        print("没有启用的账号")
        return 0

    results: list[CheckinResult] = []
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            for site in targets:
                if args.verbose:
                    print(f"[{site.name}] {site.kind} {site.base_url}")
                result = checkin_site(playwright, site, verbose=args.verbose)
                print("  " + result.line())
                results.append(result)
                if site is not targets[-1]:
                    time.sleep(_INTER_ACCOUNT_DELAY_SEC)
    except Exception as e:
        # playwright 起不来、循环里冒出未兜住的异常：不能一声不吭地退出，
        # 要把「这轮没跑完」本身记成一条失败结果，照常写日志、推送。
        done = {r.name for r in results}
        pending = [s.name for s in targets if s.name not in done]
        detail = f"整轮运行中断: {brief_error(e)}"
        if pending:
            detail += f"；未跑的账号: {','.join(pending)}"
        results.append(CheckinResult(_RUN_NAME, FAILED, detail))
        print("  " + results[-1].line())
    finally:
        # 放 finally 里：哪怕 playwright 自己炸了，已经跑完的账号也要留下记录。
        if results:
            write_log(results, args.log)
            push_to_webhook(results, notify_url)

    problems = [r for r in results if r.is_problem]
    if problems:
        print(f"\n{len(problems)}/{len(results)} 个账号需要处理：")
        for result in problems:
            print(f"  - {result.line()}")
            site = by_name.get(result.name)
            if result.status == NEED_LOGIN and site:
                if _is_anyrouter(site):
                    print(
                        f"    修复: 在 {args.config} 的该账号中填入 "
                        '"cookies": {"session": "..."}'
                    )
                else:
                    print(f"    修复: 核对 {args.config} 中该账号的 username/password；"
                          f"profile 丢失或要人工滑块时再用 "
                          f"python tools/daily_checkin.py --login {result.name}")
            elif result.status == CHECKIN_UNKNOWN:
                print("    未执行退出重登，先确认 WAF/网络和当前签到记录，再单独重试")
        return 1
    print(f"\n{len(results)} 个账号全部完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
