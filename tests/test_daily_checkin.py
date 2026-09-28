# -*- coding: utf-8 -*-
"""每日签到脚本的纯逻辑测试（不碰网络、不启浏览器）。

签到脚本最容易出错的不是网络那段，而是**结果判定**：
把「今天已经签过」当成失败会让任务计划每天报红，
把「会话失效」当成失败则会掩盖「该重新登录了」这个真正需要人管的信号。
所以判定被单独抽成了 `classify_response`，这里重点测它。
"""
import contextlib
import email.message
import io
import json
import os
import re
import sys
import time
import types
import unittest
import urllib.error
import urllib.request
import urllib.response
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import daily_checkin as dc

# 用常量而不是字面量：这两个转义在把代码当字符串搬运时最容易被吃掉
LF = bytes([10])
CRLF = bytes([13, 10])


class ClassifyResponseTests(unittest.TestCase):
    def test_success(self):
        status, _ = dc.classify_response(200, json.dumps({"success": True, "message": "签到成功"}))
        self.assertEqual(status, dc.OK)

    def test_already_checked_in_is_not_a_failure(self):
        """每天第二次跑会撞上这个，不该让任务计划报红。"""
        for body in (
            json.dumps({"success": False, "message": "今日已签到"}, ensure_ascii=False),
            json.dumps({"success": False, "message": "请勿重复签到"}, ensure_ascii=False),
            json.dumps({"success": False, "message": "You have already checked in"}),
        ):
            with self.subTest(body=body):
                status, _ = dc.classify_response(200, body)
                self.assertEqual(status, dc.ALREADY)

    def test_401_means_need_login(self):
        status, detail = dc.classify_response(
            401, json.dumps({"success": False, "message": "无权进行此操作，未登录且未提供 access token"},
                            ensure_ascii=False))
        self.assertEqual(status, dc.NEED_LOGIN)
        self.assertIn("失效", detail)

    def test_server_error_is_failure(self):
        for code in (500, 502, 503):
            with self.subTest(code=code):
                self.assertEqual(dc.classify_response(code, "")[0], dc.FAILED)

    def test_explicit_failure(self):
        status, detail = dc.classify_response(
            200, json.dumps({"success": False, "message": "余额不足"}, ensure_ascii=False))
        self.assertEqual(status, dc.FAILED)
        self.assertIn("余额不足", detail)

    def test_404_hints_at_wrong_path(self):
        status, detail = dc.classify_response(
            404, json.dumps({"error": {"message": "Invalid URL (POST /api/user/checkin)"}}))
        self.assertEqual(status, dc.FAILED)
        self.assertIn("checkin_path", detail)

    def test_non_json_body(self):
        self.assertEqual(dc.classify_response(200, "<html>挑战页</html>")[0], dc.FAILED)

    def test_empty_body(self):
        self.assertEqual(dc.classify_response(200, "")[0], dc.FAILED)


class QuotaTests(unittest.TestCase):
    def test_new_api_quota_converted(self):
        """new-api 的 quota 按 500000 = $1 存。"""
        self.assertEqual(dc.extract_quota(json.dumps({"data": {"quota": 2500000}})), "5.00$")

    def test_small_number_still_formatted_as_dollars(self):
        """数值一律带 $：外部 pick_key.ps1 靠「余额 ([0-9.]+)$」读余额。"""
        self.assertEqual(dc.extract_quota(json.dumps({"data": {"quota": 42}})), "0.00$")
        self.assertEqual(dc.extract_quota(json.dumps({"data": {"quota": 5000}})), "0.01$")

    def test_missing_or_broken(self):
        for body in ("", "not json", json.dumps({"data": {}}), json.dumps({})):
            with self.subTest(body=body):
                self.assertEqual(dc.extract_quota(body), "")


class ResultTests(unittest.TestCase):
    def test_only_real_problems_count(self):
        self.assertFalse(dc.CheckinResult("a", dc.OK).is_problem)
        self.assertFalse(dc.CheckinResult("a", dc.ALREADY).is_problem)
        self.assertTrue(dc.CheckinResult("a", dc.NEED_LOGIN).is_problem)
        self.assertTrue(dc.CheckinResult("a", dc.FAILED).is_problem)
        self.assertTrue(dc.CheckinResult("a", dc.CHECKIN_UNKNOWN).is_problem)

    def test_unverified_is_not_a_problem(self):
        """撞滑块但登录已成功：签到通常已完成，不该让任务计划报红。"""
        self.assertFalse(dc.CheckinResult("a", dc.UNVERIFIED).is_problem)
        self.assertIn(dc.UNVERIFIED, dc._STATUS_LABEL)

    def test_line_is_readable(self):
        line = dc.CheckinResult("anyrouter-1", dc.OK, "签到成功", "5.00$").line()
        self.assertIn("anyrouter-1", line)
        self.assertIn("签到成功", line)
        self.assertIn("5.00$", line)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp())

    def _write(self, payload, encoding="utf-8"):
        path = self.dir / "sites.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding=encoding)
        return path

    def test_loads_and_defaults(self):
        sites = dc.load_sites(self._write([
            {"name": "a", "base_url": "https://a.com"},
        ]))
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0].kind, "refresh")
        self.assertEqual(sites[0].checkin_path, "/api/user/sign_in")
        self.assertEqual(sites[0].checkin_method, "POST")
        self.assertEqual(sites[0].api_user_header, "New-Api-User")
        self.assertTrue(sites[0].enabled)

    def test_bom_is_tolerated(self):
        """记事本存的 JSON 常带 BOM，踩过一次。"""
        sites = dc.load_sites(self._write([{"name": "a", "base_url": "https://a.com"}],
                                          encoding="utf-8-sig"))
        self.assertEqual(sites[0].name, "a")

    def test_unknown_field_is_rejected(self):
        """字段拼错却被静默忽略，是最难查的一类配置问题。"""
        with self.assertRaises(SystemExit) as caught:
            dc.load_sites(self._write([
                {"name": "a", "base_url": "https://a.com", "proxu": "typo"},
            ]))
        self.assertIn("proxu", str(caught.exception))

    def test_missing_required_field(self):
        for payload in ([{"base_url": "https://a.com"}], [{"name": "a"}]):
            with self.subTest(payload=payload):
                with self.assertRaises(SystemExit):
                    dc.load_sites(self._write(payload))

    def test_bad_kind_rejected(self):
        with self.assertRaises(SystemExit):
            dc.load_sites(self._write([
                {"name": "a", "base_url": "https://a.com", "kind": "whatever"},
            ]))

    def test_profile_dir_isolated_per_account(self):
        sites = dc.load_sites(self._write([
            {"name": "a1", "base_url": "https://a.com", "profile": "p1"},
            {"name": "a2", "base_url": "https://a.com", "profile": "p2"},
        ]))
        self.assertNotEqual(sites[0].profile_dir, sites[1].profile_dir)

    def test_profile_defaults_to_name(self):
        sites = dc.load_sites(self._write([{"name": "a1", "base_url": "https://a.com"}]))
        self.assertTrue(str(sites[0].profile_dir).endswith("a1"))

    def test_missing_config_file_is_actionable(self):
        with self.assertRaises(SystemExit) as caught:
            dc.load_sites(self.dir / "nope.json")
        self.assertIn("example", str(caught.exception))


class ShippedExampleConfigTests(unittest.TestCase):
    """随仓库发的示例配置必须自身合法，否则照抄就报错。"""

    def test_example_parses(self):
        example = Path(dc.ROOT) / "tools" / "checkin_sites.example.json"
        sites = dc.load_sites(example, allow_placeholders=True)
        self.assertEqual(len(sites), 4, "两个站各两个账号")
        for site in sites:
            self.assertIn(site.channel, ("msedge", "chrome"),
                          "必须用真实安装的浏览器：内置 Chromium 会被 hCaptcha 认出来")
        kinds = {s.base_url: s.kind for s in sites}
        self.assertEqual(kinds["https://anyrouter.top"], "refresh")
        self.assertEqual(kinds["https://agentrouter.org"], "relogin")
        for site in sites:
            self.assertTrue(site.proxy, f"{site.name} 必须配代理：两个站直连都不通")

    def test_example_shows_the_anyrouter_cookie_fields(self):
        """AnyRouter 靠配置里的 session 认证，示例必须把这几个字段写出来。

        不写的话新装一次就会重踩「值填进 session_cookie」的坑。
        """
        example = Path(dc.ROOT) / "tools" / "checkin_sites.example.json"
        sites = json.loads(example.read_text(encoding="utf-8"))
        site = next(x for x in sites if x["name"] == "anyrouter-1")
        self.assertIn("session", site["cookies"])
        self.assertEqual(site["session_cookie"], "session",
                         "session_cookie 存的是名字，不是值")
        self.assertIn("api_user", site)

    def test_no_real_credentials_in_example(self):
        """示例里可以有凭据**字段**，但不能有凭据**值**。

        原来按字段名判（不许出现 session/cookie 字样），那是登录态还在
        profile 里时的前提；现在 AnyRouter 的认证就是配置里的 session，
        字段名必然出现，所以改成按值的形态判。
        """
        example = Path(dc.ROOT) / "tools" / "checkin_sites.example.json"
        credential_like = re.compile(r"^[A-Za-z0-9._+/=-]{40,}$")

        def walk(node, path="") :
            if isinstance(node, dict):
                for k, v in node.items():
                    walk(v, f"{path}.{k}")
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{path}[{i}]")
            elif isinstance(node, str):
                self.assertNotRegex(
                    node, credential_like,
                    f"{path} 看起来是真凭据，示例里只能放占位说明",
                )

        walk(json.loads(example.read_text(encoding="utf-8")))


