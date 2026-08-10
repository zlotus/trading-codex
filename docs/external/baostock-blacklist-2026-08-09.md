# BaoStock blacklist 规则快照

- 来源页面：`https://www.baostock.com/blacklist`
- 抓取时间：`2026-08-09T19:26:55+08:00`
- HTTP 状态：`200 OK`
- HTTP `Date`：`Sun, 09 Aug 2026 11:26:44 GMT`
- HTTP `Last-Modified`：`Tue, 23 Jun 2026 01:05:52 GMT`
- HTTP `ETag`：`"6a39dbf0-1f96"`
- 页面字节数：`8,086`
- 页面 SHA-256：`fe0bad2d5c6e6cb6ff415e291510c171ba758b7deb0001ed60a8a108c7933a18`
- 规则资源：`https://www.baostock.com/assets/index-BoSgeTsO.js`
- 规则资源字节数：`2,308,764`
- 规则资源 SHA-256：`0ce1b6d6e3f386fc7080acf6790d3ee2dfb35ca7dd79beb286da5a39e229d3a8`

`/blacklist` 返回 SPA HTML，下面的可见规则原文位于页面引用的同站点规则资源中。M7 的
global state 和 frozen manifest 固定保存规则资源 SHA-256；官网资源变化时，必须先人工复核
规则并更新 contract，不能自动采用更宽松解释。

## 页面原文

> 每日API请求不能超过5万次，并且不能并发连接访问，超过后进入黑名单控制

> 黑名单控制后，请在QQ群联系管理员寻求解决，并告知互联网IP地址

页面示例对黑名单响应的说明原文：

> 当黑名单控制时，error_code输出"10001011"、error_msg输出"黑名单用户，请与管理员联系"

示例判断：

```python
if lg.error_code == "10001011":
    print("IP已经受黑名单控制, 请去官网QQ群里向管理员求助.")
```

## 项目解释

官方页面只确认 `50,000/IP/日`、禁止并发、黑名单错误码和人工联系管理员的恢复路径。页面
没有定义自然日时区、高层 API 与底层 socket 的计数关系、失败请求是否计数或具体 QPS。
项目因此采用 `Asia/Shanghai` 自然日加 rolling 24-hour 双预算、每次底层 `send_msg` attempt
计数、至少 3 秒持久 cooldown、`45,000` 项目硬上限和默认 `2,000` 次预算。这些是项目的
保守策略，不是 BaoStock 官方原文。
