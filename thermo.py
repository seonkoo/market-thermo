#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股市场温度计 · 数据端
--------------------------------------------------
唯一职责：把各维度(指数/宽度/情绪/流动性/两融/行业/资金流/广度/期指升贴水/ETF净流向)算完，产出 data.json。
页面 index.html 只读 JSON，不做任何计算。

用法：
    python thermo.py            # 全量计算
    python thermo.py --no-kline # 跳过K线（调试/赶时间用，宽度与情绪会降级）
"""

import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(BASE, "config.json")
OUT_PATH = os.path.join(BASE, "data.json")
TL_PATH = os.path.join(BASE, "timeline.json")   # 主力视角 regime 当日轨迹（只留当日，跨日重置）

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"

EM_SPOT = "https://push2delay.eastmoney.com/api/qt/clist/get"
EM_ULIST = "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
EM_DATA = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EM_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
SINA_KLINE = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
TX_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"  # 备用

# A股全市场（沪深主板 + 创业板 + 科创板，不含北交所）
FS_ALL = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"

# 股指期货(中金所)连续合约：新浪期货行情；现货指数对应东财 secid
SINA_FUT = "https://hq.sinajs.cn/list="
FUT_MAP = {"IF0": "沪深300", "IC0": "中证500", "IH0": "上证50", "IM0": "中证1000"}
FUT_SPOT = {"IF0": "1.000300", "IC0": "1.000905", "IH0": "1.000016", "IM0": "1.000852"}
# ETF 资金流向板块（东财基金-ETF）
FS_ETF = "m:1+t:9"
ETF_TOPN = 12

DEFAULT_CFG = {
    "net": {"timeout": 25, "retries": 3, "workers": 10, "retry_sleep": 1.2},
    "width": {"top_n": 800, "groups": 20, "ma_days": 20, "kline_days": 25},
    "emotion": {
        "weights": {"advance": 0.30, "ma_breadth": 0.25, "limit_net": 0.20,
                    "volume": 0.15, "margin": 0.10},
        "hot": 70, "cold": 30,
        "limit_up_pct_main": 9.8, "limit_up_pct_gem": 19.8, "limit_up_pct_st": 4.8,
        "volume_bands": [[0.60, 18], [0.85, 38], [1.15, 55], [1.50, 75], [99.0, 92]],
        "margin_bands": [[-3.0, 15], [-0.5, 35], [0.5, 55], [3.0, 75], [99.0, 92]],
    },
    "liquidity": {
        "top_pct": 0.05, "top_ratio_warn": 0.40, "hhi_warn": 0.0020, "gini_warn": 0.65,
        "risk_weights": {"top_ratio": 0.5, "hhi": 0.3, "gini": 0.2},
    },
    "margin": {"page_size": 500, "max_pages": 12},
    "sector": {"top_n": 12, "bottom_n": 8},
    "capital_read": {
        "thresholds": {
            "main_net_in_billion": 50, "main_net_out_billion": -50,
            "xlarge_in_billion": 30, "xlarge_out_billion": -30,
            "small_in_billion": 30, "small_out_billion": -30,
            "advance_high_pct": 60, "advance_low_pct": 40,
            "limit_up_many": 50, "limit_down_many": 20,
            "basis_premium_pct": 0.10, "basis_discount_pct": -0.30,
            "etf_net_yi": 20,
        },
        "retail": {
            "共振拉升": {"posture": "顺势持有，不折腾",
                "actions": ["持有核心、享受 beta", "可沿趋势小仓加核心资产", "不频繁换股"],
                "cautions": ["趋势中设移动止盈，防反转"]},
            "诱多派发": {"posture": "不追高，管住手",
                "actions": ["停止追涨、不开新仓", "持仓设好止盈、主动减高估值仓位", "把注意力放回核心资产(等回踩)"],
                "cautions": ["最易被假突破诱多接盘", "量价背离时舍得卖，不恋战"]},
            "出货撤退": {"posture": "降仓避险",
                "actions": ["降低总仓位、回收现金", "回避高位题材与微盘股", "核心底仓可留，减弹性仓位"],
                "cautions": ["指数可能滞后于个股走弱，别被指数骗"]},
            "洗盘震仓": {"posture": "持有不动，忽略噪音",
                "actions": ["持仓不动，不被震仓洗出", "不追涨杀跌", "关注洗盘结束后的方向选择"],
                "cautions": ["洗盘与下跌初期难区分，设好止损线"]},
            "吸筹布局": {"posture": "分批低吸，别恐慌",
                "actions": ["别人恐惧时按计划分批低吸核心", "越跌越买需有纪律、控节奏", "优先景气蓝筹与宽基ETF"],
                "cautions": ["吸筹期常磨底较久，别一把梭"]},
            "弱势阴跌": {"posture": "空仓等待，保住本金",
                "actions": ["空仓或极轻仓观望", "不抄「便宜」的底", "等宽度与情绪共振企稳信号"],
                "cautions": ["阴跌最杀抄底者，宁可错过"]},
            "震荡观望": {"posture": "少动多看，等信号",
                "actions": ["维持现有仓位，不追不割", "观察主力资金流向何时明朗", "复盘持仓结构、汰弱留强"],
                "cautions": ["信号混杂时强行操作胜率最低"]},
        },
    },
    "judgement": {
        "margin_fast_in": 3.0, "margin_fast_out": -3.0,
        "vol_shrink": 0.85, "vol_expand": 1.30,
        "emotion": {"fear": 30, "cool": 42, "warm": 58, "euphoria": 70},
        "liquidity": {"high": 70, "mid": 50, "low": 30},
        "breadth": {"weak": 40, "strong": 60},
    },
    "indices": [
        {"secid": "1.000001", "name": "上证指数", "group": "A股"},
        {"secid": "0.399001", "name": "深证成指", "group": "A股"},
        {"secid": "0.399006", "name": "创业板指", "group": "A股"},
        {"secid": "1.000688", "name": "科创50", "group": "A股"},
        {"secid": "0.899050", "name": "北证50", "group": "A股"},
        {"secid": "100.HSI", "name": "恒生指数", "group": "亚太"},
        {"secid": "100.HSCEI", "name": "国企指数", "group": "亚太"},
        {"secid": "100.N225", "name": "日经225", "group": "亚太"},
        {"secid": "100.KS11", "name": "韩国KOSPI", "group": "亚太"},
        {"secid": "100.DJIA", "name": "道琼斯", "group": "欧美"},
        {"secid": "100.NDX", "name": "纳斯达克100", "group": "欧美"},
        {"secid": "100.SPX", "name": "标普500", "group": "欧美"},
        {"secid": "100.UDI", "name": "美元指数", "group": "欧美"},
    ],
}

WARN = []


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def warn(msg):
    WARN.append(msg)
    log("  !! " + msg)


def load_cfg():
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
        log("配置已加载: %s" % CFG_PATH)
    except FileNotFoundError:
        log("未找到 config.json，使用内置默认配置")
    except Exception as e:
        warn("config.json 解析失败(%s)，回落默认配置" % e)
    return cfg


def http_get(url, cfg, enc="utf-8", headers=None, retries=None):
    net = cfg["net"]
    tries = retries or net["retries"]
    h = {"User-Agent": UA, "Accept": "*/*", "Connection": "close"}
    if headers:
        h.update(headers)

    def via_urllib():
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=net["timeout"]) as r:
            return r.read().decode(enc, "ignore")

    def via_curl():
        # 沙箱对部分东财域名的 python urllib 做了连接层拦截，
        # 但 curl 能通；真实部署(本机/Actions) urllib 优先，curl 仅兜底。
        # 注意：沙箱 curl 偶发 exit 23（stdout 管道写入小毛病），
        # capture_output 读 p.stdout 会拿到空串 -> 解析失败。
        # 改成写临时文件再读，彻底避开该坑。
        import subprocess, tempfile, os
        fd, tmppath = tempfile.mkstemp(suffix=".curl")
        os.close(fd)
        try:
            cmd = ["curl", "-s", "--max-time", str(net["timeout"]), "-A", UA,
                   "-o", tmppath]
            for k, v in h.items():
                cmd += ["-H", "%s: %s" % (k, v)]
            cmd.append(url)
            subprocess.run(cmd, capture_output=True, timeout=net["timeout"] + 8)
            with open(tmppath, "rb") as f:
                return f.read().decode(enc, "ignore")
        finally:
            try:
                os.remove(tmppath)
            except OSError:
                pass

    last = None
    for i in range(tries):
        try:
            return via_urllib()
        except Exception as e:
            last = e
            # urllib 连接层失败 -> 立刻换 curl 兜底（同一轮内不浪费重试）
            try:
                return via_curl()
            except Exception as e2:
                last = e2
            time.sleep(net["retry_sleep"] * (i + 1))
    raise RuntimeError("请求失败 %s (%s)" % (url[:110], last))


def get_json(url, cfg, **kw):
    return json.loads(http_get(url, cfg, **kw))


def num(v):
    """东财大量字段用 '-' 表示空值"""
    try:
        if v in (None, "", "-", "null"):
            return None
        return float(v)
    except Exception:
        return None


# ---------------------------------------------------------------- 1. 指数全景
def fetch_indices(cfg):
    log("① 指数全景 ...")
    items = cfg["indices"]
    secids = ",".join(x["secid"] for x in items)
    url = "%s?fltt=2&secids=%s&fields=f1,f2,f3,f4,f12,f13,f14,f124" % (EM_ULIST, secids)
    j = get_json(url, cfg)
    diff = ((j.get("data") or {}).get("diff")) or []
    meta = {x["secid"]: x for x in items}

    out = []
    for d in diff:
        code = d.get("f12")
        # 东财 secid 格式恒为 "市场.代码"（A股 1.000001 / 港股 100.HSI / 美股 100.DJIA）
        secid = "%s.%s" % (d.get("f13"), code) if d.get("f13") is not None else code
        m = meta.get(secid) or meta.get(code) or {}
        price = num(d.get("f2"))
        if price is None:
            continue
        out.append({
            "name": m.get("name") or d.get("f14") or code,
            "group": m.get("group", "其他"),
            "code": code,
            "price": round(price, 2),
            "pct": round(num(d.get("f3")) or 0.0, 2),
            "change": round(num(d.get("f4")) or 0.0, 2),
        })

    order = {"A股": 0, "亚太": 1, "欧美": 2, "其他": 3}
    out.sort(key=lambda x: (order.get(x["group"], 9),))
    log("   指数 请求 %d 个 -> 有效 %d 个" % (len(items), len(out)))
    if len(out) < len(items):
        warn("指数只拿到 %d/%d 个，缺失的已跳过" % (len(out), len(items)))
    return out


# ---------------------------------------------------------------- 2. 全市场快照
def fetch_spot(cfg):
    log("② 全市场快照 ...")
    net = cfg["net"]
    # f62主力 f66超大单 f72大单 f78中单 f84小单 净流入额（元）—— 资金流构成，一次抓取全模块复用
    fields = "f2,f3,f5,f6,f12,f13,f14,f20,f100,f62,f66,f72,f78,f84"
    first = get_json("%s?pn=1&pz=1&po=1&np=1&fltt=2&invt=2&fid=f6&fs=%s&fields=%s"
                     % (EM_SPOT, FS_ALL, fields), cfg)
    total = int(((first.get("data") or {}).get("total")) or 0)
    if total <= 0:
        raise RuntimeError("全市场快照返回 total=0，数据源异常")
    page_size = 100
    pages = (total + page_size - 1) // page_size
    log("   全市场 total=%d -> 分 %d 页并发拉取" % (total, pages))

    rows = []
    lock_err = []

    def one(pn):
        u = "%s?pn=%d&pz=%d&po=1&np=1&fltt=2&invt=2&fid=f6&fs=%s&fields=%s" % (
            EM_SPOT, pn, page_size, FS_ALL, fields)
        try:
            d = (get_json(u, cfg).get("data") or {}).get("diff") or []
            return pn, d
        except Exception as e:
            lock_err.append(str(e)[:80])
            return pn, []

    with ThreadPoolExecutor(max_workers=net["workers"]) as ex:
        futs = [ex.submit(one, p) for p in range(1, pages + 1)]
        got = {}
        for f in as_completed(futs):
            pn, d = f.result()
            got[pn] = d

    for p in range(1, pages + 1):
        for d in got.get(p, []):
            code = d.get("f12")
            price, pct = num(d.get("f2")), num(d.get("f3"))
            amount = num(d.get("f6"))
            if not code or price is None or pct is None:
                continue
            rows.append({
                "code": code,
                "name": d.get("f14") or code,
                "market": int(d.get("f13") or 0),
                "price": price,
                "pct": pct,
                "amount": amount or 0.0,
                "mktcap": num(d.get("f20")) or 0.0,
                "industry": d.get("f100") or "",
                "main_net": num(d.get("f62")) or 0.0,     # 主力净流入(元)
                "xlarge_net": num(d.get("f66")) or 0.0,   # 超大单(机构)
                "large_net": num(d.get("f72")) or 0.0,    # 大单(大户)
                "mid_net": num(d.get("f78")) or 0.0,      # 中单(中户)
                "small_net": num(d.get("f84")) or 0.0,    # 小单(散户)
            })

    log("   快照 原始 %d -> 有效 %d 只（失败页 %d）" % (total, len(rows), len(lock_err)))
    if lock_err:
        warn("快照有 %d 页拉取失败，统计口径会偏小" % len(lock_err))
    return rows


# ---------------------------------------------------------------- 3. K线 / 宽度
def em_secid(row):
    return "%d.%s" % (row.get("market", 0), row["code"])


def sina_symbol(secid):
    # "1.600519" -> "sh600519" ; "0.000001" -> "sz000001"
    m, code = secid.split(".")
    return ("sh" if m == "1" else "sz") + code


def _parse_em_kl(raw):
    out = []
    for s in raw:
        p = s.split(",")
        if len(p) < 7:
            continue
        try:
            out.append([p[0], float(p[1]), float(p[2]), float(p[3]),
                        float(p[4]), float(p[5]), float(p[6])])
        except Exception:
            continue
    return out if out else None


def _parse_sina(d):
    out = []
    for x in d:
        try:
            close = float(x["close"]); vol_shares = float(x["volume"])
            # 新浪 volume 单位为「股」(非手)，无成交额字段。
            # 统一成与东财一致: [..., volume_手, amount_元]，便于 breadth 复用 c*v*100 公式
            out.append([x["day"], float(x["open"]), close, float(x["high"]),
                        float(x["low"]), vol_shares / 100.0, close * vol_shares])
        except Exception:
            continue
    return out if out else None


def fetch_kline(secid, cfg, days=25):
    """归一化返回 [date, open, close, high, low, volume, amount]，close=索引2/volume=索引5。
       主源新浪(urllib友好, 沙箱/部署通用)；兜底东财 push2his、腾讯。"""
    # 1) 新浪
    try:
        u = "%s?symbol=%s&scale=240&ma=5&datalen=%d" % (SINA_KLINE, sina_symbol(secid), days)
        j = json.loads(http_get(u, cfg, headers={"Referer": "https://finance.sina.com.cn/"}))
        if isinstance(j, list) and j:
            p = _parse_sina(j)
            if p:
                return p
    except Exception:
        pass
    # 2) 东财 push2his
    try:
        em = "%s?secid=%s&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=1&end=20500101&lmt=%d" % (
            EM_KLINE, secid, days)
        j = json.loads(http_get(em, cfg, headers={"Referer": "https://quote.eastmoney.com/"}))
        kl = (j.get("data") or {}).get("klines") or []
        if kl:
            parsed = _parse_em_kl(kl)
            if parsed:
                return parsed
    except Exception:
        pass
    # 3) 腾讯
    try:
        tx = sina_symbol(secid)
        j = json.loads(http_get("%s?param=%s,day,,,%d,qfq" % (TX_KLINE, tx, days), cfg))
        node = (j.get("data") or {}).get(tx) or {}
        kl = node.get("qfqday") or node.get("day") or []
        return kl if kl else None
    except Exception:
        return None


def fetch_klines_batch(cfg, rows):
    net = cfg["net"]
    wcfg = cfg["width"]
    kdays = int(wcfg.get("kline_days", 25))
    out, fail = {}, 0
    total = len(rows)
    log("   K线 开始拉取 %d 只（新浪主源，并发 %d）..." % (total, net["workers"]))

    def work(r):
        return r["code"], fetch_kline(em_secid(r), cfg, kdays)

    with ThreadPoolExecutor(max_workers=net["workers"]) as ex:
        futs = [ex.submit(work, r) for r in rows]
        done = 0
        for f in as_completed(futs):
            code, kl = f.result()
            done += 1
            if kl:
                out[code] = kl
            else:
                fail += 1
            if done % 200 == 0:
                log("   K线进度 %d/%d（成功 %d / 失败 %d）" % (done, total, len(out), fail))

    log("   K线 请求 %d -> 成功 %d / 失败 %d" % (total, len(out), fail))
    if total and fail > total * 0.15:
        warn("K线失败率 %.1f%%，宽度指标可信度下降" % (fail * 100.0 / total))
    return out


def compute_breadth(cfg, spot, klines):
    log("③ 成交额宽度 ...")
    w = cfg["width"]
    top_n, groups = int(w["top_n"]), int(w["groups"])
    ma_days = int(w["ma_days"])

    ranked = sorted(spot, key=lambda x: x["amount"], reverse=True)[:top_n]
    log("   取成交额 Top %d（全市场 %d 只）" % (len(ranked), len(spot)))

    stats = {}
    for r in ranked:
        kl = klines.get(r["code"])
        if not kl or len(kl) < ma_days + 1:
            continue
        try:
            closes = [float(x[2]) for x in kl[-ma_days - 1:]]
            vols = [float(x[5]) for x in kl[-ma_days - 1:]]
        except Exception:
            continue
        ma = sum(closes[-ma_days:]) / ma_days
        last = closes[-1]
        if ma <= 0:
            continue
        avg_amt = sum(c * v * 100 for c, v in zip(closes[-ma_days:], vols[-ma_days:])) / ma_days
        stats[r["code"]] = {
            "name": r["name"], "amount": r["amount"], "avg_amt": avg_amt,
            "above": 1 if last > ma else 0, "pct": r["pct"], "industry": r["industry"],
        }

    log("   K线匹配成功 %d / %d -> 进入分组" % (len(stats), len(ranked)))
    if len(stats) < top_n * 0.5:
        warn("宽度样本仅 %d 只（<50%%），该卡仅供参考" % len(stats))

    if not stats:
        return None

    arr = sorted(stats.values(), key=lambda x: x["avg_amt"])
    per = max(1, len(arr) // groups)
    grid = []
    for g in range(groups):
        seg = arr[g * per:(g + 1) * per] if g < groups - 1 else arr[g * per:]
        if not seg:
            break
        above = sum(x["above"] for x in seg)
        grid.append({
            "g": g + 1,
            "n": len(seg),
            "pct_above": round(above * 100.0 / len(seg), 1),
            "avg_amt": round(sum(x["avg_amt"] for x in seg) / len(seg) / 1e8, 2),
        })

    total_above = sum(x["above"] for x in stats.values())
    width = round(total_above * 100.0 / len(stats), 1)

    head = grid[-3:] if len(grid) >= 3 else grid
    tail = grid[:3] if len(grid) >= 3 else grid
    head_v = sum(x["pct_above"] for x in head) / len(head)
    tail_v = sum(x["pct_above"] for x in tail) / len(tail)

    if head_v - tail_v > 15:
        trend, note = "头部领涨", "大票在冲，资金聚焦头部"
    elif tail_v - head_v > 15:
        trend, note = "尾部领涨", "微盘股在冲，注意投机情绪"
    elif width >= 60:
        trend, note = "全面扩张", "多数个股站稳均线，市场偏强"
    elif width <= 40:
        trend, note = "普遍收缩", "多数个股跌破均线，市场偏弱"
    else:
        trend, note = "结构分化", "头部与尾部强度接近，方向未明"

    log("   宽度 %.1f%% | 头部 %.0f%% vs 尾部 %.0f%% -> %s" % (width, head_v, tail_v, trend))
    return {
        "width": width, "trend": trend, "note": note,
        "grid": grid, "sample": len(stats),
        "head_pct": round(head_v, 1), "tail_pct": round(tail_v, 1),
        "scope": "成交额Top%d" % top_n,
    }


# ---------------------------------------------------------------- 4. 流动性
def compute_liquidity(cfg, spot):
    log("④ 流动性集中度 ...")
    L = cfg["liquidity"]
    amounts = sorted([x["amount"] for x in spot if x["amount"] > 0])
    n = len(amounts)
    if n < 50:
        warn("流动性样本不足(%d)，跳过" % n)
        return None

    total = sum(amounts)
    top_k = max(1, int(n * float(L["top_pct"])))
    top_ratio = sum(amounts[-top_k:]) / total
    hhi = sum((a / total) ** 2 for a in amounts)
    cum = sum((i + 1) * a for i, a in enumerate(amounts))
    gini = (2.0 * cum) / (n * total) - (n + 1.0) / n

    rw = L["risk_weights"]
    def norm(v, warn_v):
        return min(100.0, max(0.0, v / warn_v * 60.0))
    risk = (norm(top_ratio, L["top_ratio_warn"]) * rw["top_ratio"]
            + norm(hhi, L["hhi_warn"]) * rw["hhi"]
            + norm(gini, L["gini_warn"]) * rw["gini"])
    risk = round(risk, 1)

    if risk >= 70:
        level, note = "高风险", "资金高度集中，个股分化剧烈，建议降低仓位"
    elif risk >= 50:
        level, note = "中风险", "资金中度集中，赚钱效应收敛，控制追高"
    elif risk >= 30:
        level, note = "偏低", "资金分布均衡，普涨格局为主"
    else:
        level, note = "分散", "资金高度分散，缺乏主线，谨慎参与"

    log("   TopRatio=%.4f HHI=%.5f Gini=%.4f -> 风险 %.1f (%s)"
        % (top_ratio, hhi, gini, risk, level))
    return {
        "top_ratio": round(top_ratio * 100, 1), "hhi": round(hhi, 5),
        "gini": round(gini, 4), "risk": risk, "level": level, "note": note,
        "sample": n, "top_pct": round(float(L["top_pct"]) * 100, 1),
    }


# ---------------------------------------------------------------- 5. 两融
def _margin_sum(cfg, date_str):
    M = cfg["margin"]
    page_size = int(M["page_size"])
    filt = urllib.parse.quote("(DATE='%s')" % date_str)
    tot = {"rz": 0.0, "rq": 0.0, "total": 0.0, "buy": 0.0,
           "net5": 0.0, "net10": 0.0, "n": 0}
    for pn in range(1, int(M["max_pages"]) + 1):
        u = ("%s?reportName=RPTA_WEB_RZRQ_GGMX&columns=ALL&pageSize=%d&pageNumber=%d"
             "&sortColumns=DATE&sortTypes=-1&source=WEB&client=WEB&filter=%s"
             % (EM_DATA, page_size, pn, filt))
        try:
            j = get_json(u, cfg)
        except Exception as e:
            warn("两融第%d页失败: %s" % (pn, str(e)[:60]))
            break
        res = j.get("result") or {}
        rows = res.get("data") or []
        if not rows:
            break
        for r in rows:
            tot["rz"] += float(r.get("RZYE") or 0)
            tot["rq"] += float(r.get("RQYE") or 0)
            tot["total"] += float(r.get("RZRQYE") or 0)
            tot["buy"] += float(r.get("RZMRE") or 0)
            tot["net5"] += float(r.get("RZJME5D") or 0)
            tot["net10"] += float(r.get("RZJME10D") or 0)
            tot["n"] += 1
        if pn * page_size >= int(res.get("count") or 0):
            break
    return tot


def fetch_margin(cfg):
    log("⑤ 融资融券 ...")
    probe = get_json("%s?reportName=RPTA_WEB_RZRQ_GGMX&columns=DATE&pageSize=1&pageNumber=1"
                     "&sortColumns=DATE&sortTypes=-1&source=WEB&client=WEB" % EM_DATA, cfg)
    rows = ((probe.get("result") or {}).get("data")) or []
    if not rows:
        warn("两融无数据，跳过")
        return None
    date_str = rows[0]["DATE"][:10]
    log("   最新两融日期 %s" % date_str)

    t = _margin_sum(cfg, date_str)
    if t["n"] == 0:
        warn("两融该日明细为空，跳过")
        return None
    log("   汇总 %d 条 -> 融资余额 %.1f亿" % (t["n"], t["rz"] / 1e8))

    def chg(net, cur):
        if cur <= 0:
            return None
        return round(net / (cur - net) * 100.0, 2) if (cur - net) > 0 else None

    d5, d10 = chg(t["net5"], t["rz"]), chg(t["net10"], t["rz"])
    if d10 is not None and d10 >= 3:
        note = "杠杆资金快速进场，情绪偏热"
    elif d10 is not None and d10 <= -3:
        note = "杠杆资金持续撤离，注意风险释放"
    else:
        note = "杠杆资金平稳，无明显方向"

    return {
        "date": date_str,
        "rz": round(t["rz"] / 1e8, 1),
        "rq": round(t["rq"] / 1e8, 1),
        "total": round(t["total"] / 1e8, 1),
        "buy": round(t["buy"] / 1e8, 1),
        "chg5": d5, "chg10": d10,
        "note": note, "n": t["n"],
    }


# ---------------------------------------------------------------- 6. 行业资金流
def fetch_sector(cfg):
    log("⑥ 行业资金流 ...")
    S = cfg["sector"]
    url = ("%s?fid=f62&po=1&pz=200&pn=1&np=1&fltt=2&invt=2&fs=%s&fields=f12,f14,f62,f184,f3"
           % (EM_SPOT, urllib.parse.quote("m:90+t:2")))
    j = get_json(url, cfg)
    rows = ((j.get("data") or {}).get("diff")) or []
    data = []
    for d in rows:
        flow = num(d.get("f62"))
        if flow is None:
            continue
        data.append({"code": d.get("f12"), "name": d.get("f14") or "",
                     "flow": flow, "pct": num(d.get("f3")) or 0.0})
    data.sort(key=lambda x: x["flow"], reverse=True)
    log("   板块 原始 %d -> 有效 %d" % (len(rows), len(data)))

    top = data[:int(S["top_n"])]
    bottom = list(reversed(data[-int(S["bottom_n"]):])) if len(data) > int(S["bottom_n"]) else []

    inflow = sum(x["flow"] for x in top)
    outflow = sum(x["flow"] for x in data[-int(S["bottom_n"]):]) if len(data) > int(S["bottom_n"]) else 0
    if inflow > abs(outflow) * 3:
        note = "资金集中涌入少数板块，主线清晰"
    elif inflow > abs(outflow):
        note = "流入略占优，板块轮动偏积极"
    else:
        note = "流出占优，板块普遍承压"

    return {
        "top": [{"name": x["name"], "flow": round(x["flow"] / 1e8, 2), "pct": round(x["pct"], 2)} for x in top],
        "bottom": [{"name": x["name"], "flow": round(x["flow"] / 1e8, 2), "pct": round(x["pct"], 2)} for x in bottom],
        "note": note, "n": len(data),
    }


# ---------------------------------------------------------------- 7. 情绪
def band_lookup(bands, v):
    for lim, score in bands:
        if v <= lim:
            return score
    return bands[-1][1]


def compute_emotion(cfg, spot, breadth, margin, idx_amount_ratio):
    log("⑦ 情绪指数 ...")
    E = cfg["emotion"]
    W = E["weights"]
    parts, used = {}, {}

    up = sum(1 for x in spot if x["pct"] > 0)
    adv = up * 100.0 / len(spot) if spot else 50.0
    parts["advance"] = round(min(100.0, max(0.0, adv)), 1)
    used["advance"] = "上涨家数占比 %.1f%%" % adv

    if breadth:
        parts["ma_breadth"] = round(min(100.0, max(0.0, breadth["width"])), 1)
        used["ma_breadth"] = "站上20日均线 %.1f%%" % breadth["width"]
    else:
        warn("宽度缺失，情绪的均线分项按中性 50 计")
        parts["ma_breadth"] = 50.0
        used["ma_breadth"] = "宽度数据缺失，按中性计"

    lu = sum(1 for x in spot if x["pct"] >= float(E["limit_up_pct_main"]))
    ld = sum(1 for x in spot if x["pct"] <= -float(E["limit_up_pct_main"]))
    net = (lu - ld) * 100.0 / max(1, (lu + ld)) if (lu + ld) else 0.0
    parts["limit_net"] = round(min(100.0, max(0.0, 50 + net * 0.9)), 1)
    used["limit_net"] = "涨停 %d / 跌停 %d" % (lu, ld)

    r = idx_amount_ratio
    if r is None:
        parts["volume"] = 50.0
        used["volume"] = "成交量基准缺失，按中性计"
    else:
        parts["volume"] = float(band_lookup(E["volume_bands"], r))
        used["volume"] = "沪市量能比 %.2f（相对20日均值）" % r

    if margin and margin.get("chg10") is not None:
        parts["margin"] = float(band_lookup(E["margin_bands"], margin["chg10"]))
        used["margin"] = "两融10日变动 %+.2f%%" % margin["chg10"]
    else:
        parts["margin"] = 50.0
        used["margin"] = "两融缺失，按中性计"

    tw = sum(W[k] for k in parts)
    score = sum(parts[k] * W[k] for k in parts) / tw if tw else 50.0
    score = round(min(100.0, max(0.0, score)), 1)

    hot, cold = float(E["hot"]), float(E["cold"])
    if score >= hot:
        level, note = "狂热", "情绪进入过热区，历史上多对应阶段性高位"
    elif score >= hot - 12:
        level, note = "偏热", "情绪偏乐观，追高性价比下降"
    elif score <= cold:
        level, note = "恐惧", "情绪进入冰点，往往对应机会区"
    elif score <= cold + 12:
        level, note = "偏冷", "情绪偏谨慎，可关注错杀"
    else:
        level, note = "中性", "情绪中性，跟随主线即可"

    log("   情绪 %.1f (%s) | 分项 %s" % (score, level, parts))
    return {
        "score": score, "level": level, "note": note,
        "parts": parts, "why": used,
        "bands": {"hot": hot, "cold": cold},
        "up": up, "down": len(spot) - up,
        "limit_up": lu, "limit_down": ld,
    }


# ---------------------------------------------------------------- 8. 主力资金构成（订单流）
def compute_flow(cfg, spot):
    """全市场超大单/大单/中单/小单净流入求和（元->亿元）。
       这是「主力 vs 散户」博弈最直接的证据：
       超大单≈机构、大单≈大户、小单≈散户。"""
    log("⑧ 主力资金构成 ...")
    if not spot:
        warn("无快照，主力资金构成跳过")
        return None
    s_main = s_xl = s_lg = s_md = s_sm = 0.0
    for x in spot:
        s_main += x.get("main_net") or 0.0
        s_xl += x.get("xlarge_net") or 0.0
        s_lg += x.get("large_net") or 0.0
        s_md += x.get("mid_net") or 0.0
        s_sm += x.get("small_net") or 0.0
    total_amt = sum(x["amount"] for x in spot if x["amount"])  # 元
    to_yi = lambda v: round(v / 1e8, 1)
    main_pct = round(s_main / total_amt * 100, 2) if total_amt else 0.0
    log("   主力 %.1f亿(占成交额%.1f%%) | 超大单 %.1f | 大单 %.1f | 中单 %.1f | 小单 %.1f"
        % (to_yi(s_main), main_pct, to_yi(s_xl), to_yi(s_lg), to_yi(s_md), to_yi(s_sm)))
    return {
        "main_net": to_yi(s_main), "xlarge_net": to_yi(s_xl),
        "large_net": to_yi(s_lg), "mid_net": to_yi(s_md), "small_net": to_yi(s_sm),
        "main_pct": main_pct,
        "main_in": s_main > 0, "retail_in": s_sm > 0,
    }


# ---------------------------------------------------------------- 9. 市场广度（涨跌家数 / 涨跌停）
def compute_market_breadth(cfg, spot):
    """涨跌家数 + 涨停/跌停家数。比成交额宽度更直观的「参与度」信号：
       涨多跌少=普涨；涨停多跌停少=投机热；反之=恐慌。"""
    log("⑨ 市场广度 ...")
    if not spot:
        warn("无快照，市场广度跳过")
        return None
    E = cfg["emotion"]
    up = sum(1 for x in spot if x["pct"] > 0)
    down = sum(1 for x in spot if x["pct"] < 0)
    flat = len(spot) - up - down
    lu = sum(1 for x in spot if x["pct"] >= float(E["limit_up_pct_main"]))
    ld = sum(1 for x in spot if x["pct"] <= -float(E["limit_up_pct_main"]))
    adv = up * 100.0 / len(spot) if spot else 50.0
    log("   上涨 %d / 下跌 %d / 平 %d | 涨停 %d / 跌停 %d | 上涨占比 %.1f%%"
        % (up, down, flat, lu, ld, adv))
    return {
        "up": up, "down": down, "flat": flat,
        "limit_up": lu, "limit_down": ld,
        "advance_pct": round(adv, 1), "limit_net": lu - ld,
    }


def idx_volume_ratio(cfg):
    """沪市当日成交量 / 近20日均量，作为量能温度"""
    try:
        kl = fetch_kline("1.000001", cfg, 25)
        if not kl or len(kl) < 21:
            return None
        vols = [float(x[5]) for x in kl]
        avg = sum(vols[-21:-1]) / 20.0
        return round(vols[-1] / avg, 3) if avg > 0 else None
    except Exception as e:
        warn("沪市量能基准获取失败: %s" % str(e)[:60])
        return None


# ---------------------------------------------------------------- 11. 期指升贴水（主力后市态度）
def fetch_sina_futures(cfg):
    """新浪期货期指连续合约最新价。新浪内盘期货(nf_)在沙箱 urllib 友好，无需东财。
    返回 {IF0: 最新价, ...}。"""
    syms = list(FUT_MAP.keys())
    url = SINA_FUT + ",".join("nf_" + s for s in syms)
    try:
        txt = http_get(url, cfg, enc="gbk", headers={"Referer": "https://finance.sina.com.cn/"})
    except Exception as e:
        warn("新浪期指行情获取失败: %s" % str(e)[:60])
        return {}
    out = {}
    for s in syms:
        m = re.search(r'var hq_str_nf_%s="([^"]*)"' % s, txt)
        if not m:
            continue
        parts = m.group(1).split(",")
        # 新浪内盘期货(nf_)格式：今开,最高,最低,最新价,买价,卖价,成交量,持仓,仓差,...
        # 最新价在 parts[3]（parts[7] 是买价一档，该 feed 恒为 0.000，不能当价格）。
        if len(parts) > 4:
            p = num(parts[3])
            if p:
                out[s] = p
    return out


def fetch_spot_index(cfg, secids):
    """拉一批东财指数现货（给定 secid 列表）。返回 {secid: 现价}。"""
    url = "%s?fltt=2&secids=%s&fields=f1,f2,f3,f12,f13,f14" % (EM_ULIST, ",".join(secids))
    try:
        j = get_json(url, cfg)
        diff = ((j.get("data") or {}).get("diff")) or []
        return {d.get("f12"): num(d.get("f2")) for d in diff if num(d.get("f2")) is not None}
    except Exception as e:
        warn("现货指数获取失败: %s" % str(e)[:60])
        return {}


def compute_futures_basis(cfg, futures):
    """期指升贴水 = 期货价 - 现货指数。
       升水(期货>现货)=主力看多后市；贴水=主力后市谨慎/避险。
       这是「主力对后市态度」最直接的衍生品信号。"""
    log("⑪ 期指升贴水 ...")
    T = cfg["capital_read"]["thresholds"]
    prem = float(T.get("basis_premium_pct", 0.10))
    disc = float(T.get("basis_discount_pct", -0.30))
    if not futures:
        warn("无期指数据，升贴水跳过")
        return None
    spots = fetch_spot_index(cfg, list(FUT_SPOT.values()))
    code_by_sec = {v: k for k, v in FUT_SPOT.items()}
    items, bps = [], []
    for sym, name in FUT_MAP.items():
        sec = FUT_SPOT[sym]
        code = sec.split(".")[1]
        fut = futures.get(sym)
        spot = spots.get(code)
        if fut is None or spot is None:
            continue
        basis = round(fut - spot, 2)
        bp = round(basis / spot * 100, 3)
        bps.append(bp)
        stance = "升水" if bp > prem else ("贴水" if bp < disc else "持平")
        items.append({"name": name, "fut": round(fut, 2), "spot": round(spot, 2),
                      "basis": basis, "basis_pct": bp, "stance": stance})
    if not items:
        warn("期指/现货配对失败，升贴水跳过")
        return None
    avg_bp = round(sum(bps) / len(bps), 3)
    if avg_bp > prem:
        attitude, note = "主力偏多", "四大期指整体升水，主力资金对后市偏乐观"
    elif avg_bp < disc:
        attitude, note = "主力偏空/避险", "四大期指整体贴水，主力对后市谨慎、偏向对冲/避险"
    else:
        attitude, note = "中性", "期指升贴水中性，主力态度不明确"
    log("   平均升贴水 %.3f%% (%s) | %s" % (avg_bp, attitude, "; ".join(
        "%s%s%.2f%%" % (i["name"], ("+" if i["basis_pct"] >= 0 else ""), i["basis_pct"]) for i in items)))
    return {"items": items, "avg_basis_pct": avg_bp,
            "stance": "升水" if avg_bp > prem else ("贴水" if avg_bp < disc else "持平"),
            "attitude": attitude, "note": note}


# ---------------------------------------------------------------- 12. ETF 净流向（机构借道进出）
def compute_etf_flow(cfg):
    """ETF 主力净流入(元->亿)。机构借道 ETF 进出是大资金态度的另一面镜子：
       宽基/红利 ETF 大额净流入=机构在低位布局；净流出=撤离。"""
    log("⑫ ETF 净流向 ...")
    try:
        url = "%s?pn=1&pz=%d&po=1&np=1&fltt=2&invt=2&fid=f62&fs=%s&fields=f12,f14,f2,f3,f62,f184" % (
            EM_SPOT, max(ETF_TOPN, 100), FS_ETF)
        d = (get_json(url, cfg).get("data") or {}).get("diff") or []
    except Exception as e:
        warn("ETF 资金流获取失败: %s" % str(e)[:60])
        return None
    rows = []
    for x in d:
        net = num(x.get("f62"))
        if net is None:
            continue
        rows.append({
            "code": x.get("f12"), "name": x.get("f14") or x.get("f12"),
            "pct": round(num(x.get("f3")) or 0.0, 2),
            "net_yi": round(net / 1e8, 2),
            "net_pct": round(num(x.get("f184")) or 0.0, 2),
        })
    if not rows:
        warn("ETF 列表为空，净流向跳过")
        return None
    rows.sort(key=lambda r: r["net_yi"], reverse=True)
    top_in = [r for r in rows if r["net_yi"] > 0][:ETF_TOPN]
    top_out = [r for r in rows if r["net_yi"] < 0][:ETF_TOPN][::-1]
    net_sum = round(sum(r["net_yi"] for r in rows), 1)
    T = cfg["capital_read"]["thresholds"]
    th = float(T.get("etf_net_yi", 20))
    # 宽基/指数 ETF 名称关键词：判断机构是否在借道布局
    broad_kw = ("沪深300", "上证50", "中证500", "中证1000", "科创50", "创业板", "ETF", "指数")
    broad_in = sum(r["net_yi"] for r in top_in if any(k in r["name"] for k in ("沪深300", "上证50", "中证500", "中证1000", "科创50", "创业板")))
    if net_sum > th:
        signal = "机构借道 ETF 净申购（偏布局）"
    elif net_sum < -th:
        signal = "ETF 净赎回（资金撤离）"
    else:
        signal = "ETF 资金流向中性"
    log("   ETF 净流入合计 %.1f 亿 | 宽基流入 %.1f 亿 | %s" % (net_sum, broad_in, signal))
    return {"top_in": top_in, "top_out": top_out, "net_sum_yi": net_sum,
            "broad_in_yi": round(broad_in, 1), "signal": signal}


# ---------------------------------------------------------------- 7. 综合研判（操作指引）
def calc_judgement(cfg, emotion, breadth, liquidity, margin, ratio, sector=None, capital_read=None):
    """把前面 6 个模块的信号，用可验证的规则融合成
       仓位 / 风格 / 节奏 三个操作维度 + 总览一句话。
       不预测点位、不荐股，只给「现在该怎么盯盘决策」的框架，
       每条结论都带原始数据证据，出错能回溯到具体阈值。"""
    log("⑦ 综合研判（操作指引）...")
    J = cfg["judgement"]
    E, L, B = J["emotion"], J["liquidity"], J["breadth"]
    mfi, mfo = float(J["margin_fast_in"]), float(J["margin_fast_out"])
    vs, ve = float(J["vol_shrink"]), float(J["vol_expand"])

    es = emotion["score"] if emotion else None
    el = emotion["level"] if emotion else "未知"
    lr = liquidity["risk"] if liquidity else None
    tr = liquidity["top_ratio"] if liquidity else None
    bw = breadth["width"] if breadth else None
    trend = breadth["trend"] if breadth else "未知"
    chg10 = margin["chg10"] if margin else None

    # =========================================================  仓位
    pos_ev = []
    if es is None:
        pos_ev.append("情绪数据缺失，按中性计"); emo_tag = "中性"
    elif es >= E["euphoria"]:
        pos_ev.append("情绪狂热 %.0f → 不宜加仓" % es); emo_tag = "狂热"
    elif es >= E["warm"]:
        pos_ev.append("情绪偏热 %.0f → 不加仓" % es); emo_tag = "偏热"
    elif es <= E["fear"]:
        pos_ev.append("情绪冰点 %.0f → 长线低吸窗口" % es); emo_tag = "冰点"
    elif es <= E["cool"]:
        pos_ev.append("情绪偏冷 %.0f → 可小幅低吸" % es); emo_tag = "偏冷"
    else:
        pos_ev.append("情绪中性 %.0f" % es); emo_tag = "中性"

    if lr is None:
        pos_ev.append("流动性数据缺失，按中性计")
    elif lr >= L["high"]:
        pos_ev.append("流动性高风险 %.0f → 降仓防御" % lr)
    elif lr >= L["mid"]:
        pos_ev.append("流动性中风险 %.0f → 控制追高" % lr)
    else:
        pos_ev.append("流动性适中 %.0f" % lr)

    if chg10 is None:
        pos_ev.append("两融数据缺失，按中性计")
    elif chg10 >= mfi:
        pos_ev.append("杠杆10日 +%.2f%% 快速进场 → 情绪偏热需谨慎" % chg10)
    elif chg10 <= mfo:
        pos_ev.append("杠杆10日 %.2f%% 撤离 → 风险释放中" % chg10)
    else:
        pos_ev.append("杠杆平稳(10日 %+.2f%%)" % chg10)

    if (lr is not None and lr >= L["high"]) or (es is not None and es >= E["euphoria"]):
        pos_label = "防御为主，降仓位"
    elif es is not None and es <= E["fear"]:
        pos_label = "逢低加仓（偏多）"
    elif es is not None and es >= E["warm"]:
        pos_label = "中性偏防守，不加仓"
    elif es is not None and es <= E["cool"]:
        pos_label = "中性偏多，可小仓低吸"
    else:
        pos_label = "中性，维持仓位"

    score = 50
    if es is not None:
        if es <= E["fear"]: score += 22
        elif es <= E["cool"]: score += 11
        elif es >= E["euphoria"]: score -= 25
        elif es >= E["warm"]: score -= 11
    if lr is not None:
        if lr >= L["high"]: score -= 20
        elif lr >= L["mid"]: score -= 8
        else: score += 5
    if chg10 is not None:
        if chg10 >= mfi: score -= 8
        elif chg10 <= mfo: score += 8
    score = round(min(100.0, max(0.0, score)), 0)
    pos_advice = {
        "防御为主，降仓位": "控制总仓位、降低杠杆暴露，优先保住利润。",
        "逢低加仓（偏多）": "别人恐惧时按分批原则低吸核心标的，不一把梭。",
        "中性偏防守，不加仓": "不加新仓、不追高，持有为主、等信号。",
        "中性偏多，可小仓低吸": "可用小仓位沿核心资产分批低吸。",
        "中性，维持仓位": "维持现有仓位，跟随主线即可。",
    }.get(pos_label, "")

    # =========================================================  风格
    sty_ev = []
    if trend == "头部领涨":
        sty_label = "大票/核心资产占优"
        sty_ev.append("头部 %.0f%% 强于尾部 %.0f%% → 资金聚焦权重" %
                      (breadth["head_pct"], breadth["tail_pct"]))
    elif trend == "尾部领涨":
        sty_label = "小微盘/题材活跃（不追）"
        sty_ev.append("尾部 %.0f%% 强于头部 %.0f%% → 微盘在冲，投机性高" %
                      (breadth["tail_pct"], breadth["head_pct"]))
    elif trend == "全面扩张":
        sty_label = "普涨格局，均衡配置"
        sty_ev.append("宽度 %.0f%% 偏高 → 多数个股走强" % bw)
    elif trend == "普遍收缩":
        sty_label = "各线走弱，防守为主"
        sty_ev.append("宽度 %.0f%% 偏低 → 多数个股走弱" % bw)
    else:
        sty_label = "结构分化，精选个股"
        sty_ev.append("头尾强度接近 → 方向未明")
    if tr is not None and tr >= float(cfg["liquidity"]["top_ratio_warn"]) * 100:
        sty_ev.append("资金集中度 %.0f%% 偏高 → 抱团核心，宜跟随头部" % tr)
    elif tr is not None:
        sty_ev.append("资金集中度 %.0f%% 适中" % tr)
    sty_advice = {
        "大票/核心资产占优": "契合蓝筹长线，沿景气核心资产持有/低吸。",
        "小微盘/题材活跃（不追）": "题材热闹但持续性差，长线账户不追、只看核心。",
        "普涨格局，均衡配置": "beta 行情，均衡持有即可。",
        "各线走弱，防守为主": "减少操作、控制回撤。",
        "结构分化，精选个股": "轻指数、重个股，精选景气细分。",
    }.get(sty_label, "")

    # =========================================================  节奏
    tim_ev = []
    if es is not None and es <= E["fear"]:
        tim_label = "分批低吸窗口"
    elif es is not None and es >= E["euphoria"]:
        tim_label = "不追高，等回踩"
    else:
        if ratio is None:
            tim_label = "持有观察，等信号"; tim_ev.append("量能基准缺失")
        elif ratio < vs:
            tim_label = "缩量观望，等方向"
            tim_ev.append("沪市量能比 %.2f < %.2f → 缩量" % (ratio, vs))
        elif ratio > ve:
            if es is not None and es >= E["warm"] and bw is not None and bw < B["strong"]:
                tim_label = "放量分歧，不追"
                tim_ev.append("量能比 %.2f 放量但宽度 %.0f 未跟上 → 警惕分歧" % (ratio, bw))
            else:
                tim_label = "放量待确认"
                tim_ev.append("量能比 %.2f 放量" % ratio)
        else:
            tim_label = "持有观察，等信号"
            tim_ev.append("量能比 %.2f 中性" % ratio)
    tim_advice = {
        "分批低吸窗口": "长线账户按计划分批低吸，越跌越买需有纪律。",
        "不追高，等回踩": "不追涨，等回调至支撑再考虑。",
        "缩量观望，等方向": "缩量无方向，观望为主。",
        "放量分歧，不追": "放量但非普涨，说明有分歧，不追。",
        "放量待确认": "放量出现，观察能否持续带动宽度。",
        "持有观察，等信号": "无明确信号，持有等待。",
    }.get(tim_label, "")

    # =========================================================  总览
    if es is not None and es >= E["euphoria"]:
        posture = "不宜追高，宜等回踩"
    elif es is not None and es <= E["fear"]:
        posture = "是长线低吸窗口"
    else:
        posture = "中性应对、不追不慌"
    summary = ("情绪%s(%.0f)、流动性%s(%.0f)、%s(宽度%.0f)：当前%s；"
               "仓位%s，风格%s，节奏%s。以上为数据框架，非投资建议。"
               % (el, es if es is not None else 0,
                  liquidity["level"] if liquidity else "未知", lr if lr is not None else 0,
                  trend, bw if bw is not None else 0,
                  posture, pos_label, sty_label, tim_label))

    log("   仓位[%s] 风格[%s] 节奏[%s] 进攻分%.0f" % (pos_label, sty_label, tim_label, score))

    # =========================================================  主线
    # 主线 = 行业资金净流入 Top 行业（描述性，不荐股），质量由主力视角状态机判定
    ml_ev = []
    top3 = (sector.get("top") or [])[:3] if isinstance(sector, dict) else []
    if top3:
        nf = "、".join("%s(+%.1f亿)" % (x.get("name", "?"), float(x.get("flow", 0))) for x in top3)
        ml_ev.append("行业净流入 Top3：%s" % nf)
        regime = (capital_read or {}).get("regime", "")
        posture = (capital_read or {}).get("posture", "")
        if regime in ("吸筹布局", "共振拉升"):
            ml_label = "增量主线·可跟随"
            ml_advice = "资金合力净流入、主力整体偏多（%s），当前主线可信，可沿主线核心标的分批低吸，不追高。" % posture
        elif regime in ("诱多派发", "出货撤退", "弱势阴跌"):
            ml_label = "存量轮动·不追"
            ml_advice = ("资金在「%s」等板块零散流入，但主力整体净流出、散户接盘，属板块间打游击的存量轮动，不是增量合力主线。"
                         "操作上：① 不追这些行业；② 仓位锚定核心资产/宽基ETF；③ 等主线确认（主力翻多+期指转升水）再动。") % nf
        else:
            ml_label = "主线不明·等确认"
            ml_advice = "主力态度混杂（%s）、方向未明，主线尚不可信，维持现有结构、不强行切换赛道。" % (posture or "震荡观望")
        if regime:
            ml_ev.append("主力视角：%s（%s）" % (regime, posture))
    else:
        ml_label = "主线不明"
        ml_advice = "行业资金流数据缺失，无法判定主线，维持现有持仓结构、不强行切换。"
        ml_ev.append("行业资金流数据缺失")

    return {
        "posture": posture, "summary": summary,
        "position": {"label": pos_label, "score": score, "advice": pos_advice, "evidence": pos_ev},
        "style": {"label": sty_label, "advice": sty_advice, "evidence": sty_ev},
        "timing": {"label": tim_label, "advice": tim_advice, "evidence": tim_ev},
        "mainline": {"label": ml_label, "advice": ml_advice, "evidence": ml_ev},
    }


# ---------------------------------------------------------------- 10. 主力视角 → 散户应对
def calc_capital_read(cfg, indices, breadth, emotion, liquidity, margin, sector, flow, mb, ratio, futures_basis=None, etf_flow=None):
    """把全部维度翻译成「主力资金此刻在干什么」的视角，再落到
       「作为一个小散户，我该怎么应对」。
       不预测点位、不荐股，只用可验证的订单流/广度/宽度/期指升贴水/ETF净流向信号做状态机分类。"""
    log("⑩ 主力视角 → 散户应对 ...")
    CR = cfg["capital_read"]
    T = CR.get("thresholds", {})
    flow_in = float(T.get("main_net_in_billion", 50))
    flow_out = float(T.get("main_net_out_billion", -50))
    xl_in_th = float(T.get("xlarge_in_billion", 30))
    xl_out_th = float(T.get("xlarge_out_billion", -30))
    sm_in_th = float(T.get("small_in_billion", 30))
    sm_out_th = float(T.get("small_out_billion", -30))
    adv_high_th = float(T.get("advance_high_pct", 60))
    adv_low_th = float(T.get("advance_low_pct", 40))
    limit_many_th = int(T.get("limit_up_many", 50))
    limit_down_many_th = int(T.get("limit_down_many", 20))
    prem_th = float(T.get("basis_premium_pct", 0.10))
    disc_th = float(T.get("basis_discount_pct", -0.30))
    etf_th = float(T.get("etf_net_yi", 20))
    J = cfg["judgement"]
    vs = float(J["vol_shrink"]); ve = float(J["vol_expand"])

    # ============ 信号提取（全部来自前面已算好的模块，零新网络请求）
    idx = {x["code"]: x for x in (indices or [])}
    sh = idx.get("000001")
    sh_pct = sh["pct"] if sh else 0.0
    idx_up = sh_pct > 0
    idx_strong = sh_pct > 0.5
    idx_down = sh_pct < 0
    idx_flat = -0.3 <= sh_pct <= 0.3

    bw = (breadth or {}).get("width", 50)
    width_high = bw > 60
    width_low = bw < 40
    width_mid = 40 <= bw <= 60
    head_strong = breadth and breadth.get("trend") == "头部领涨"
    tail_strong = breadth and breadth.get("trend") == "尾部领涨"

    es = (emotion or {}).get("score", 50)
    emo_hot = es >= 58
    emo_euph = es >= 70
    emo_cool = es <= 42
    emo_fear = es <= 30
    emo_neutral = 42 < es < 58

    lr = (liquidity or {}).get("risk", 50)
    liq_high = lr >= 70

    chg10 = (margin or {}).get("chg10")
    margin_in = chg10 is not None and chg10 >= 3
    margin_out = chg10 is not None and chg10 <= -3

    fl = flow or {}
    main_in = fl.get("main_net", 0) > flow_in
    main_out = fl.get("main_net", 0) < flow_out
    xl_in = fl.get("xlarge_net", 0) > xl_in_th
    xl_out = fl.get("xlarge_net", 0) < xl_out_th
    sm_in = fl.get("small_net", 0) > sm_in_th
    sm_out = fl.get("small_net", 0) < sm_out_th

    mb = mb or {}
    adv_pct = mb.get("advance_pct", 50)
    adv_high_b = adv_pct > adv_high_th
    adv_low_b = adv_pct < adv_low_th
    lu = mb.get("limit_up", 0); ld = mb.get("limit_down", 0)
    limit_many_b = lu >= limit_many_th
    limit_down_many_b = ld >= limit_down_many_th

    vol_shrink = ratio is not None and ratio < vs
    vol_expand = ratio is not None and ratio > ve

    # 期指升贴水：主力对后市的态度（升水=偏多，贴水=避险）
    fb = futures_basis or {}
    avg_bp = fb.get("avg_basis_pct")
    basis_bull = avg_bp is not None and avg_bp > prem_th
    basis_bear = avg_bp is not None and avg_bp < disc_th
    # ETF 净流向：机构借道进出（净流入=偏布局，净流出=撤离）
    ef = etf_flow or {}
    ef_sum = ef.get("net_sum_yi")
    etf_in = ef_sum is not None and ef_sum > etf_th
    etf_out = ef_sum is not None and ef_sum < -etf_th

    # ============ 状态机：6 种主力 regime，每条证据 (条件, 权重, 文字)
    regimes = [
        ("共振拉升", [
            (idx_up, 2, "指数红盘"),
            (idx_strong, 1, "上证涨幅>0.5%"),
            (width_high, 2, "成交额宽度>60%（多数个股走强）"),
            (main_in, 2, "主力净流入"),
            (xl_in, 1.5, "超大单(机构)净买入"),
            (emo_hot, 1, "情绪偏热但未狂热"),
            (adv_high_b, 1, "上涨家数占比>60%"),
            (limit_many_b, 0.5, "涨停家数偏多"),
            (basis_bull, 1, "期指升水（主力看多后市）"),
            (etf_in, 1, "ETF 净申购（机构借道布局）"),
        ]),
        ("诱多派发", [
            (idx_up or idx_flat, 3, "指数不弱（红或平）"),
            (width_low or width_mid, 1, "宽度未同步放大（分化）"),
            (main_out, 1.5, "主力净流出"),
            (sm_in, 1.5, "小单(散户)净买入（接盘）"),
            (emo_hot, 1.5, "情绪偏热"),
            (limit_many_b, 1, "涨停多但易炸板"),
            (head_strong, 0.5, "拉抬集中在头部权重"),
            (basis_bear, 1, "期指贴水（主力后市谨慎）"),
        ]),
        ("出货撤退", [
            (idx_down, 2, "指数下跌"),
            (main_out, 2, "主力净流出"),
            (xl_out, 1.5, "超大单(机构)净卖出"),
            (width_low, 1.5, "成交额宽度<40%"),
            (emo_cool or emo_fear, 1.5, "情绪转冷/冰点"),
            (limit_down_many_b, 1, "跌停家数偏多"),
            (liq_high, 0.5, "流动性高风险"),
            (basis_bear, 1.5, "期指贴水（避险情绪）"),
            (etf_out, 1, "ETF 净赎回（资金撤离）"),
        ]),
        ("洗盘震仓", [
            (idx_down or idx_flat, 1, "指数小跌/震荡"),
            (width_mid, 1, "宽度中性（未恶化）"),
            (xl_in, 1, "超大单仍在净买入"),
            (emo_neutral, 1, "情绪中性"),
            (vol_shrink, 1, "缩量（无恐慌抛压）"),
            ((main_out and not xl_out), 0.5, "大单流出但机构未撤"),
            (basis_bull, 0.5, "期指升水（非系统性风险）"),
        ]),
        ("吸筹布局", [
            (idx_down or idx_flat, 1, "指数震荡/小跌"),
            (main_in, 1.5, "主力净流入"),
            (xl_in, 1, "超大单(机构)低位吸筹"),
            (sm_out, 1.5, "小单(散户)净卖出（割肉）"),
            (emo_cool or emo_fear, 1.5, "情绪偏冷/冰点"),
            (width_low, 1, "宽度偏低（未启动）"),
            (etf_in, 1, "ETF 净申购（机构借道布局）"),
        ]),
        ("弱势阴跌", [
            (idx_down, 2, "指数下跌"),
            (main_out, 2, "主力持续净流出"),
            (width_low, 2, "成交额宽度<40%"),
            (emo_fear, 1.5, "情绪冰点"),
            (limit_down_many_b, 1, "跌停家数偏多"),
            (liq_high, 1, "流动性高风险"),
            (adv_low_b, 1, "上涨家数占比<40%"),
            (basis_bear, 1, "期指贴水（资金避险）"),
            (etf_out, 1, "ETF 净赎回"),
        ]),
    ]

    # 硬约束：上证指数明显下跌时，"诱多派发"语义前提破缺（"诱多"要求指数稳定）
    # 必须放在 regimes 定义「之后」，否则会被上面的原列表赋值覆盖成死代码
    no_trap_th = float(T.get("no_trap_when_sh_pct", -0.5))
    if sh_pct < no_trap_th:
        regimes = [(n, items) for n, items in regimes if n != "诱多派发"]
        log("   [硬约束] 上证 %.2f%% < %.2f%% → 剔除「诱多派发」" % (sh_pct, no_trap_th))

    scores = {}
    best, best_conf, best_fw, best_tw, best_ev = None, -1, 0, 0, []
    for name, items in regimes:
        fw = tw = 0.0; ev = []
        for cond, w, txt in items:
            tw += w
            if cond:
                fw += w; ev.append(txt)
        conf = round(fw / tw * 100) if tw else 0
        scores[name] = conf
        # 按「置信度」(命中/自身总权重) 选 regime，避免总权重大的 regime 靠绝对分胜出
        if conf > best_conf:
            best, best_conf, best_fw, best_tw, best_ev = name, conf, fw, tw, ev

    conf = best_conf
    if best_fw < 3:  # 信号过弱 -> 归为震荡观望
        posture_key = "震荡观望"
        reg_ev = ["各维度信号混杂、无主导 regime，以观望为主"]
        conf = min(conf, 40)
    else:
        posture_key = best
        reg_ev = best_ev

    retail = CR.get("retail", {}).get(posture_key, {})
    posture = retail.get("posture", "观望")
    actions = retail.get("actions", [])
    cautions = retail.get("cautions", [])

    log("   regime=%s 置信度=%d%% | 主力净流入%.1f亿 散户净流入%.1f亿 | 期指%s(%.3f%%) ETF%s(%.1f亿)"
        % (posture_key, conf, fl.get("main_net", 0), fl.get("small_net", 0),
           fb.get("attitude", "无"), avg_bp if avg_bp is not None else 0,
           ef.get("signal", "无"), ef_sum if ef_sum is not None else 0))
    return {
        "regime": posture_key,
        "confidence": conf,
        "evidence": reg_ev,
        "posture": posture,
        "actions": actions,
        "cautions": cautions,
        "scores": scores,
    }


# ---------------------------------------------------------------- 数据时效标签
def build_freshness():
    # 时效分类：实时=盘中有效；T-1=前一交易日收盘(两融)；unset=依赖当日K线、收盘才定论
    return {
        "capital_read":  {"label": "盘中未定·收盘定论", "cls": "unset"},
        "judgement":     {"label": "盘中未定·收盘定论", "cls": "unset"},
        "indices":       {"label": "实时", "cls": "live"},
        "breadth":       {"label": "实时", "cls": "live"},
        "emotion":       {"label": "实时", "cls": "live"},
        "liquidity":     {"label": "实时", "cls": "live"},
        "sector":        {"label": "实时", "cls": "live"},
        "futures_basis": {"label": "实时", "cls": "live"},
        "etf_flow":      {"label": "实时", "cls": "live"},
        "flow":          {"label": "实时", "cls": "live"},
        "breadth_count": {"label": "实时", "cls": "live"},
        "margin":        {"label": "T-1·昨日", "cls": "stale"},
    }


# ---------------------------------------------------------------- 主力状态当日轨迹
def append_timeline(data):
    """把本轮主力 regime 追加进当日轨迹 timeline.json（只留当日，跨日重置）。

    规则：同一 regime 连续采样合并成一段 (s~e)，只有发生状态切换才开新段；
    同一 HH:MM 内重复运行不产生新点（幂等）。这样页面能直接画"转换时间轴"，
    无需前端再压缩。"""
    cr = data.get("capital_read") or {}
    regime = cr.get("regime")
    if not regime:
        return  # 本轮状态机失败/缺失，不记
    now = datetime.now(timezone(timedelta(hours=8)))
    today = now.strftime("%Y-%m-%d")
    hm = now.strftime("%H:%M")
    tl = {"date": today, "segments": []}
    if os.path.exists(TL_PATH):
        try:
            with open(TL_PATH, "r", encoding="utf-8") as f:
                old = json.load(f)
            if old.get("date") == today:
                tl = old
        except Exception:
            pass
    segs = tl.setdefault("segments", [])
    conf = cr.get("confidence", 0)
    if segs and segs[-1]["regime"] == regime:
        # 状态未变：合并进最后一段（时间延伸）；同分钟则完全跳过
        if segs[-1]["e"] == hm:
            return
        segs[-1]["e"] = hm
        segs[-1]["confidence"] = conf
    else:
        segs.append({"s": hm, "e": hm, "regime": regime, "confidence": conf})
    with open(TL_PATH, "w", encoding="utf-8") as f:
        json.dump(tl, f, ensure_ascii=False, indent=1)
    # JSONP 兜底（file:// 双击 / 实时模式直读本地）
    tl_js = os.path.join(BASE, "timeline.js")
    with open(tl_js, "w", encoding="utf-8") as f:
        f.write("window.THERMO_TIMELINE = " + json.dumps(tl, ensure_ascii=False) + ";\n")


# ---------------------------------------------------------------- main
def main():
    t0 = time.time()
    cfg = load_cfg()
    no_kline = "--no-kline" in sys.argv
    log("=" * 60)
    log("A股市场温度计 · 开始计算")

    data = {
        "generated_at": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S GMT+8"),
        "scope_note": "宽度为成交额Top%d口径；流动性为全市场口径" % int(cfg["width"]["top_n"]),
        "warnings": WARN,
    }

    try:
        data["indices"] = fetch_indices(cfg)
    except Exception as e:
        warn("指数全景失败: %s" % str(e)[:90])
        data["indices"] = []

    spot = []
    try:
        spot = fetch_spot(cfg)
    except Exception as e:
        warn("全市场快照失败，本轮只输出指数: %s" % str(e)[:90])

    breadth = None
    if spot and not no_kline:
        ranked = sorted(spot, key=lambda x: x["amount"], reverse=True)[:int(cfg["width"]["top_n"])]
        klines = fetch_klines_batch(cfg, ranked)
        try:
            breadth = compute_breadth(cfg, spot, klines)
        except Exception as e:
            warn("宽度计算失败: %s" % str(e)[:90])
    elif no_kline:
        warn("本次以 --no-kline 运行，宽度/情绪降级")
    data["breadth"] = breadth

    try:
        data["liquidity"] = compute_liquidity(cfg, spot) if spot else None
    except Exception as e:
        warn("流动性计算失败: %s" % str(e)[:90])
        data["liquidity"] = None

    try:
        data["margin"] = fetch_margin(cfg)
    except Exception as e:
        warn("两融获取失败: %s" % str(e)[:90])
        data["margin"] = None

    try:
        data["sector"] = fetch_sector(cfg)
    except Exception as e:
        warn("行业资金流失败: %s" % str(e)[:90])
        data["sector"] = None

    ratio = idx_volume_ratio(cfg) if spot else None
    data["volume_ratio"] = ratio

    try:
        data["emotion"] = compute_emotion(cfg, spot, breadth, data.get("margin"), ratio) if spot else None
    except Exception as e:
        warn("情绪计算失败: %s" % str(e)[:90])
        data["emotion"] = None

    flow = None
    mb = None
    futures = None
    etf = None
    if spot:
        try:
            flow = compute_flow(cfg, spot)
        except Exception as e:
            warn("主力资金构成失败: %s" % str(e)[:90])
        try:
            mb = compute_market_breadth(cfg, spot)
        except Exception as e:
            warn("市场广度失败: %s" % str(e)[:90])
    data["flow"] = flow
    data["breadth_count"] = mb

    try:
        fut_raw = fetch_sina_futures(cfg)
        data["futures_basis"] = compute_futures_basis(cfg, fut_raw)
    except Exception as e:
        warn("期指升贴水失败: %s" % str(e)[:90])
        data["futures_basis"] = None
    try:
        data["etf_flow"] = compute_etf_flow(cfg)
    except Exception as e:
        warn("ETF 净流向失败: %s" % str(e)[:90])
        data["etf_flow"] = None

    try:
        data["capital_read"] = calc_capital_read(
            cfg, data.get("indices"), data.get("breadth"), data.get("emotion"),
            data.get("liquidity"), data.get("margin"), data.get("sector"),
            flow, mb, data.get("volume_ratio"),
            data.get("futures_basis"), data.get("etf_flow"))
    except Exception as e:
        warn("主力视角研判失败: %s" % str(e)[:90])
        data["capital_read"] = None

    # 综合研判放最后：需汇总 sector + capital_read 等全部模块
    try:
        data["judgement"] = calc_judgement(
            cfg, data.get("emotion"), data.get("breadth"),
            data.get("liquidity"), data.get("margin"), data.get("volume_ratio"),
            data.get("sector"), data.get("capital_read"))
    except Exception as e:
        warn("综合研判失败: %s" % str(e)[:90])
        data["judgement"] = None

    data["freshness"] = build_freshness()
    data["elapsed"] = round(time.time() - t0, 1)
    data["warnings"] = WARN

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)

    # JSONP 兜底：file:// 双击打开时 fetch 会被 CORS 拦截，
    # 页面退而读取本文件里的 window.THERMO_DATA。Pages 上仍优先用 data.json。
    js_path = os.path.join(BASE, "data.js")
    with open(js_path, "w", encoding="utf-8") as f:
        f.write("window.THERMO_DATA = " + json.dumps(data, ensure_ascii=False) + ";\n")

    # 主力 regime 当日轨迹（盘中加密采样 → 页面画转换时间轴）
    try:
        append_timeline(data)
    except Exception as e:
        warn("轨迹记录失败: %s" % str(e)[:90])

    log("=" * 60)
    log("完成，耗时 %.1fs -> %s" % (data["elapsed"], OUT_PATH))
    if WARN:
        log("警告 %d 条:" % len(WARN))
        for w in WARN:
            log("  - " + w)
    return data


if __name__ == "__main__":
    main()