class BatchFileLineEndingTests(unittest.TestCase):
    """含中文的 .cmd 必须是 CRLF，否则 cmd.exe 解析错乱。

    实测（2026-08-24）：纯 ASCII 的批处理用 LF 没问题，cmd.exe 能容忍；
    但**一旦文件里有中文，LF 就会让它把行拆错**，症状是单个字符被当成命令：

        'CODE' 不是内部或外部命令，也不是可运行的程序

    而且退出码会变成 0——任务计划看不出失败。所以这条必须钉住。
    """

    ROOT = Path(dc.ROOT)

    def _cmd_files(self):
        return sorted(self.ROOT.glob("*.cmd")) + sorted((self.ROOT / "tools").glob("*.cmd"))

    def test_cmd_with_non_ascii_uses_crlf(self):
        offenders = []
        for path in self._cmd_files():
            raw = path.read_bytes()
            bare_lf = raw.count(LF) - raw.count(CRLF)
            has_non_ascii = any(byte > 127 for byte in raw)
            if bare_lf and has_non_ascii:
                offenders.append(path.name)
        self.assertEqual(
            offenders, [],
            "这些 .cmd 含中文却是 LF 换行，cmd.exe 会解析错乱（且退出码变 0）："
            + ", ".join(offenders),
        )

    def test_our_wrapper_is_crlf(self):
        raw = (self.ROOT / "tools" / "daily_checkin.cmd").read_bytes()
        self.assertEqual(raw.count(LF) - raw.count(CRLF), 0)

    def test_wrapper_propagates_exit_code(self):
        """任务计划靠退出码判断成败，包装脚本不能把它吞掉。"""
        text = (self.ROOT / "tools" / "daily_checkin.cmd").read_text(encoding="utf-8")
        self.assertIn("exit /b %CODE%", text)


class LoginFlowTests(unittest.TestCase):
    """登录必须用真实浏览器普通启动，不能带自动化痕迹。

    linux.do 登录页有 hCaptcha。Playwright 启的浏览器（连真实 Edge 也一样）
    `navigator.webdriver` 为 true，hCaptcha 会直接拒绝：
    「Your browser or network settings are blocking hCaptcha」。
    所以 --login 走 subprocess 普通启动，全程零自动化开关。
    """

    def test_finds_installed_browser(self):
        self.assertTrue(dc.find_browser("msedge") or dc.find_browser("chrome"),
                        "至少要能找到 Edge 或 Chrome")

    def test_unknown_channel_returns_empty(self):
        self.assertEqual(dc.find_browser("firefox"), "")
        self.assertEqual(dc.find_browser(""), "")

    def test_login_uses_plain_subprocess_not_playwright(self):
        """这是本次修复的核心：登录路径不能再走 Playwright 有头浏览器。"""
        source = Path(dc.__file__).read_text(encoding="utf-8")
        login = source[source.index("def interactive_login"):source.index("def _verify_login")]
        self.assertIn("subprocess.run", login)
        self.assertNotIn("_launch(", login)
        self.assertNotIn("headless=False", login)

    def test_login_does_not_kill_the_browser(self):
        """强杀会让 cookie 来不及落盘——实测过：terminate 之后 profile 里读不到。"""
        source = Path(dc.__file__).read_text(encoding="utf-8")
        login = source[source.index("def interactive_login"):source.index("def _verify_login")]
        for forbidden in ("terminate()", "kill()", "taskkill"):
            self.assertNotIn(forbidden, login, f"登录流程不该 {forbidden}")

    def test_login_target_defaults_to_console(self):
        site = dc.SiteConfig(name="a", base_url="https://a.com/")
        self.assertEqual(site.login_target, "https://a.com/console")

    def test_login_target_can_be_overridden(self):
        site = dc.SiteConfig(name="a", base_url="https://a.com",
                             login_url="https://a.com/login?x=1")
        self.assertEqual(site.login_target, "https://a.com/login?x=1")

    def test_headless_run_applies_stealth(self):
        """日常跑仍要抹掉 webdriver：站点随时可能加验证码。"""
        source = Path(dc.__file__).read_text(encoding="utf-8")
        launch = source[source.index("def _launch"):source.index("def _settle")]
        self.assertIn("playwright_stealth", launch)
        self.assertIn("channel", launch)


class _FakeContext:
    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self, _url):
        return list(self._cookies)


class _FakePage:
    """够用的假页面：只实现 _get_waf_cookies 真正会碰的那几个方法。

    ``scripts`` 按调用顺序给出每一轮 (cookie 列表, body 文本, ready)，
    让「cookie 齐了但仍是验证页」这个场景可以在没有浏览器时被测到。
    """

    def __init__(self, scripts):
        self._scripts = list(scripts)
        self._step = 0
        self.goto_calls = 0

    def _current(self):
        index = min(self._step, len(self._scripts) - 1)
        return self._scripts[index]

    @property
    def context(self):
        return _FakeContext(self._current()[0])

    def goto(self, _url, **_kwargs):
        self.goto_calls += 1

    def reload(self, **_kwargs):
        self._step += 1

    def wait_for_timeout(self, _ms):
        self._step += 1

    def inner_text(self, _selector):
        return self._current()[1]

    def evaluate(self, script):
        if "navigator.userAgent" in script:
            return "FakeUA/1.0"
        return self._current()[2]


_ALL_WAF = [
    {"name": name, "value": "v", "domain": "anyrouter.top"}
    for name in ("acw_tc", "cdn_sec_tc", "acw_sc__v2")
]


def _anyrouter_site(**kwargs):
    defaults = dict(name="anyrouter-1", base_url="https://anyrouter.top",
                    timeout_ms=1000, settle_ms=1000, waf_budget_ms=0)
    defaults.update(kwargs)
    return dc.SiteConfig(**defaults)


class WafChallengeTests(unittest.TestCase):
    def test_challenge_markers_detected(self):
        for text in ("请进行验证", "为了更好的访问体验，请…", "访问受限",
                     "Access denied", "Verify you are human",
                     '<meta name="aliyun_wa',
                     "Denied by http_auto_ratelimit",
                     "Sorry, you have been blocked",
                     "RuleID: 21888953"):
            with self.subTest(text=text):
                self.assertTrue(dc.is_waf_challenge_page(text))

    def test_rate_limit_page_is_distinguished(self):
        self.assertTrue(dc.is_waf_rate_limited_page(
            "403 Sorry, you have been blocked; Denied by http_auto_ratelimit"
        ))
        self.assertFalse(dc.is_waf_rate_limited_page("登录 AnyRouter"))

    def test_normal_page_is_not_a_challenge(self):
        for text in ("", "登录 AnyRouter", "控制台 余额 756.83$"):
            with self.subTest(text=text):
                self.assertFalse(dc.is_waf_challenge_page(text))

    def test_cookies_present_but_still_on_challenge_page_is_not_passed(self):
        """这是 403 的根因：acw_* 是挑战页自己下发的，齐了不代表过了。"""
        page = _FakePage([(_ALL_WAF, "请进行验证", False)])
        warmup = dc._get_waf_cookies(page, _anyrouter_site())
        self.assertFalse(warmup.passed)
        self.assertEqual(set(warmup.cookies), {"acw_tc", "cdn_sec_tc", "acw_sc__v2"})

    def test_rate_limit_page_stops_warmup_without_reload(self):
        page = _FakePage([
            (_ALL_WAF,
             "403 Sorry, you have been blocked\nDenied by http_auto_ratelimit",
             False),
            (_ALL_WAF, "登录", True),
        ])
        warmup = dc._get_waf_cookies(page, _anyrouter_site())
        self.assertFalse(warmup.passed)
        self.assertTrue(warmup.rate_limited)
        self.assertEqual(page.goto_calls, 1)

    def test_cookies_present_and_page_ready_is_passed(self):
        page = _FakePage([(_ALL_WAF, "登录", True)])
        warmup = dc._get_waf_cookies(page, _anyrouter_site())
        self.assertTrue(warmup.passed)
        self.assertEqual(warmup.user_agent, "FakeUA/1.0")

    def test_page_ready_but_cookies_incomplete_is_not_passed(self):
        page = _FakePage([([_ALL_WAF[0]], "登录", True)])
        warmup = dc._get_waf_cookies(page, _anyrouter_site())
        self.assertFalse(warmup.passed)
        self.assertEqual(set(warmup.cookies), {"acw_tc"})

    def test_challenge_then_ready_within_deadline(self):
        page = _FakePage([
            (_ALL_WAF, "请进行验证", False),
            (_ALL_WAF, "登录", True),
        ])
        warmup = dc._get_waf_cookies(page, _anyrouter_site())
        self.assertTrue(warmup.passed)

    def test_page_probe_failure_counts_as_not_passed(self):
        class _Broken(_FakePage):
            def inner_text(self, _selector):
                raise RuntimeError("page closed")

        page = _Broken([(_ALL_WAF, "登录", True)])
        self.assertFalse(dc._get_waf_cookies(page, _anyrouter_site()).passed)


