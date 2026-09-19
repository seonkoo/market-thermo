#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
anchor_build.py  —  收盘后构建「尺子」(锚)，供 market-thermo「今日推荐(实时)」使用

【为什么需要它】
页面的「前 30 分钟两线不交叉」判定，需要一个取自窗口之外的固定尺子（锚）。
原实现是页面上自己建锚：只有当它读到「不是今天」的分时数据时才建，
所以必须由用户在每个交易日开盘前先打开一次页面。

但东财的分钟资金流接口（fflow/kline klt=1）**只保留最近 1 个交易日**，
盘中再也拿不到昨天的分钟资金 —— 前端无法自行补建。

故改由服务端在【收盘后】抓当天完整数据，写成 JSON 放仓库；页面直接同源读。
从此页面任何时候打开都有正确的尺子，不需要用户提前打开。

【口径必须与页面 buildAnchor() 完全一致】
  ① 价格：取当日全天【分钟收盘价】的 min/max，并把昨收并入，上下各留 10% 余量
  ② 资金：取当日全天【分钟主力净流入(亿元)】的 min/max，强制包含 0，上下各留 10% 余量
  ③ 价、资按【时间字符串】配对，只统计能配上的分钟点；配对数 < 30 视为无效

【覆盖范围】
  收盘时资金净流入前 TOP_BK 名行业的成员（按主力净流入降序取前 PER_BK 只，去重）
  + ETF 池。次日页面的候选池（资金流入前 3 行业 × 前 12 只）大概率落在其中。

用法：
  python3 anchor_build.py                 # 正常跑（带 15:05 收盘门禁）
  python3 anchor_build.py --force         # 忽略时间门禁（测试用）
  python3 anchor_build.py --probe 120     # 只抓 120 只，打印耗时/成功率，不写文件
