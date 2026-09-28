# Router Checkin

AnyRouter 和 AgentRouter 的独立签到项目。

这个目录是完整的可运行项目，**不属于 wxbot，也不属于 `E:\newcheckin`**。签到脚本、
账号配置、浏览器 profile、日志、测试和文档都放在本项目内。运行时不会导入
wxbot 的代码，也不会复用 wxbot 的配置。

## 目录

```text
E:\router-checkin\
├─ tools\
│  ├─ daily_checkin.py             # 主程序
│  ├─ daily_checkin.cmd            # Windows 任务计划包装
│  ├─ checkin_sites.example.json   # 配置模板
│  └─ checkin_sites.json           # 本机真实配置，不提交
├─ data\
│  └─ checkin_profiles\             # AgentRouter 浏览器 profile，每个账号一个（AnyRouter 不用）
├─ logs\
│  └─ daily_checkin.log
├─ tests\
│  └─ test_daily_checkin.py
└─ docs\
   ├─ 每日签到脚本.md
   └─ 开发过程记录.md
```

## 安装

要求：

- Windows
- Python 3.10+
- 已安装 Microsoft Edge 或 Google Chrome
- 两个站点可用的代理出口

在项目目录执行：

```powershell
cd E:\router-checkin
C:\Python314\python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

脚本使用系统安装的 Edge/Chrome channel，不使用 Playwright 自带的 Chromium。
如果本机 Python 路径不同，把命令中的 `C:\Python314\python.exe` 换成实际路径。

## 配置

首次安装时复制模板：

```powershell
Copy-Item tools\checkin_sites.example.json tools\checkin_sites.json
```

模板里的占位内容（含「这里填」、`AgentRouter 用户 ID` 等）必须改成真实值；启用的
账号还留着占位内容时，脚本启动即报错退出（退出码 2）。暂时不用的账号设
`"enabled": false`。

### AnyRouter

每个账号填入浏览器登录后的 `session` Cookie：

```json
{
  "name": "anyrouter-1",
  "kind": "refresh",
  "base_url": "https://anyrouter.top",
  "cookies": {
    "session": "真实 session Cookie 值"
  },
  "session_cookie": "session",
  "api_user": "12345",
  "proxy": "http://127.0.0.1:7890",
  "enabled": true
}
```

`session_cookie` 填 Cookie 名称，通常是 `"session"`；真正的值放在
`cookies.session`。AnyRouter 的 ESA/WAF Cookie 由脚本运行时通过浏览器动态获取。

`api_user`（站点用户 ID，浏览器控制台 `localStorage.user.id`）**推荐填写**：
不填时站点可能对 `/api/user/self` 回 401「未提供 New-Api-User」。

### AgentRouter

AgentRouter 使用邮箱/用户名和密码重新登录。它的账号每天需要退出当前会话后重新
登录，访问 `/api/user/self` 才会触发签到：

```json
{
  "name": "agentrouter-1",
  "kind": "relogin",
  "base_url": "https://agentrouter.org",
  "profile": "agentrouter_1",
  "checkin_path": "",
  "api_user": "AgentRouter 用户 ID",
  "username": "邮箱或用户名",
  "password": "密码",
  "proxy": "http://127.0.0.1:7890",
  "enabled": true
}
```

**`api_user` 必填**：它是退出重登前预检（查今天的系统签到日志）要用的用户 ID，
登录后在浏览器控制台 `localStorage.user.id` 可以看到。缺了或填错，脚本为了保护
现有登录态不会退出重登，只报「签到状态未知（未执行重登）」。

没配 `username` / `password` 的 AgentRouter 账号直接报「需要人工重新登录」，
不打开浏览器、不动 profile。

当前 profile 已从原签到实现复制到本项目的 `data\checkin_profiles`。正常情况下
不需要重新抓取；只有 profile 被删除、损坏或站点再次要求人工滑块时，才需要：

```powershell
python tools\daily_checkin.py --login agentrouter-1
```

手工登录完成后正常关闭浏览器，等待 Cookie 写入 profile。

## 运行

两个站的签到日界不同：

- AnyRouter 按 UTC 日期重置，上海时间每天 08:00 才进入新的一天，08:00 之后访问一次即完成签到；08:00 前跑会白跑。
- AgentRouter 按本地 0 点重置，0 点之后退出并重新登录一次才计签到；预检也按本地 0 点判断「今天」。

所以统一在上海时间 08:10 之后运行，一次覆盖两个站：

```powershell
python tools\daily_checkin.py --verbose
python tools\daily_checkin.py --only anyrouter-1 --verbose
python tools\daily_checkin.py --only agentrouter-1 --verbose
```

运行结果写入 `logs\daily_checkin.log`，每个账号一行：

```text
2026-09-28 08:10:25 agentrouter-1: 签到成功 | 余额 1066.90$
2026-09-28 08:10:25 anyrouter-1: 今天已签过 | 余额 1406.83$ | 今日已签到（额度未变化）
```

余额一律是 `数字$` 的形式（`E:\claude-router\pick_key.ps1` 按这个格式读余额）。
配置错误、整轮运行中断这类不属于某个账号的结果记为 `daily_checkin: 失败 | ...`。

### 通知

可选地把结果 POST 到通用 webhook：

```powershell
python tools\daily_checkin.py --notify-url https://example.invalid/webhook
# 或者不在命令行里写 URL：
setx CHECKIN_NOTIFY_URL "https://example.invalid/webhook"
```

`--notify-url` 优先；没给时读环境变量 `CHECKIN_NOTIFY_URL`；都没有就不推送。
只接受 `http://` / `https://`。webhook 返回 3xx 视为推送失败，不跟随重定向
（urllib 跟随时会把 POST 改成不带 body 的 GET）。