class AnyrouterTransportTests(unittest.TestCase):
    """AnyRouter 必须走浏览器网络栈——这是 403 的真正分水岭。

    实测（同一上下文、同一组 Cookie、同一代理出口）：
    页内 fetch → 401 未登录；httpx 带同样的 Cookie → 403 Forbidden。
    ESA 认的是 Chrome 的 TLS/HTTP2 指纹，改 header 无效。
    """

    def test_module_no_longer_imports_httpx(self):
        source = Path(dc.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import httpx", source)

    def test_anyrouter_calls_api_through_the_page(self):
        source = Path(dc.__file__).read_text(encoding="utf-8")
        code = source[source.index("def _checkin_anyrouter"):source.index("def _settle")]
        self.assertIn("_call(page", code)
        self.assertNotIn("_httpx_call", code)

    def test_session_cookie_injected_after_waf_passed(self):
        """顺序不能反：先注入再过挑战会把登录态暴露给挑战页那一跳。"""
        source = Path(dc.__file__).read_text(encoding="utf-8")
        code = source[source.index("def _checkin_anyrouter"):source.index("def _settle")]
        self.assertLess(code.index("_get_waf_cookies("), code.index("_inject_session_cookie("))

    def test_inject_session_cookie_scopes_to_site_host(self):
        recorded = []

        class _Ctx:
            def add_cookies(self, cookies):
                recorded.extend(cookies)

        dc._inject_session_cookie(_Ctx(), _anyrouter_site(), {"session": "secret"})
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["domain"], "anyrouter.top")
        self.assertEqual(recorded[0]["path"], "/")
        self.assertTrue(recorded[0]["secure"])


class QuotaAmountTests(unittest.TestCase):
    """AnyRouter 重复签到的响应和首次成功一样，只能靠额度差判断。"""

    def test_reads_raw_quota(self):
        body = json.dumps({"data": {"quota": 390915000}})
        self.assertEqual(dc.quota_amount(body), 390915000.0)

    def test_falls_back_across_key_names(self):
        for key in ("quota", "balance", "remain_quota"):
            with self.subTest(key=key):
                self.assertEqual(dc.quota_amount(json.dumps({"data": {key: 12}})), 12.0)

    def test_booleans_are_not_amounts(self):
        """bool 是 int 的子类，不挡掉会把 success 之类的字段当额度。"""
        self.assertIsNone(dc.quota_amount(json.dumps({"data": {"quota": True}})))

    def test_missing_or_broken_body_returns_none(self):
        for body in ("", "not json", json.dumps({"data": {}}), json.dumps({"data": None})):
            with self.subTest(body=body):
                self.assertIsNone(dc.quota_amount(body))

    def test_non_empty_signin_message_wins_over_quota(self):
        """message 非空即确定本次签到成功，不该再被额度相等推翻。

        实测：首次签到 message 是「签到成功，获得 $25 额度」，重复签到是空串。
        """
        source = Path(dc.__file__).read_text(encoding="utf-8")
        code = source[source.index("def _checkin_anyrouter"):source.index("def _settle")]
        self.assertIn("if status == OK and not detail:", code)

    def test_unchanged_quota_is_reported_as_already(self):
        source = Path(dc.__file__).read_text(encoding="utf-8")
        code = source[source.index("def _checkin_anyrouter"):source.index("def _settle")]
        self.assertIn("after == before", code)
        self.assertIn("ALREADY", code)

    def test_decreased_quota_keeps_the_safe_default(self):
        """额度变少说明期间有消耗，不能据此判定没签上。"""
        source = Path(dc.__file__).read_text(encoding="utf-8")
        code = source[source.index("def _checkin_anyrouter"):source.index("def _settle")]
        self.assertNotIn("after <= before", code)
        self.assertNotIn("after < before", code)


class SessionConfigNormalizationTests(unittest.TestCase):
    """session_cookie 存名字、cookies 存值——这两个字段实际配置时被填反过一次。"""

    def test_normal_config_is_untouched(self):
        cookies, name = dc.normalize_session_config({"session": "abc"}, "session")
        self.assertEqual(cookies, {"session": "abc"})
        self.assertEqual(name, "session")

    def test_value_mistakenly_put_in_session_cookie_is_recovered(self):
        value = "s" * 444
        cookies, name = dc.normalize_session_config({}, value)
        self.assertEqual(cookies, {"session": value})
        self.assertEqual(name, "session")

    def test_existing_cookies_win_over_the_misplaced_value(self):
        """已经填对的不该被误配覆盖。"""
        cookies, name = dc.normalize_session_config({"session": "good"}, "b" * 100)
        self.assertEqual(cookies["session"], "good")
        self.assertEqual(name, "session")

    def test_empty_session_cookie_defaults_to_session(self):
        for raw in ("", None, "   "):
            with self.subTest(raw=raw):
                _, name = dc.normalize_session_config({}, raw)
                self.assertEqual(name, "session")

    def test_custom_short_cookie_name_is_respected(self):
        _, name = dc.normalize_session_config({"sid": "x"}, "sid")
        self.assertEqual(name, "sid")

    def test_cookie_string_form_still_works(self):
        cookies, name = dc.normalize_session_config("session=abc; other=1", "session")
        self.assertEqual(cookies["session"], "abc")
        self.assertEqual(name, "session")


class AgentrouterLoginTests(unittest.TestCase):
    """AgentRouter：每天退出重登才计签到，所以每次跑都全新表单登录。

    OAuth 那条路实测走不通——`/oauth/linuxdo` 不自动跳，点按钮开的弹窗停在
    connect.linux.do 的授权页且 body 为空（headless 下渲染不出来），
    即便 profile 里的 linux.do `_t` 和 `cf_clearance` 都还有效。
    """

    def test_missing_credentials_reports_need_login(self):
        site = dc.SiteConfig(name="agentrouter-1",
                             base_url="https://agentrouter.org")
        result = dc._checkin_agentrouter(None, site)
        self.assertEqual(result.status, dc.NEED_LOGIN)
        self.assertIn("username/password", result.detail)

    def test_relogin_routes_to_the_fresh_login_flow(self):
        """relogin 不论有没有配密码都走密码登录流程，不再落到 OAuth 重登。"""
        for password in ("secret", ""):
            with self.subTest(password=bool(password)):
                site = dc.SiteConfig(name="agentrouter-1", kind="relogin",
                                     base_url="https://agentrouter.org",
                                     password=password)
                with mock.patch.object(dc, "_checkin_agentrouter",
                                       return_value=dc.CheckinResult("x", dc.OK)) as flow, \
                     mock.patch.object(dc, "_launch") as launch:
                    dc.checkin_site(object(), site)
                flow.assert_called_once()
                launch.assert_not_called()

    def test_relogin_without_credentials_never_touches_the_profile(self):
        """缺凭据直接报 NEED_LOGIN，连浏览器都不开，更不会清 Cookie。"""
        site = dc.SiteConfig(name="agentrouter-2", kind="relogin",
                             base_url="https://agentrouter.org")
        with mock.patch.object(dc, "_launch") as launch:
            result = dc.checkin_site(object(), site)
        self.assertEqual(result.status, dc.NEED_LOGIN)
        self.assertIn("username/password", result.detail)
        launch.assert_not_called()

    def test_oauth_relogin_path_is_gone(self):
        """旧 OAuth 重登会整域清 Cookie 且异常不恢复，不能再留着被误走到。"""
        self.assertFalse(hasattr(dc, "_relogin"))
        self.assertFalse(hasattr(dc, "_restore_site_cookies"))

    def test_example_credentialless_agentrouter_is_disabled(self):
        path = Path(dc.__file__).parent / "checkin_sites.example.json"
        for site in json.loads(path.read_text(encoding="utf-8")):
            if site["kind"] == "relogin" and not site.get("password"):
                self.assertFalse(site["enabled"], f"{site['name']} 没有凭据却启用")

    def test_agentrouter_keeps_the_profile(self):
        """必须留 profile：WAF 放行 Cookie 只能靠人工滑块换来，丢了就得再人工。

        一开始设计成临时浏览器（每次全新登录），实测被推翻：
        /api/user/self 被阿里云人工滑块挡着，代理/直连、有头/无头都一样。
        「退出」改成精确清掉登录 Cookie，而不是丢掉整个 profile。
        """
        source = Path(dc.__file__).read_text(encoding="utf-8")
        code = source[source.index("def _checkin_agentrouter"):source.index("def _settle")]
        self.assertIn("_launch(", code)
        self.assertNotIn("_open_temp_page", code)

    def test_logout_drops_login_cookie_but_keeps_waf(self):
        """按名字 + 域精确清，不再整个 context 清空再加回来。"""

        class _Ctx:
            """按 Playwright 的语义实现 clear_cookies 的名字/域过滤。"""

            def __init__(self):
                self.jar = [
                    {"name": "session", "value": "s1", "domain": "agentrouter.org"},
                    {"name": "session", "value": "s2", "domain": ".agentrouter.org"},
                    {"name": "acw_tc", "value": "w", "domain": "agentrouter.org"},
                    {"name": "acw_sc__v2", "value": "w", "domain": "agentrouter.org"},
                    {"name": "session", "value": "x", "domain": "linux.do"},
                    {"name": "_t", "value": "t", "domain": "linux.do"},
                ]

            def cookies(self):
                return list(self.jar)

            def clear_cookies(self, name=None, domain=None, path=None):
                assert name is not None and domain is not None, "不许无条件清空"

                def hit(value, cond):
                    return cond.search(value) if hasattr(cond, "search") else value == cond

                self.jar = [c for c in self.jar
                            if not (hit(c["name"], name) and hit(c["domain"], domain))]

            def add_cookies(self, cookies):
                raise AssertionError("退出不该重新 add_cookies")

        ctx = _Ctx()
        site = dc.SiteConfig(name="agentrouter-1",
                             base_url="https://agentrouter.org")
        dropped = dc._logout_keep_waf(ctx, site)
        self.assertEqual(sorted(c["value"] for c in dropped), ["s1", "s2"])
        left = {(c["name"], c["domain"]) for c in ctx.jar}
        self.assertEqual(left, {("acw_tc", "agentrouter.org"),
                                ("acw_sc__v2", "agentrouter.org"),
                                ("session", "linux.do"), ("_t", "linux.do")})

    def test_agentrouter_does_not_post_a_checkin_endpoint(self):
        """这个站查 /api/user/self 就会触发签到，没有独立签到接口。"""
        source = Path(dc.__file__).read_text(encoding="utf-8")
        code = source[source.index("def _checkin_agentrouter"):source.index("def _settle")]
        self.assertIn("site.self_path", code)
        self.assertNotIn("site.checkin_path", code)

    def test_password_is_never_printed(self):
        source = Path(dc.__file__).read_text(encoding="utf-8")
        code = source[source.index("def _login_with_password"):source.index("def _checkin_agentrouter")]
        for line in code.splitlines():
            if "print(" in line:
                self.assertNotIn("password", line)
                self.assertNotIn("site.username", line)

    def test_example_shows_agentrouter_credential_fields(self):
        path = Path(dc.__file__).parent / "checkin_sites.example.json"
        sites = json.loads(path.read_text(encoding="utf-8"))
        site = next(x for x in sites if x["name"] == "agentrouter-1")
        self.assertIn("username", site)
        self.assertIn("password", site)