"""

import argparse
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))

# ---------- 参数（与页面保持一致） ----------
TOP_BK = 60          # 最多展开多少个行业（按资金净流入排名，够了就提前停）
PER_BK = 120         # 每行业最多取多少只成员
MAX_WORKERS = 12     # 并发（避免触发限流）
MIN_PAIRS = 30       # 有效配对分钟数下限（页面同为 30）
CLOSE_GATE_HM = (15, 5)   # CST 15:05 之后才算「收盘后」

EM_HOSTS = [
    "push2delay.eastmoney.com",
    "push2his.eastmoney.com",
    "push2.eastmoney.com",
]

# 页面 _live.js 里的 ETF_POOL（保持同一份清单）
ETF_POOL = [
    ("1.588170", "科创半导体ETF"),
    ("1.515880", "通信ETF"),
    ("0.159995", "芯片ETF华夏"),
    ("1.516350", "半导体设备ETF"),
    ("1.512480", "半导体ETF"),
    ("1.512760", "芯片ETF国泰"),
    ("1.516010", "游戏ETF"),
    ("0.159997", "电子ETF"),
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

_lock = threading.Lock()
_stats = {"req": 0, "ok": 0, "fail": 0, "host_used": {}}


def _bump(key, n=1):
    with _lock:
        _stats[key] = _stats.get(key, 0) + n


def _via_urllib(url, timeout, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _via_curl(url, timeout, headers):
    """沙箱对部分东财域名的 python urllib 做连接层拦截，但 curl 能通。
    真实部署(Actions) urllib 优先，curl 仅兜底。curl 写临时文件再读，避开 exit 23。"""
    import subprocess
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".curl")
    os.close(fd)
    try:
        cmd = ["curl", "-s", "--max-time", str(timeout), "-o", tmp]
        for k, v in headers.items():
            cmd += ["-H", "%s: %s" % (k, v)]
        cmd.append(url)
        subprocess.run(cmd, capture_output=True, timeout=timeout + 8)
        with open(tmp, "rb") as f:
            return f.read().decode("utf-8", "replace")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


_URLLIB_OK = True   # 一旦发现 urllib 被拦，后续直接用 curl


def http_get(path, timeout=8, retries=2):
    """依次尝试多个东财域名；返回解析后的 JSON dict 或 None。"""
    global _URLLIB_OK
    last = None
    headers = {
        "User-Agent": UA,
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "application/json, text/plain, */*",
    }
    for attempt in range(retries):
        for host in EM_HOSTS:
            url = "https://" + host + path
            _bump("req")
            raw = None
            if _URLLIB_OK:
                try:
                    raw = _via_urllib(url, timeout, headers)
                except Exception as e:  # noqa: BLE001
                    last = e
                    with _lock:
                        _URLLIB_OK = False
            if raw is None:
                try:
                    raw = _via_curl(url, timeout, headers)
                except Exception as e:  # noqa: BLE001
                    last = e
                    continue
            try:
                j = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            if j:
                _bump("ok")
                with _lock:
                    _stats["host_used"][host] = _stats["host_used"].get(host, 0) + 1
                return j
        time.sleep(0.4 * (attempt + 1))
    _bump("fail")
    if last:
        print("   [warn] 请求失败 %s (%s)" % (path[:70], last))
    return None


def num(v, d=0.0):
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else d
    except Exception:  # noqa: BLE001
        return d


# ---------- 行业 / 成员 ----------
def top_industries(n):
    """资金净流入前 n 名的行业板块。"""
    path = ("/api/qt/clist/get?pn=1&pz=%d&po=1&np=1&fltt=2&invt=2&fid=f62"
            "&fs=m:90+t:2&fields=f12,f13,f14,f62" % max(n, 10))
    j = http_get(path)
    out = []
    try:
        for x in j["data"]["diff"]:
            out.append({"code": str(x.get("f12")), "name": str(x.get("f14")),
                        "flow": num(x.get("f62"))})
    except Exception:  # noqa: BLE001
        return []
    return out[:n]


def tradable(code, name):
    """与页面 tradable() 一致：只认沪深主板，剔除 ST/退。"""
    if not code or not name:
        return False
    if not (code.startswith(("600", "601", "603", "605", "000", "001", "002", "003"))):
        return False
    if "ST" in name or "退" in name:
        return False
    return True


def members(bk_code, limit):
    """取板块成员。先按资金降序多拿一些，过滤掉不可交易标的，再截断到 limit。"""
    want = min(max(limit * 4, 120), 400)
    path = ("/api/qt/clist/get?fid=f62&po=1&pz=%d&pn=1&np=1&fltt=2&invt=2"
            "&fs=b:%s&fields=f12,f13,f14,f3,f62" % (want, bk_code))
    j = http_get(path)
    out = []
    try:
        for x in j["data"]["diff"]:
            code = str(x.get("f12"))
            name = str(x.get("f14"))
            if not tradable(code, name):
                continue
            out.append({"secid": "%s.%s" % (x.get("f13"), code), "name": name})
            if len(out) >= limit:
                break
    except Exception:  # noqa: BLE001
        pass
    return out


# ---------- 单只标的的尺子 ----------
def price_series(secid):
    """当日分时：返回 (preClose, {HH:MM: close}, day)。"""
    path = ("/api/qt/stock/trends2/get?secid=%s"
            "&fields1=f1,f2,f3,f4,f5,f6,f7,f8"
            "&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
            "&iscr=0&ndays=1" % secid)
    j = http_get(path)
    try:
        d = j["data"]
        pre = num(d.get("preClose"), 0.0)
        pts = {}
        day = ""
        for s in d["trends"]:
            p = str(s).split(",")
            if len(p) < 3:
                continue
            dt = p[0].split(" ")
            if len(dt) < 2:
                continue
            day = dt[0]
            pts[dt[1]] = num(p[2])          # f53 = 分钟收盘价
        return pre, pts, day
    except Exception:  # noqa: BLE001
        return 0.0, {}, ""


def fund_series(secid):
    """当日分钟主力净流入（亿元）。"""
    path = ("/api/qt/stock/fflow/kline/get?lmt=0&klt=1"
            "&fields1=f1,f2,f3,f7&fields2=f51,f52&secid=%s" % secid)
    j = http_get(path)
    out = {}
    try:
        for s in j["data"]["klines"]:
            p = str(s).split(",")
            if len(p) < 2:
                continue
            dt = p[0].split(" ")
            hm = dt[1][:5] if len(dt) > 1 else p[0][:5]
            out[hm] = num(p[1]) / 1e8       # 与页面一致：已是亿元
    except Exception:  # noqa: BLE001
        pass
    return out


def build_one(item, want_day):
    """抓一只标的，按页面 buildAnchor() 的口径算出尺子；不合格返回 None。"""
    secid = item["secid"]
    pre, pts, day = price_series(secid)
    if not pts:
        return None
    if want_day and day and day != want_day:
        return None                      # 拿到的不是目标交易日的完整数据
    flow = fund_series(secid)
    if not flow:
        return None

    lo = float("inf")
    hi = float("-inf")
    flo = float("inf")
    fhi = float("-inf")
    n = 0
    for hm, p in pts.items():
        f = flow.get(hm)
        if f is None:
            continue
        if p < lo:
            lo = p
        if p > hi:
            hi = p
        if f < flo:
            flo = f
        if f > fhi:
            fhi = f
        n += 1
    if n < MIN_PAIRS:
        return None

    # —— 以下与页面 buildAnchor() 逐行对齐 ——
    if pre > 0:
        if pre < lo:
            lo = pre
        if pre > hi:
            hi = pre
    if not (hi > lo):
        hi = lo + abs(lo * 0.01) + 1e-6
    pad = (hi - lo) * 0.1
    lo -= pad
    hi += pad

    if flo > 0:
        flo = 0.0
    if fhi < 0:
        fhi = 0.0
    if not (fhi > flo):
        fhi = flo + 1.0
    pad = (fhi - flo) * 0.1
    flo -= pad
    fhi += pad

    return {
        "nm": item.get("name", ""),
        "lo": round(lo, 4), "hi": round(hi, 4),
        # 资金单位＝亿元（与页面实时曲线一致）；保留 8 位小数 ＝ 精确到 1 元
        "flo": round(flo, 8), "fhi": round(fhi, 8),
        "n": n, "_d": day,
    }


# ---------- 主流程 ----------
def collect_universe(top_bk, per_bk, target=0, limit=0):
    """按资金净流入排名依次展开行业成员，直到去重后达到 target 只（或行业数用尽）。"""
    seen = set()
    uni = []
    for e in ETF_POOL:
        if e[0] in seen:
            continue
        seen.add(e[0])
        uni.append({"secid": e[0], "name": e[1], "bk": "ETF"})
    bks = top_industries(top_bk)
    print("[universe] 候选行业 %d 个：%s"
          % (len(bks), "、".join(b["name"] for b in bks[:10]) + ("…" if len(bks) > 10 else "")))
    for i, b in enumerate(bks, 1):
        ms = members(b["code"], per_bk)
        added = 0
        for m in ms:
            if m["secid"] in seen:
                continue
            seen.add(m["secid"])
            m["bk"] = b["name"]
            uni.append(m)
            added += 1
        print("   #%2d %-16s 新增 %3d 只  累计 %4d" % (i, b["name"][:16], added, len(uni)))
        if target and len(uni) >= target:
            print("[universe] 已达目标 %d 只，停止扩张" % target)
            break
        if limit and len(uni) >= limit:
            break
    if limit:
        uni = uni[:limit]
    return uni


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="snap/anchor.json")
    ap.add_argument("--top-bk", type=int, default=TOP_BK, help="最多展开多少个行业")
    ap.add_argument("--per-bk", type=int, default=PER_BK)
    ap.add_argument("--target", type=int, default=1200, help="去重后累计到多少只就停")
    ap.add_argument("--workers", type=int, default=MAX_WORKERS)
    ap.add_argument("--probe", type=int, default=0, help="只抓 N 只做探测，不写文件")
    ap.add_argument("--force", action="store_true", help="忽略 15:05 收盘门禁")
    ap.add_argument("--rebuild", action="store_true", help="即使今天已构建过也重建")
    ap.add_argument("--max-seconds", type=int, default=1500)
    args = ap.parse_args()

    now = datetime.now(CST)
    hm = (now.hour, now.minute)
    print("[time] 现在 CST %s（周%d）" % (now.strftime("%Y-%m-%d %H:%M:%S"), now.isoweekday()))

    if not args.force:
        if now.isoweekday() > 5:
            print("[skip] 周末不构建"); return 0
        if hm < CLOSE_GATE_HM:
            print("[skip] 未到 15:05，当日数据尚不完整 —— 保持上一版尺子不动"); return 0

    today = now.strftime("%Y-%m-%d")

    # 幂等：同一天只构建一次（当天的 run 有多次，避免重复烧时间）
    if not args.force and not args.rebuild and os.path.exists(args.out):
        try:
            with open(args.out, "r", encoding="utf-8") as f:
                old = json.load(f)
            if old.get("d") == today:
                print("[skip] 今天的尺子已存在（d=%s，%d 只），无需重建" % (today, old.get("n", 0)))
                return 0
        except Exception:  # noqa: BLE001
            pass

    want_day = today if not args.force else ""   # probe/force 时放宽日期校验

    t0 = time.time()
    uni = collect_universe(args.top_bk, args.per_bk, target=args.target, limit=args.probe)
    print("[universe] 候选 %d 只" % len(uni))
    if not uni:
        print("[fatal] 候选池为空"); return 1

    items = {}
    bad = 0
    day_seen = {}
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(build_one, u, want_day): u for u in uni}
        done = 0
        for fut in as_completed(futs):
            u = futs[fut]
            done += 1
            try:
                r = fut.result()
            except Exception:  # noqa: BLE001
                r = None
            if r:
                items[u["secid"]] = r
            else:
                bad += 1
            if done % 25 == 0 or done == len(futs):
                print("   ... %d/%d  已成功 %d  失败 %d  用时 %.1fs"
                      % (done, len(futs), len(items), bad, time.time() - t0))
            if time.time() - t0 > args.max_seconds:
                print("   [warn] 超过 --max-seconds，提前收工")
                break

    dt = time.time() - t0
    print("[stats] 请求 %d 次 / 成功 %d / 失败 %d | 用时 %.1fs | 尺子 %d 只 / 失败 %d 只"
          % (_stats["req"], _stats["ok"], _stats["fail"], dt, len(items), bad))
    print("[hosts] %s" % _stats["host_used"])

    if args.probe:
        print("[probe] 模式：不写文件")
        # 抽样打印 3 只便于人工核对
        for i, k in enumerate(list(items)[:3]):
            print("   样本 %s %s" % (k, items[k]))
        return 0

    if len(items) < 50:
        print("[fatal] 成功数过少(%d)，判定为异常，保留上一版尺子" % len(items))
        return 1

    # 锚的基准日 = 数据实际所属的交易日（主流值），而不是运行时的墙钟日期。
    # 例：周六手动跑时拿到的是周五的数据，d 必须是周五。
    day_vote = {}
    for v in items.values():
        dv = v.pop("_d", "") or ""
        if dv:
            day_vote[dv] = day_vote.get(dv, 0) + 1
    data_day = max(day_vote, key=day_vote.get) if day_vote else today
    agree = day_vote.get(data_day, 0)
    print("[day] 数据所属交易日 = %s（%d/%d 一致）" % (data_day, agree, len(items)))
    if not args.force and data_day != today:
        print("[skip] 数据日(%s)不是今天(%s) —— 可能休市，保留上一版尺子" % (data_day, today))
        return 0

    out = {"d": data_day, "built": now.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
           "n": len(items), "src": "close", "items": items}
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("[write] %s  d=%s  n=%d  %d bytes"
          % (args.out, out["d"], out["n"], os.path.getsize(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