请求体是 UTF-8 JSON，`Content-Type: application/json; charset=utf-8`：

```json
{
  "content": "每日签到 09-28\nagentrouter-1: 签到成功 | 余额 1066.90$\nanyrouter-1: 失败 | WAF/限流未通过，请稍后重试（不是重新登录）",
  "text": "（与 content 相同）"
}
```

`content` 与 `text` 内容相同，照顾不同机器人的字段名；第一行是「每日签到 MM-DD」，
之后每个账号一行，格式与日志行相同。配置错误和整轮中断也会推送。推送失败只打印
提示，不影响签到结果和退出码。这个通知功能只使用 Python 标准库，不依赖 wxbot。

## Windows 任务计划

```powershell
schtasks /create /tn "router-checkin-daily" /tr "E:\router-checkin\tools\daily_checkin.cmd" /sc daily /st 08:10
```

批处理文件会自动切换到本项目目录，优先使用 `C:\Python314\python.exe`，找不到时
回退到 PATH 中的 `python`。

## 结果与退出码

| 日志里的状态 | 含义 | 退出码 |
|---|---|---|
| 签到成功 / 今天已签过 | 正常 | 不影响 |
| 签到已提交（未能核对） | AgentRouter 已登录并触发签到，只是核对余额的请求被滑块挡了 | 不影响 |
| 签到状态未知（未执行重登） | 预检无法确认今天是否已签，为保护现有登录态没有退出重登 | `1` |
| 需要人工重新登录 | 会话失效、缺凭据、登录页明确提示凭据错误 | `1` |
| 失败 | WAF/限流、网络、页面异常、整轮运行中断 | `1` |

- `0`：所有启用账号都是前两行的状态（或没有启用的账号）
- `1`：至少一个账号需要处理
- `2`：配置无法加载（缺文件、JSON 错误、未知字段、占位内容没改），或使用了已废弃的
  `--notify` 参数

## 从 wxbot 迁移

旧的签到实现在 wxbot 里，按微信 ID 推送（`--notify WXID`）。迁到本项目时：

1. **停用旧任务**，避免两边同时签到、互相退出对方的会话：

   ```powershell
   schtasks /change /tn "wxbot-daily-checkin" /disable
   ```

2. **创建新任务** `router-checkin-daily`（见上一节的 `schtasks /create`）。

3. **通知**：本项目没有 `--notify WXID`。旧参数被保留为隐藏参数，一旦出现就以
   退出码 `2` 失败，并在日志写一行「--notify 已废弃」，方便发现漏改的任务。改用
   `--notify-url URL` 或环境变量 `CHECKIN_NOTIFY_URL`；想继续推到微信，就让 wxbot
   暴露一个接收上面 JSON 的 webhook。需要在任务里带参数时：

   ```powershell
   schtasks /create /tn "router-checkin-daily" /tr "\"E:\router-checkin\tools\daily_checkin.cmd\" --notify-url https://example.invalid/webhook" /sc daily /st 08:10 /f
   ```

4. **claude-router**：`E:\claude-router\config.json` 的 `checkin_log` 仍指向 wxbot
   的日志时，余额过滤会一直读旧数据。改成本项目的日志：

   ```json
   "checkin_log": "E:\\router-checkin\\logs\\daily_checkin.log"
   ```

## 站点实现要点

- 两个站点的 API 请求都必须使用浏览器页内 `fetch`，不能改成 requests、HTTPX 或
  curl；ESA 会依据 Chrome 的 TLS/HTTP2 特征拦截普通 HTTP 客户端。
- AnyRouter 使用配置中的 `session` Cookie，临时浏览器负责 WAF 预热。
- AgentRouter 使用持久化 profile 和密码重新登录。
- AgentRouter 在退出重登前会先查当天系统日志；无法确认状态时保留现有会话，不贸然
  清除登录 Cookie。profile 里没有会话、或服务端明确回答会话已失效时才直接重登。
  退出后任何一步失败，都会把原来的 `session` 放回去。
- AnyRouter 遇到 403、`http_auto_ratelimit` 或验证页时不要连续重试。更换节点后，
  等限流窗口结束再单独重试。

## 测试

测试不访问站点，也不会启动浏览器：

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m py_compile tools\daily_checkin.py
```

## 文档

- [每日签到脚本](docs/每日签到脚本.md)：配置、运行、排错和任务计划说明。
- [开发过程记录](docs/开发过程记录.md)：历史方案、WAF 结论和验证记录。