class CheckinLogTests(unittest.TestCase):
    """AgentRouter 靠系统日志判「今日已签到」。

    这个站查 /api/user/self 就等于签到，拿不到「签到前」余额来对比，
    而重复签到站点照样返回成功——只有 type=4 的系统日志是可靠依据。
    实测踩过：09:01 已由别处签过，14:28 再跑仍报「签到成功」，余额却没变。
    """

    def _log(self, *entries):
        return json.dumps({"data": {"items": [
            {"content": c, "created_at": t, "type": 4} for c, t in entries
        ]}}, ensure_ascii=False)

    def test_picks_out_checkin_entries(self):
        body = self._log(("每日签到成功，增加额度 ＄25.000000 额度", 1787792514))
        self.assertEqual(dc.parse_checkin_log(body), [1787792514])

    def test_ignores_unrelated_system_entries(self):
        body = self._log(("系统公告更新", 111), ("每日签到成功", 222))
        self.assertEqual(dc.parse_checkin_log(body), [222])

    def test_broken_or_empty_bodies(self):
        for body in ("", "not json", "{}", json.dumps({"data": {}}),
                     json.dumps({"data": {"items": []}}),
                     json.dumps({"data": {"items": ["不是对象"]}})):
            with self.subTest(body=body):
                self.assertEqual(dc.parse_checkin_log(body), [])

    def test_unusable_timestamps_are_dropped(self):
        body = self._log(("每日签到成功", 0), ("每日签到成功", None),
                         ("每日签到成功", "坏值"))
        self.assertEqual(dc.parse_checkin_log(body), [])

    def test_today_range_covers_the_whole_local_day(self):
        start, end = dc._today_range()
        self.assertEqual(end - start, 86399)
        local = time.localtime(start)
        self.assertEqual((local.tm_hour, local.tm_min, local.tm_sec), (0, 0, 0))
        self.assertLessEqual(start, int(time.time()))
        self.assertGreaterEqual(end, int(time.time()))

    def test_agentrouter_consults_the_log_before_claiming_success(self):
        source = Path(dc.__file__).read_text(encoding="utf-8")
        code = source[source.index("def _checkin_agentrouter"):source.index("def _settle")]
        self.assertIn("_checked_in_before", code)
        self.assertIn("ALREADY", code)


class SliderFalseNegativeTests(unittest.TestCase):
    """撞滑块 ≠ 需要重新登录。

    走到滑块分支时表单登录已经成功（登录失败会先行 return NEED_LOGIN），
    只是用来核对余额的那次 fetch 被阿里云滑块挡了；签到本身通常已由登录后
    控制台自发的 /api/user/self 触发。实测踩过：agentrouter-4 当天先报
    「需要人工重新登录」，7 分钟后重跑却显示「本次运行前已签过」——即误报。
    所以这条分支改判「已提交、未核对」，不再让任务计划天天报红。
    """

    def _agentrouter_code(self):
        source = Path(dc.__file__).read_text(encoding="utf-8")
        return source[source.index("def _checkin_agentrouter"):
                      source.index("def _settle")]

    def test_slider_block_reports_unverified_not_need_login(self):
        code = self._agentrouter_code()
        idx = code.index("核对被滑块拦截")
        # 只看承载这条消息的 return 语句本身（状态参数），不受前面解释性注释干扰
        ret = code.rindex("return CheckinResult(", 0, idx)
        stmt = code[ret:idx]
        self.assertIn("UNVERIFIED", stmt)
        self.assertNotIn("NEED_LOGIN", stmt)

    def test_slider_is_retried_after_rewarm(self):
        """滑块间歇性触发，重新预热后要多试几次，别一撞就判失败。"""
        code = self._agentrouter_code()
        self.assertIn("_WAF_REWARM_RETRIES", code)
        self.assertIn("_warm_site", code)

    def test_rewarm_retry_count_is_restrained(self):
        """次数要克制：连着重试会给阿里云限流计数续命。"""
        self.assertLessEqual(dc._WAF_REWARM_RETRIES, 3)
        self.assertGreaterEqual(dc._WAF_REWARM_RETRIES, 1)


class _FakeApiPage:
    """驱动 _call 的假页面：按 URL 子串返回预设的 (status, body)。

    _call 走的是页内 fetch（page.evaluate(script, {url,...})），所以这里的
    evaluate 收两个参数、从 arg['url'] 分派——和管 WAF 预热的 _FakePage 不同。
    """

    def __init__(self, responses):
        self._responses = responses
        self.urls = []

    def evaluate(self, _script, arg=None):
        url = (arg or {}).get("url", "")
        self.urls.append(url)
        for key, (status, body) in self._responses.items():
            if key in url:
                return {"status": status, "body": body}
        return {"status": 404, "body": ""}


