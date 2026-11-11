# -*- coding: utf-8 -*-
"""check_http.py — HTTP 行为断言器（http 动作的执行体）。零写入。

用法: check_http.py <url> <expect_status> [contains]
GET url → 状态码须等于 expect_status；给了 contains 则响应体须含该子串。
不符 exit 1；网络/参数异常 exit 2（考卷/环境故障，非产物不合格）。
"""
import sys
import urllib.request


def main() -> int:
    if len(sys.argv) < 3:
        print("用法: check_http.py <url> <expect_status> [contains]")
        return 2
    url, expect = sys.argv[1], sys.argv[2]
    contains = sys.argv[3] if len(sys.argv) > 3 else None
    try:
        expect_code = int(expect)
    except ValueError:
        print(f"FAIL: expect_status 非整数: {expect}")
        return 2
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            status = resp.status
            body = resp.read(65536).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status, body = e.code, ""
    except Exception as e:  # 网络故障=环境错误
        print(f"ERROR: 请求失败 {type(e).__name__}: {e}")
        return 2
    if status != expect_code:
        print(f"FAIL: 期望 {expect_code} 实得 {status}")
        return 1
    if contains is not None and contains not in body:
        print(f"FAIL: 响应不含 {contains!r}")
        return 1
    print(f"http ok: {url} → {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