class IdempotencyGuardTests(unittest.TestCase):
    """当天已签就别再退出重登：重跑该是空操作，而不是去触发滑块。

    实测踩过：08:14 定时跑 5/6 成功，08:21 手动重跑把已经签好的号退出重登，
    反而集体撞滑块，看着像大面积失败——其实当天都签上了。既然签过了，
    重跑就该在退出重登之前先查系统日志、直接返回。
    """

    def _log_body(self, *stamps):
        return json.dumps({"data": {"items": [
            {"content": "每日签到成功", "created_at": t, "type": 4}
            for t in stamps
        ]}}, ensure_ascii=False)

    def _site(self):
        return dc.SiteConfig(name="agentrouter-1",
                             base_url="https://agentrouter.org",
                             timeout_ms=1000, settle_ms=0, waf_budget_ms=0)

    def test_stamps_present_when_signed_today(self):
        page = _FakeApiPage({"/api/log/self": (200, self._log_body(1787792514))})
        self.assertEqual(dc._today_checkin_stamps(page, self._site(), "1"),
                         [1787792514])

    def test_stamps_empty_when_not_signed_yet(self):
        """读到日志、但今天没有签到记录——是「确定没签」，不是「不知道」。"""
        page = _FakeApiPage({"/api/log/self": (200, self._log_body())})
        self.assertEqual(dc._today_checkin_stamps(page, self._site(), "1"), [])

    def test_stamps_none_when_waf_blocks_the_log(self):
        page = _FakeApiPage({"/api/log/self": (200, "请进行验证")})
        self.assertIsNone(dc._today_checkin_stamps(page, self._site(), "1"))

    def test_stamps_none_on_http_error(self):
        page = _FakeApiPage({"/api/log/self": (403, "")})
        self.assertIsNone(dc._today_checkin_stamps(page, self._site(), "1"))

    def test_stamps_none_on_unexpected_success_body(self):
        page = _FakeApiPage({"/api/log/self": (200, json.dumps({"success": True}))})
        self.assertIsNone(dc._today_checkin_stamps(page, self._site(), "1"))

    def test_checked_in_before_builds_on_the_shared_helper(self):
        """重构后 _checked_in_before 仍靠时间戳判定：早于本次运行才算之前签过。"""
        page = _FakeApiPage({"/api/log/self": (200, self._log_body(1000))})
        site = self._site()
        self.assertTrue(dc._checked_in_before(page, site, "1", 2000))
        self.assertFalse(dc._checked_in_before(page, site, "1", 500))

    def test_checked_in_before_none_when_log_unreadable(self):
        page = _FakeApiPage({"/api/log/self": (200, "请进行验证")})
        self.assertIsNone(dc._checked_in_before(page, self._site(), "1", 999))

    def test_guard_returns_already_before_logout(self):
        """守卫必须落在退出重登之前——否则先退出了再判，等于没守。"""
        source = Path(dc.__file__).read_text(encoding="utf-8")
        code = source[source.index("def _checkin_agentrouter"):
                      source.index("def _settle")]
        precheck = code.index("_precheck_agentrouter")
        already_return = code.index("跳过退出重登", precheck)
        logout = code.index("_logout_keep_waf")
        self.assertLess(precheck, logout, "幂等预检必须在退出重登之前")
        self.assertLess(already_return, logout, "命中已签的返回必须在退出重登之前")

    def test_precheck_distinguishes_signed_and_not_signed(self):
        page = object()
        site = self._site()
        with mock.patch.object(dc, "_has_login_cookie", return_value=True), \
             mock.patch.object(dc, "_warm_site", return_value=True), \
             mock.patch.object(dc, "_browser_api_user", return_value="1"), \
             mock.patch.object(dc, "_read_checkin_log",
                               side_effect=[([1000], "", ""), ([], "", "")]):
            self.assertEqual(dc._precheck_agentrouter(page, site),
                             (True, "1", ""))
            self.assertEqual(dc._precheck_agentrouter(page, site),
                             (False, "1", ""))

    def test_precheck_uses_configured_api_user(self):
        """配置有用户 ID 时，预检不依赖 profile 的 localStorage。"""
        page = object()
        site = self._site()
        site.api_user = "10002"
        with mock.patch.object(dc, "_has_login_cookie", return_value=True), \
             mock.patch.object(dc, "_warm_site", return_value=True), \
             mock.patch.object(dc, "_browser_api_user") as browser_user, \
             mock.patch.object(dc, "_read_checkin_log", return_value=([], "", "")):
            self.assertEqual(dc._precheck_agentrouter(page, site),
                             (False, "10002", ""))
        browser_user.assert_not_called()

    def test_precheck_unknown_is_non_destructive(self):
        page = object()
        site = self._site()
        with mock.patch.object(dc, "_has_login_cookie", return_value=True), \
             mock.patch.object(dc, "_warm_site", return_value=False):
            result = dc._precheck_agentrouter(page, site)
        self.assertIsNone(result[0])
        self.assertIn("未执行退出重登", result[2])

    def test_agentrouter_does_not_logout_when_precheck_unknown(self):
        site = self._site()
        site.username = "user@example.com"
        site.password = "password"
        context = mock.Mock()
        context.pages = [mock.Mock()]
        with mock.patch.object(dc, "_launch", return_value=context), \
             mock.patch.object(dc, "_precheck_agentrouter",
                               return_value=(None, "", "日志不可读，未执行退出重登")), \
             mock.patch.object(dc, "_logout_keep_waf") as logout:
            result = dc._checkin_agentrouter(object(), site)
        self.assertEqual(result.status, dc.CHECKIN_UNKNOWN)
        logout.assert_not_called()
        context.close.assert_called_once()

    def test_agentrouter_relogs_when_precheck_says_not_signed(self):
        """明确未签到时必须继续退出重登，不能把保护误写成永远跳过。"""
        site = self._site()
        site.username = "user@example.com"
        site.password = "password"
        context = mock.Mock()
        context.pages = [mock.Mock()]
        with mock.patch.object(dc, "_launch", return_value=context), \
             mock.patch.object(dc, "_precheck_agentrouter",
                               return_value=(False, "1", "")), \
             mock.patch.object(dc, "_logout_keep_waf",
                               return_value=[{"name": "session"}]) as logout, \
             mock.patch.object(dc, "_warm_site", return_value=True), \
             mock.patch.object(dc, "_login_with_password",
                               return_value=(dc.NEED_LOGIN, "stop after proving relogin")):
            result = dc._checkin_agentrouter(object(), site)
        self.assertEqual(result.status, dc.NEED_LOGIN)
        logout.assert_called_once_with(context, site)
        # 重登没成功：原会话要放回去
        context.add_cookies.assert_called_once_with([{"name": "session"}])
        context.close.assert_called_once()


class RunResilienceTests(unittest.TestCase):
    """一个账号出问题不该拖垮整轮。

    实测踩过：agentrouter-3 的 /login 导航 ERR_TIMED_OUT 让 main() 直接抛出，
    后面的账号一个没跑，前面 3 个已经成功的结果也因为没走到 write_log 而丢失。
    """

    def test_site_branches_catch_exceptions(self):
        source = Path(dc.__file__).read_text(encoding="utf-8")
        start = source.index("def checkin_site")
        code = source[start:source.index(chr(10) + "def ", start + 10)]
        # 两条早退分支都必须自己兜住异常，否则异常会一路冲到 main()
        self.assertEqual(code.count("except Exception as e:"), 3)
        for call in ("_checkin_anyrouter(", "_checkin_agentrouter("):
            with self.subTest(call=call):
                self.assertLess(code.index("try:"), code.index(call),
                                f"{call} 必须在 try 里")

    def test_log_is_written_even_when_the_loop_dies(self):
        source = Path(dc.__file__).read_text(encoding="utf-8")
        start = source.index("def main(")
        code = source[start:]
        finally_at = code.index("    finally:")
        write_at = code.index("write_log(results, args.log)")
        self.assertLess(finally_at, write_at, "write_log 必须在 finally 里")

    def test_login_page_is_retried(self):
        """网络抖动导致的一次失败不该判成账号问题。"""
        source = Path(dc.__file__).read_text(encoding="utf-8")
        start = source.index("def _open_login_page")
        code = source[start:source.index(chr(10) + "def ", start + 10)]
        self.assertIn("attempts", code)
        self.assertIn("for attempt in range", code)

    def test_login_retry_reports_the_last_reason(self):
        source = Path(dc.__file__).read_text(encoding="utf-8")
        start = source.index("def _open_login_page")
        code = source[start:source.index(chr(10) + "def ", start + 10)]
        self.assertIn("登录页打不开", code)
        self.assertIn("登录页找不到邮箱/用户名表单", code)



# =========================================================================== 会话保护 / 通知 / 配置校验

def _agentrouter_site(**kwargs):
    defaults = dict(name="agentrouter-1", kind="relogin",
                    base_url="https://agentrouter.org",
                    timeout_ms=1000, settle_ms=0, waf_budget_ms=0)
    defaults.update(kwargs)
    return dc.SiteConfig(**defaults)


def _api_body(message, success=False):
    return json.dumps({"success": success, "message": message}, ensure_ascii=False)


class PrecheckSessionTests(unittest.TestCase):
    """没有会话就没什么要保护的；401 要分清「会话失效」和「api_user 配错」。"""

    def test_no_session_cookie_skips_precheck_and_logs_in(self):
        with mock.patch.object(dc, "_has_login_cookie", return_value=False), \
             mock.patch.object(dc, "_warm_site") as warm, \
             mock.patch.object(dc, "_read_checkin_log") as read_log:
            precheck, _, reason = dc._precheck_agentrouter(object(), _agentrouter_site())
        self.assertIs(precheck, False)
        self.assertIn("跳过预检", reason)
        warm.assert_not_called()
        read_log.assert_not_called()

    def test_has_login_cookie_only_counts_this_site(self):
        class _Page:
            def __init__(self, cookies):
                self.context = _FakeContext(cookies)

        site = _agentrouter_site()
        self.assertFalse(dc._has_login_cookie(_Page([
            {"name": "acw_tc", "value": "v", "domain": "agentrouter.org"},
            {"name": "session", "value": "v", "domain": "linux.do"},
        ]), site))
        self.assertTrue(dc._has_login_cookie(_Page([
            {"name": "session", "value": "v", "domain": ".agentrouter.org"},
        ]), site))

    def _precheck_with_log(self, status, body):
        page = _FakeApiPage({"/api/log/self": (status, body)})
        with mock.patch.object(dc, "_has_login_cookie", return_value=True), \
             mock.patch.object(dc, "_warm_site", return_value=True):
            return dc._precheck_agentrouter(page, _agentrouter_site(api_user="1"))

    def test_401_not_logged_in_allows_relogin(self):
        for message in ("无权进行此操作，未登录且未提供 access token",
                        "无权进行此操作，access token 无效"):
            with self.subTest(message=message):
                precheck, api_user, reason = self._precheck_with_log(401, _api_body(message))
                self.assertIs(precheck, False)
                self.assertEqual(api_user, "1")
                self.assertIn("会话已失效", reason)

    def test_invalid_token_with_http_200_also_counts_as_expired(self):
        """new-api 对 access token 无效回的是 200 + success:false。"""
        precheck, _, _ = self._precheck_with_log(
            200, _api_body("无权进行此操作，access token 无效"))
        self.assertIs(precheck, False)

    def test_401_api_user_mismatch_keeps_unknown(self):
        precheck, _, reason = self._precheck_with_log(
            401, _api_body("无权进行此操作，New-Api-User 与登录用户不匹配"))
        self.assertIsNone(precheck)
        self.assertIn("api_user 配置有误", reason)
        self.assertIn("未执行退出重登", reason)

    def test_unrecognized_401_keeps_unknown(self):
        precheck, _, reason = self._precheck_with_log(401, "")
        self.assertIsNone(precheck)
        self.assertIn("未执行退出重登", reason)

    def test_auth_failure_kind(self):
        self.assertEqual(dc.auth_failure_kind("无权进行此操作，未提供 New-Api-User"),
                         dc.AUTH_API_USER)
        self.assertEqual(dc.auth_failure_kind("无权进行此操作，New-Api-User 格式错误"),
                         dc.AUTH_API_USER)
        self.assertEqual(dc.auth_failure_kind("无权进行此操作，未登录且未提供 access token"),
                         dc.AUTH_EXPIRED)
        self.assertEqual(dc.auth_failure_kind("用户已被封禁"), "")
        self.assertEqual(dc.auth_failure_kind(""), "")


class ReloginRollbackTests(unittest.TestCase):
    """退出之后任何一步没走通，都要把原会话放回去。"""

    SAVED = [{"name": "session", "value": "old", "domain": "agentrouter.org", "path": "/"}]

    def _run(self, *, warm=True, login=None, login_error=None):
        site = _agentrouter_site(username="user@example.com", password="pw")
        context = mock.Mock()
        context.pages = [mock.Mock()]
        login_mock = mock.Mock(return_value=login, side_effect=login_error)
        with mock.patch.object(dc, "_launch", return_value=context), \
             mock.patch.object(dc, "_precheck_agentrouter", return_value=(False, "1", "")), \
             mock.patch.object(dc, "_logout_keep_waf", return_value=list(self.SAVED)), \
             mock.patch.object(dc, "_warm_site", return_value=warm), \
             mock.patch.object(dc, "_login_with_password", login_mock):
            result = dc._checkin_agentrouter(object(), site)
        return result, context, login_mock

    def test_navigation_failure_is_failed_and_restores_session(self):
        result, context, _ = self._run(
            login=(dc.FAILED, "登录页打不开: TimeoutError（重试 3 次）"))
        self.assertEqual(result.status, dc.FAILED)
        self.assertIn("已恢复原登录态", result.detail)
        context.add_cookies.assert_called_once_with(self.SAVED)

    def test_credential_error_is_need_login_and_restores_session(self):
        result, context, _ = self._run(
            login=(dc.NEED_LOGIN, "登录未完成（仍停在登录页）: 用户名或密码错误"))
        self.assertEqual(result.status, dc.NEED_LOGIN)
        context.add_cookies.assert_called_once_with(self.SAVED)

    def test_post_logout_warmup_failure_skips_login(self):
        result, context, login = self._run(warm=False)
        self.assertEqual(result.status, dc.FAILED)
        self.assertIn("未尝试登录", result.detail)
        login.assert_not_called()
        context.add_cookies.assert_called_once_with(self.SAVED)

    def test_exception_during_login_restores_session(self):
        """登录中途抛异常：先恢复会话再往上抛，由 checkin_site 兜成 FAILED。"""
        site = _agentrouter_site(username="user@example.com", password="pw")
        context = mock.Mock()
        context.pages = [mock.Mock()]
        with mock.patch.object(dc, "_launch", return_value=context), \
             mock.patch.object(dc, "_precheck_agentrouter", return_value=(False, "1", "")), \
             mock.patch.object(dc, "_logout_keep_waf", return_value=list(self.SAVED)), \
             mock.patch.object(dc, "_warm_site", return_value=True), \
             mock.patch.object(dc, "_login_with_password",
                               side_effect=RuntimeError("page crashed")):
            result = dc.checkin_site(object(), site)
        self.assertEqual(result.status, dc.FAILED)
        self.assertIn("page crashed", result.detail)
        context.add_cookies.assert_called_once_with(self.SAVED)
        context.close.assert_called_once()


class _LoginPage:
    """_login_with_password 用到的最小页面：停在登录页，body 文本固定。"""

    def __init__(self, body, url="https://agentrouter.org/login", cookies=()):
        self.url = url
        self._body = body
        self.context = _FakeContext(list(cookies))

    def inner_text(self, _selector):
        return self._body

    def wait_for_timeout(self, _ms):
        pass


class LoginOutcomeTests(unittest.TestCase):
    """只有「仍停在登录页且报了凭据错误」才是 NEED_LOGIN。"""

    def _login(self, page, opened=(True, "")):
        clock = iter(range(0, 10_000, 5))
        with mock.patch.object(dc, "_open_login_page", return_value=opened), \
             mock.patch.object(dc, "_first_visible", return_value=mock.Mock()), \
             mock.patch.object(dc.time, "monotonic", side_effect=lambda: next(clock)):
            return dc._login_with_password(page, _agentrouter_site(username="u", password="p"))

    def test_login_page_unreachable_is_failed(self):
        status, reason = self._login(_LoginPage(""), opened=(False, "登录页打不开: TimeoutError"))
        self.assertEqual(status, dc.FAILED)
        self.assertIn("登录页打不开", reason)

    def test_credential_error_is_need_login(self):
        status, reason = self._login(_LoginPage("用户名或密码错误，或用户已被封禁"))
        self.assertEqual(status, dc.NEED_LOGIN)
        self.assertIn("登录未完成（仍停在登录页）", reason)

    def test_stuck_without_credential_error_is_failed(self):
        status, reason = self._login(_LoginPage("登录 AgentRouter"))
        self.assertEqual(status, dc.FAILED)
        self.assertIn("登录未完成（仍停在登录页）", reason)

    def test_session_and_redirect_is_ok(self):
        page = _LoginPage("控制台", url="https://agentrouter.org/console",
                          cookies=[{"name": "session", "value": "v",
                                    "domain": "agentrouter.org"}])
        self.assertEqual(self._login(page), (dc.OK, ""))


class LogLineCompatibilityTests(unittest.TestCase):
    """E:\\claude-router\\pick_key.ps1 按这个正则从日志里读余额，格式不能漂。"""

    PATTERNS = {
        "agentrouter-": re.compile(r"(agentrouter-[^:]+): .*?余额 ([0-9.]+)\$"),
        "anyrouter-": re.compile(r"(anyrouter-[^:]+): .*?余额 ([0-9.]+)\$"),
    }

    def _line(self, name, status, raw_quota, detail=""):
        quota = dc.extract_quota(json.dumps({"data": {"quota": raw_quota}}))
        return dc.CheckinResult(name, status, detail, quota).line()

    def test_ok_already_and_small_balances_match(self):
        cases = [
            ("agentrouter-1", dc.OK, 533450000, "", "1066.90"),
            ("agentrouter-2", dc.ALREADY, 361970000, "今日已签到（系统日志显示本次运行前已签过）",
             "723.94"),
            ("agentrouter-3", dc.OK, 42, "", "0.00"),
            ("anyrouter-1", dc.OK, 703415000, "签到成功，获得 $25 额度", "1406.83"),
            ("anyrouter-2", dc.ALREADY, 5000, "今日已签到（额度未变化）", "0.01"),
        ]
        for name, status, raw, detail, expected in cases:
            with self.subTest(name=name, raw=raw):
                line = f"2026-09-28 08:10:25 {self._line(name, status, raw, detail)}"
                prefix = "agentrouter-" if name.startswith("agentrouter-") else "anyrouter-"
                match = self.PATTERNS[prefix].search(line)
                self.assertIsNotNone(match, line)
                self.assertEqual(match.group(1), name)
                self.assertEqual(match.group(2), expected)

    def test_failure_lines_have_no_balance(self):
        line = dc.CheckinResult("agentrouter-1", dc.FAILED, "WAF/限流未通过").line()
        self.assertIsNone(self.PATTERNS["agentrouter-"].search(line))

    def test_run_level_failures_do_not_look_like_accounts(self):
        line = dc.CheckinResult(dc._RUN_NAME, dc.FAILED, "整轮运行中断").line()
        for pattern in self.PATTERNS.values():
            self.assertIsNone(pattern.search(line))


class _FakeSyncApi(types.ModuleType):
    """代替 playwright.sync_api：sync_playwright() 的上下文进入时抛出指定异常。"""

    def __init__(self, error):
        super().__init__("playwright.sync_api")
        self._error = error

    def sync_playwright(self):
        error = self._error

        class _Ctx:
            def __enter__(self):
                if error:
                    raise error
                return object()

            def __exit__(self, *exc):
                return False

        return _Ctx()


class MainRunTests(unittest.TestCase):
    """整轮崩溃、配置错误、旧参数都要留日志、发通知，不能静默退出。"""

    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp())
        self.log = self.dir / "run.log"
        self.config = self.dir / "sites.json"
        self.config.write_text(json.dumps([
            {"name": "anyrouter-1", "base_url": "https://anyrouter.top",
             "cookies": {"session": "abc"}, "api_user": "1"},
            {"name": "anyrouter-2", "base_url": "https://anyrouter.top",
             "cookies": {"session": "def"}, "api_user": "2"},
        ]), encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {dc._NOTIFY_ENV: ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _main(self, *argv, playwright_error=None):
        fake = _FakeSyncApi(playwright_error)
        pushed = []
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"playwright.sync_api": fake}), \
             mock.patch.object(dc, "push_to_webhook",
                               side_effect=lambda results, url: pushed.append((results, url))), \
             mock.patch.object(dc, "_INTER_ACCOUNT_DELAY_SEC", 0), \
             contextlib.redirect_stdout(out):
            code = dc.main(["--config", str(self.config), "--log", str(self.log), *argv])
        return code, pushed

    def _log_text(self):
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def test_playwright_start_failure_is_logged_and_pushed(self):
        code, pushed = self._main("--notify-url", "https://hook.example/x",
                                  playwright_error=RuntimeError("Executable doesn't exist\ncall log"))
        self.assertEqual(code, 1)
        self.assertIn("daily_checkin: 失败 | 整轮运行中断: RuntimeError", self._log_text())
        self.assertIn("anyrouter-1,anyrouter-2", self._log_text())
        self.assertNotIn("call log", self._log_text(), "只留异常首行")
        self.assertEqual(len(pushed), 1)
        self.assertEqual(pushed[0][1], "https://hook.example/x")

    def test_loop_exception_keeps_finished_results(self):
        results = iter([dc.CheckinResult("anyrouter-1", dc.OK, "", "1.00$")])

        def fake_checkin(_pw, site, verbose=False):
            if site.name == "anyrouter-2":
                raise KeyError("boom")
            return next(results)

        with mock.patch.object(dc, "checkin_site", side_effect=fake_checkin):
            code, pushed = self._main()
        self.assertEqual(code, 1)
        log = self._log_text()
        self.assertIn("anyrouter-1: 签到成功 | 余额 1.00$", log)
        self.assertIn("整轮运行中断: KeyError", log)
        self.assertIn("未跑的账号: anyrouter-2", log)
        self.assertEqual([r.name for r in pushed[0][0]], ["anyrouter-1", dc._RUN_NAME])

    def test_env_var_is_used_when_no_notify_url(self):
        os.environ[dc._NOTIFY_ENV] = "https://env.example/hook"
        with mock.patch.object(dc, "checkin_site",
                               return_value=dc.CheckinResult("a", dc.OK)):
            _, pushed = self._main()
        self.assertEqual(pushed[0][1], "https://env.example/hook")

    def test_cli_notify_url_wins_over_env(self):
        os.environ[dc._NOTIFY_ENV] = "https://env.example/hook"
        self.assertEqual(dc.resolve_notify_url("https://cli.example/"), "https://cli.example/")
        self.assertEqual(dc.resolve_notify_url(""), "https://env.example/hook")

    def test_config_error_is_logged_and_pushed(self):
        self.config.write_text("{不是 JSON", encoding="utf-8")
        code, pushed = self._main("--notify-url", "https://hook.example/x")
        self.assertEqual(code, 2)
        self.assertIn("配置加载失败", self._log_text())
        self.assertEqual(len(pushed), 1)

    def test_config_placeholder_error_is_logged(self):
        self.config.write_text(json.dumps([
            {"name": "agentrouter-1", "kind": "relogin", "base_url": "https://agentrouter.org",
             "username": "在这里填 AgentRouter 的邮箱或用户名", "password": "x"},
        ], ensure_ascii=False), encoding="utf-8")
        code, _ = self._main()
        self.assertEqual(code, 2)
        self.assertIn("示例占位内容", self._log_text())

    def test_legacy_notify_fails_loudly(self):
        code, pushed = self._main("--notify", "wxid_abc")
        self.assertEqual(code, 2)
        self.assertIn("--notify 已废弃", self._log_text())
        self.assertIn("--notify-url", self._log_text())
        self.assertEqual(pushed, [([mock.ANY], "")])

    def test_legacy_notify_is_hidden_from_help(self):
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            dc.parse_args(["--help"])
        self.assertNotIn("WXID", out.getvalue())
        self.assertIn("--notify-url", out.getvalue())


def _NO_PROXY():
    # 本机若设了 HTTP_PROXY，默认 ProxyHandler 会改写请求；测试里显式关掉
    return urllib.request.ProxyHandler({})


class _Fake302(urllib.request.BaseHandler):
    """假的 HTTP 层：每个请求都回 302，记录收到的方法，用来证明没有跟随。"""

    handler_order = 100

    def __init__(self):
        self.methods = []

    def http_open(self, req):
        self.methods.append(req.get_method())
        headers = email.message.Message()
        headers["Location"] = "http://hook.example/elsewhere"
        response = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
        response.msg = "Found"
        return response


class WebhookTests(unittest.TestCase):
    """只允许 http/https；3xx 当失败；payload 是约定的 JSON。"""

    def _push(self, url, opener=None, error=None):
        results = [dc.CheckinResult("anyrouter-1", dc.OK, "", "1.00$")]
        opener = opener or mock.MagicMock()
        if error is not None:
            opener.open.side_effect = error
        out = io.StringIO()
        with mock.patch.object(dc, "build_opener", return_value=opener) as build, \
             contextlib.redirect_stdout(out):
            ok = dc.push_to_webhook(results, url)
        return ok, opener, build, out.getvalue()

    def test_posts_json_payload(self):
        ok, opener, build, _ = self._push("https://hook.example/x")
        self.assertTrue(ok)
        build.assert_called_once_with(dc._RejectRedirect)
        request = opener.open.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Content-type"), "application/json; charset=utf-8")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["content"], payload["text"])
        self.assertTrue(payload["content"].startswith("每日签到 "))
        self.assertIn("anyrouter-1: 签到成功 | 余额 1.00$", payload["content"])

    def test_non_http_scheme_is_refused(self):
        for url in ("file:///C:/Windows/win.ini", "ftp://x/", "javascript:alert(1)"):
            with self.subTest(url=url):
                ok, opener, build, out = self._push(url)
                self.assertFalse(ok)
                build.assert_not_called()
                self.assertIn("http/https", out)

    def test_http_error_is_reported(self):
        error = urllib.error.HTTPError("https://hook.example/x", 500, "boom", {}, io.BytesIO())
        ok, _, _, out = self._push("https://hook.example/x", error=error)
        self.assertFalse(ok)
        self.assertIn("HTTP 500", out)

    def test_network_error_is_reported(self):
        ok, _, _, out = self._push("https://hook.example/x",
                                   error=urllib.error.URLError("timed out"))
        self.assertFalse(ok)
        self.assertIn("推送失败", out)

    def test_redirect_is_not_followed(self):
        """真实 urllib 链路：默认会跟随 302 并改成 GET；我们的 opener 必须当失败。"""
        fake = _Fake302()
        opener = urllib.request.build_opener(dc._RejectRedirect, fake, _NO_PROXY())
        ok, _, _, out = self._push("http://hook.example/x", opener=opener)
        self.assertFalse(ok)
        self.assertEqual(fake.methods, ["POST"], "不能跟随重定向再发一次 GET")
        self.assertIn("重定向 HTTP 302", out)

        # 对照组：不装 _RejectRedirect 时 urllib 确实会把 POST 变成 GET 跟过去
        contrast = _Fake302()
        request = urllib.request.Request("http://hook.example/x", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.build_opener(contrast, _NO_PROXY()).open(request, timeout=1)
        caught.exception.close()
        self.assertEqual(contrast.methods[:2], ["POST", "GET"])


class ClassifyJsonMessageTests(unittest.TestCase):
    """「已签过」只看 JSON message；401 要带上服务端原话。"""

    def test_already_word_in_json_data_does_not_count(self):
        body = json.dumps({"success": True, "message": "",
                           "data": {"username": "already_here", "group": "checked in"}})
        self.assertEqual(dc.classify_response(200, body)[0], dc.OK)

    def test_already_in_json_message_counts(self):
        body = json.dumps({"success": False, "message": "今天已经签到过了"}, ensure_ascii=False)
        self.assertEqual(dc.classify_response(200, body)[0], dc.ALREADY)

    def test_non_json_body_falls_back_to_raw_text(self):
        self.assertEqual(dc.classify_response(200, "今日已签到")[0], dc.ALREADY)

    def test_self_step_never_reports_already(self):
        body = json.dumps({"success": False, "message": "今日已签到"}, ensure_ascii=False)
        status, _ = dc.classify_response(200, body, allow_already=False)
        self.assertEqual(status, dc.FAILED)

    def test_anyrouter_self_step_uses_allow_already_false(self):
        source = Path(dc.__file__).read_text(encoding="utf-8")
        code = source[source.index("def _checkin_anyrouter"):source.index("_USERNAME_SELECTORS")]
        self.assertIn("classify_response(self_code, self_body, allow_already=False)", code)

    def test_401_detail_includes_server_message(self):
        status, detail = dc.classify_response(
            401, _api_body("无权进行此操作，未登录且未提供 access token"))
        self.assertEqual(status, dc.NEED_LOGIN)
        self.assertIn("未登录且未提供 access token", detail)

    def test_401_api_user_problem_is_not_need_login(self):
        """会话其实有效、是 api_user 配错了——提示去重新取 session 会把人带偏。"""
        status, detail = dc.classify_response(
            401, _api_body("无权进行此操作，未提供 New-Api-User"))
        self.assertEqual(status, dc.FAILED)
        self.assertIn("api_user", detail)
        self.assertIn("未提供 New-Api-User", detail)


class WarmupNavigationCapTests(unittest.TestCase):
    """预热导航有上限，限流页一出现就停。"""

    def test_get_waf_cookies_caps_navigations(self):
        page = _FakePage([(_ALL_WAF, "请进行验证", False)])
        site = _anyrouter_site(settle_ms=0, waf_budget_ms=600_000)
        self.assertFalse(dc._get_waf_cookies(page, site).passed)
        self.assertEqual(page.goto_calls, dc._WAF_MAX_NAVIGATIONS)
        self.assertLessEqual(dc._WAF_MAX_NAVIGATIONS, 4)

    def test_get_waf_cookies_no_longer_reloads(self):
        source = Path(dc.__file__).read_text(encoding="utf-8")
        start = source.index("def _get_waf_cookies")
        code = source[start:source.index(chr(10) + "def ", start + 10)]
        self.assertNotIn("page.reload(", code)

    def test_rate_limit_after_later_navigation_stops_immediately(self):
        class _Page(_FakePage):
            def goto(self, _url, **_kwargs):
                self.goto_calls += 1
                self._step = self.goto_calls - 1

        page = _Page([
            (_ALL_WAF, "请进行验证", False),
            (_ALL_WAF, "Denied by http_auto_ratelimit", False),
            (_ALL_WAF, "登录", True),
        ])
        site = _anyrouter_site(settle_ms=0, waf_budget_ms=600_000)
        warmup = dc._get_waf_cookies(page, site)
        self.assertTrue(warmup.rate_limited)
        self.assertEqual(page.goto_calls, 2)

    def test_warm_site_caps_navigations_and_stops_on_rate_limit(self):
        page = _FakePage([([], "请进行验证", False)])
        site = _agentrouter_site(waf_budget_ms=600_000)
        self.assertFalse(dc._warm_site(page, site))
        self.assertEqual(page.goto_calls, dc._WAF_MAX_NAVIGATIONS)

        limited = _FakePage([([], "Sorry, you have been blocked", False)])
        self.assertFalse(dc._warm_site(limited, site))
        self.assertEqual(limited.goto_calls, 1)


class PlaceholderConfigTests(unittest.TestCase):
    """照抄模板没改就直接报错，而不是带着占位串去请求。"""

    def setUp(self):
        import tempfile
        self.dir = Path(tempfile.mkdtemp())

    def _load(self, payload):
        path = self.dir / "sites.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return dc.load_sites(path)

    def test_placeholders_are_rejected(self):
        cases = [
            {"api_user": "AgentRouter 用户 ID"},
            {"username": "在这里填 AgentRouter 的邮箱或用户名"},
            {"cookies": {"session": "在这里填 session 的【值】"}},
            {"cookies": "session=SESSION_VALUE"},
        ]
        for extra in cases:
            with self.subTest(extra=extra):
                item = {"name": "a", "base_url": "https://a.com", **extra}
                with self.assertRaises(SystemExit) as caught:
                    self._load([item])
                self.assertIn("示例占位内容", str(caught.exception))

    def test_disabled_accounts_may_keep_placeholders(self):
        sites = self._load([{"name": "a", "base_url": "https://a.com", "enabled": False,
                             "api_user": "AgentRouter 用户 ID"}])
        self.assertEqual(len(sites), 1)

    def test_shipped_example_is_rejected_until_filled(self):
        example = Path(dc.ROOT) / "tools" / "checkin_sites.example.json"
        with self.assertRaises(SystemExit):
            dc.load_sites(example)

    def test_config_path_cannot_be_set_from_json(self):
        with self.assertRaises(SystemExit):
            self._load([{"name": "a", "base_url": "https://a.com", "config_path": "x"}])

    def test_messages_use_the_config_path_in_use(self):
        """提示里写实际的配置路径，而不是写死 tools/checkin_sites.json。"""
        sites = self._load([{"name": "anyrouter-1", "base_url": "https://anyrouter.top"}])
        result = dc._checkin_anyrouter(None, sites[0])
        self.assertEqual(result.status, dc.NEED_LOGIN)
        self.assertIn(str(self.dir / "sites.json"), result.detail)


class CallFallbackTests(unittest.TestCase):
    """只有执行上下文被销毁才退回 page.request，其余异常照常抛出。"""

    def _page(self, error):
        page = mock.Mock()
        page.evaluate.side_effect = error
        response = mock.Mock(status=200)
        response.text.return_value = "{}"
        page.request.fetch.return_value = response
        return page

    def test_context_destroyed_falls_back(self):
        page = self._page(RuntimeError(
            "Execution context was destroyed, most likely because of a navigation"))
        self.assertEqual(dc._call(page, _anyrouter_site(), "/api/user/self"), (200, "{}"))
        page.request.fetch.assert_called_once()

    def test_other_errors_are_not_masked(self):
        page = self._page(RuntimeError("Target page, context or browser has been closed"))
        with self.assertRaises(RuntimeError):
            dc._call(page, _anyrouter_site(), "/api/user/self")
        page.request.fetch.assert_not_called()


class AnyrouterRateLimitOrderTests(unittest.TestCase):
    """被限流时 Cookie 本来就不全，先报限流才不会把人带偏。"""

    def test_rate_limit_reported_before_missing_cookies(self):
        site = _anyrouter_site(cookies={"session": "abc"}, api_user="1")
        browser, context, page = mock.Mock(), mock.Mock(), mock.Mock()
        with mock.patch.object(dc, "_open_temp_page", return_value=(browser, context, page)), \
             mock.patch.object(dc, "_get_waf_cookies",
                               return_value=dc.WafWarmup({}, "", False, True)):
            result = dc._checkin_anyrouter(object(), site)
        self.assertEqual(result.status, dc.FAILED)
        self.assertIn("ESA 自动限流", result.detail)
        self.assertNotIn("不完整", result.detail)
        context.close.assert_called_once()
        browser.close.assert_called_once()


class BriefErrorTests(unittest.TestCase):
    """异常摘要只留首行，Cookie/session 值要打码。"""

    def test_first_line_only(self):
        text = dc.brief_error(RuntimeError("Timeout 45000ms exceeded.\n=== logs ===\nnavigating"))
        self.assertEqual(text, "RuntimeError: Timeout 45000ms exceeded.")

    def test_secret_values_are_redacted(self):
        text = dc.brief_error(ValueError('bad cookie session=abcdef123456; acw_tc: "xyz"'))
        self.assertNotIn("abcdef123456", text)
        self.assertNotIn("xyz", text)
        self.assertIn("session=***", text)

    def test_empty_message(self):
        self.assertEqual(dc.brief_error(KeyError()), "KeyError")

    def test_checkin_site_uses_brief_error(self):
        site = _anyrouter_site()
        with mock.patch.object(dc, "_checkin_anyrouter",
                               side_effect=RuntimeError("session=topsecret\nmore")):
            result = dc.checkin_site(object(), site)
        self.assertEqual(result.status, dc.FAILED)
        self.assertNotIn("topsecret", result.detail)
        self.assertNotIn("more", result.detail)


class StaleReferenceTests(unittest.TestCase):
    """本项目已独立，不该再提 wxbot 的 plugins/ckeckin。"""

    def test_no_plugins_ckeckin_mentions(self):
        root = Path(dc.ROOT)
        for path in (root / "tools" / "daily_checkin.py",
                     root / "docs" / "每日签到脚本.md", root / "README.md"):
            with self.subTest(path=path.name):
                self.assertNotIn("ckeckin", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
