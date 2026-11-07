#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DLsite 收藏比对工具
- 扫描本地目录，从文件夹名提取 RJ 号，建立本地索引
- 抓取 DLsite 搜索结果页（自动翻页）
- 对搜索结果中本地没有的作品，抓取其作品页的 data-language-editions，
  获取该作品所有语言版本的 RJ 号，再与本地索引比对
- 比对结果持久化保存，可重新刷新；条目可标记忽略
- 单一标准库实现，无第三方依赖
"""
import argparse
import html as html_mod
import json
import os
import re
import stat as stat_mod
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))

# pythonw（无窗口后台启动）下 sys.stdout/stderr 是 None，
# 请求日志 sys.stderr.write 会让处理器崩溃、客户端收到空响应；重定向到 devnull
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")

CONFIG_PATH = os.path.join(BASE, "config.json")
LOCAL_INDEX_PATH = os.path.join(BASE, "local_index.json")
EDITIONS_CACHE_PATH = os.path.join(BASE, "editions_cache.json")
RESULTS_DIR = os.path.join(BASE, "results")
WEB_DIR = os.path.join(BASE, "web")

DEFAULT_CONFIG = {
    "local_dirs": [],
    "proxy": "auto",  # "auto" | "none" | "http://127.0.0.1:7897"
    "concurrency": 4,
    "cookie": "",  # 可选：DLsite 登录 Cookie，用于收藏夹等需登录页面
}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
RJ_RE = re.compile(r"RJ\d{6,9}", re.I)
PAGE_LINK_RE = re.compile(r"(?<!per_)page/(\d+)")
EDITIONS_RE = re.compile(r"data-language-editions='(.*?)'", re.S)
TITLE_RE = re.compile(r'multiline_truncate"><a [^>]*title="([^"]*)"', re.S)
TITLE_TEXT_RE = re.compile(r'multiline_truncate"><a [^>]*>([^<]*)</a>', re.S)
MAKER_RE = re.compile(r'class="maker_name">\s*(?:<a[^>]*>)?\s*([^<]+?)\s*(?:</a>)?\s*<', re.S)

PROXY_CANDIDATES = ["http://127.0.0.1:7897", "http://127.0.0.1:7890",
                    "http://127.0.0.1:10809", "http://127.0.0.1:10808",
                    "http://127.0.0.1:1080"]

_config_lock = threading.Lock()
_editions_lock = threading.Lock()
_records_lock = threading.Lock()


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def get_config():
    with _config_lock:
        cfg = load_json(CONFIG_PATH, None)
        if not cfg:
            cfg = dict(DEFAULT_CONFIG)
            save_json(CONFIG_PATH, cfg)
        return cfg


def save_config(cfg):
    with _config_lock:
        save_json(CONFIG_PATH, cfg)


def make_opener(proxy):
    if proxy and proxy != "none":
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def fetch(url, proxy, timeout=30):
    cookie = "adultchecked=1"
    # 收藏夹等需登录的页面：附上用户在设置里填的 Cookie（仅发往 dlsite.com）
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host == "dlsite.com" or host.endswith(".dlsite.com"):
        user_cookie = (get_config().get("cookie") or "").strip()
        if user_cookie:
            cookie = cookie + "; " + user_cookie
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Cookie": cookie,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "ja-JP,ja;q=0.9",
    })
    opener = make_opener(proxy)
    with opener.open(req, timeout=timeout) as resp:
        final_url = resp.geturl()
        raw = resp.read()
    final_host = (urllib.parse.urlparse(final_url).hostname or "").lower()
    if final_host == "login.dlsite.com":
        raise RuntimeError("DLsite 要求登录后才能访问该页面："
                           "请在「目录与代理设置」里填入你的 DLsite Cookie 后重试。")
    return raw.decode("utf-8", errors="replace")


def detect_proxy():
    """依次尝试候选代理和直连，返回可用的 proxy 字符串（'none' 表示直连）。"""
    test_url = "https://www.dlsite.com/maniax/"
    for proxy in PROXY_CANDIDATES + ["none"]:
        try:
            fetch(test_url, proxy, timeout=8)
            return proxy
        except Exception:
            continue
    return None


def resolve_proxy(cfg):
    proxy = cfg.get("proxy", "auto")
    if proxy == "auto":
        detected = cfg.get("proxy_detected")
        if detected:
            return detected
        detected = detect_proxy()
        if detected:
            cfg["proxy_detected"] = detected
            save_config(cfg)
        return detected
    return proxy


# ---------------- 本地索引 ----------------

SCAN_CACHE_PATH = os.path.join(BASE, "scan_cache.json")


def scan_local(dirs):
    """增量扫描本地目录，返回 {rj: [路径,...]}。
    缓存所有已知目录的 mtime（scan_cache.json）。目录 mtime 只在其直接子项
    增删/改名时变化，因此：已知目录 mtime 不变 → 其直接子项不变；子树任何
    一级有新增目录，该级父目录 mtime 必变 → 会被重扫。未变化的目录不再枚举
    子目录（跳过作品文件夹里成千上万个音声文件的列举），只逐个 stat 校验。
    注意：不能简单地对 RJ 文件夹剪枝——实测存在中文版文件夹嵌套在日语版
    文件夹内等情况，剪枝会漏掉这些作品。"""
    cache = load_json(SCAN_CACHE_PATH, {})
    rj_map = {}
    new_cache = {}
    stale = []
    # 只采信当前配置目录范围内的缓存路径：目录从设置里移除后，
    # 其下旧缓存条目即使还在磁盘上也不能再计入本地库
    norm_dirs = [os.path.normcase(os.path.normpath(d)) for d in dirs]

    def in_scope(path):
        pn = os.path.normcase(os.path.normpath(path))
        return any(pn == d or pn.startswith(d + os.sep) for d in norm_dirs)

    def record(path):
        for m in RJ_RE.finditer(os.path.basename(path)):
            rj = m.group(0).upper()
            if rj not in rj_map:
                rj_map[rj] = [path]
            elif len(rj_map[rj]) < 3:
                rj_map[rj].append(path)

    # 1) 逐个 stat 校验已知目录：mtime 不变则直接采信，变化的标记重扫
    for path, mt in cache.items():
        if not in_scope(path):
            continue  # 已从配置移除的目录：不再采信，也不进新缓存
        try:
            st = os.stat(path)
        except OSError:
            continue  # 已删除/移动：不进新缓存，也不记录 RJ
        if not stat_mod.S_ISDIR(st.st_mode):
            continue
        if st.st_mtime == mt:
            new_cache[path] = mt
            record(path)
        else:
            stale.append(path)

    # 2) 重扫变化的子树与顶层目录；遇到 mtime 不变的子目录则剪枝
    def walk(root):
        try:
            entries = list(os.scandir(root))
        except OSError:
            return
        subdirs = []
        for e in entries:
            try:
                if not e.is_dir():
                    continue
                mt = e.stat().st_mtime
            except OSError:
                continue
            p = e.path
            new_cache[p] = mt
            record(p)
            if cache.get(p) == mt:
                continue  # 未变化：其子树已在第 1 步采信，不再深入
            subdirs.append(p)
        for p in subdirs:
            walk(p)

    for root in list(dirs) + stale:
        if not os.path.isdir(root):
            continue
        try:
            new_cache[root] = os.stat(root).st_mtime
        except OSError:
            pass
        record(root)
        walk(root)
    save_json(SCAN_CACHE_PATH, new_cache)
    return rj_map


def get_local_index():
    return load_json(LOCAL_INDEX_PATH, None)


def rescan_local():
    cfg = get_config()
    rj_map = scan_local(cfg.get("local_dirs", []))
    idx = {"scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "dirs": cfg.get("local_dirs", []),
           "count": len(rj_map),
           "rj_map": rj_map}
    save_json(LOCAL_INDEX_PATH, idx)
    return idx


# ---------------- DLsite 解析 ----------------

def normalize_search_url(url):
    """去掉已有分页；搜索结果页统一 per_page/100，返回 (path, query_string)"""
    url = url.split("#")[0].strip()
    m = re.match(r"(https?://[^?#]+)(\?.*)?$", url)
    if not m:
        raise ValueError("URL 无法识别")
    path, qs = m.group(1), m.group(2) or ""
    host = (urllib.parse.urlparse(path).hostname or "").lower()
    if not (host == "dlsite.com" or host.endswith(".dlsite.com")):
        raise ValueError("仅支持 DLsite 搜索结果页或收藏夹链接")
    path = re.sub(r"/page/\d+/?", "/", path)
    if re.search(r"/per_page/\d+", path):
        path = re.sub(r"/per_page/\d+", "/per_page/100", path)
    elif "/fsr/" in path:
        # 只有搜索结果页确定支持 per_page；收藏夹等页面保持原样
        path = path.rstrip("/") + "/per_page/100"
    return path.rstrip("/"), qs


def make_page_url(base, qs, n):
    if n <= 1:
        return base + "/" + qs
    return base + "/page/%d" % n + qs


def parse_search_page(html_text):
    """返回 (items, last_page)。items: [{rj, title, maker}]"""
    items = []
    chunks = html_text.split('data-list_item_product_id="')
    for chunk in chunks[1:]:
        m = re.match(r'(RJ\d+)"', chunk)
        if not m:
            continue
        rj = m.group(1).upper()
        end = chunk.find("</li>")
        block = chunk[:end if end > 0 else 8000]
        tm = TITLE_RE.search(block)
        title = html_mod.unescape(tm.group(1)).strip() if tm else ""
        mm = MAKER_RE.search(block)
        maker = html_mod.unescape(mm.group(1)).strip() if mm else ""
        items.append({"rj": rj, "title": title, "maker": maker})
    if not items and '<tile-work-item' in html_text:
        # 收藏夹（mypage/wishlist）：tile-work-item 组件，标题是链接文本，
        # 社团在 dd.maker_name 里（与搜索页相同的 class）
        seen = set()
        for chunk in html_text.split('<tile-work-item')[1:]:
            m = re.search(r'product-id="(RJ\d+)"', chunk)
            if not m:
                continue
            rj = m.group(1).upper()
            if rj in seen:
                continue
            seen.add(rj)
            end = chunk.find('</tile-work-item>')
            block = chunk[:end if end > 0 else 10000]
            tm = TITLE_RE.search(block) or TITLE_TEXT_RE.search(block)
            title = html_mod.unescape(tm.group(1)).strip() if tm else ""
            mm = MAKER_RE.search(block)
            maker = html_mod.unescape(mm.group(1)).strip() if mm else ""
            items.append({"rj": rj, "title": title, "maker": maker})
    if not items:
        # 兜底：其他结构未知的页面，直接从作品链接提取 RJ 号
        seen = set()
        for m in re.finditer(
                r'href="[^"]*product_id/(RJ\d+)[^"]*"([^>]*)>',
                html_text):
            rj = m.group(1).upper()
            if rj in seen:
                continue
            seen.add(rj)
            tm = re.search(r'title="([^"]*)"', m.group(2))
            title = html_mod.unescape(tm.group(1)).strip() if tm else ""
            items.append({"rj": rj, "title": title, "maker": ""})
    pages = [int(x) for x in PAGE_LINK_RE.findall(html_text)]
    last_page = max(pages) if pages else 1
    return items, last_page


def parse_editions(html_text, self_rj):
    m = EDITIONS_RE.search(html_text)
    if not m:
        return [{"workno": self_rj, "display_label": "", "lang": ""}]
    try:
        data = json.loads(html_mod.unescape(m.group(1)))
        out = []
        for d in data:
            wn = str(d.get("workno", "")).upper()
            if RJ_RE.fullmatch(wn):
                out.append({"workno": wn,
                            "display_label": d.get("display_label", ""),
                            "lang": d.get("lang", "")})
        return out or [{"workno": self_rj, "display_label": "", "lang": ""}]
    except Exception:
        return [{"workno": self_rj, "display_label": "", "lang": ""}]


def get_editions_cached():
    with _editions_lock:
        return load_json(EDITIONS_CACHE_PATH, {})


def save_editions_cache(cache):
    with _editions_lock:
        save_json(EDITIONS_CACHE_PATH, cache)


# ---------------- 比对结果记录 ----------------

def record_path(rid):
    if not re.fullmatch(r"[A-Za-z0-9_\-]+", rid or ""):
        return None
    return os.path.join(RESULTS_DIR, rid + ".json")


def list_records():
    os.makedirs(RESULTS_DIR, exist_ok=True)
    out = []
    with _records_lock:
        for fn in os.listdir(RESULTS_DIR):
            if not fn.endswith(".json"):
                continue
            rec = load_json(os.path.join(RESULTS_DIR, fn), None)
            if not rec:
                continue
            out.append({"id": rec.get("id"), "name": rec.get("name"),
                        "url": rec.get("url"), "max_pages": rec.get("max_pages", 0),
                        "created_at": rec.get("created_at"),
                        "updated_at": rec.get("updated_at"),
                        "summary": rec.get("summary")})
    out.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return out


def load_record(rid):
    p = record_path(rid)
    if not p:
        return None
    with _records_lock:
        return load_json(p, None)


def save_record(rec):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    p = record_path(rec["id"])
    with _records_lock:
        save_json(p, rec)


def delete_record(rid):
    p = record_path(rid)
    if p and os.path.exists(p):
        with _records_lock:
            os.remove(p)
        return True
    return False


GLOBAL_IGNORE_PATH = os.path.join(BASE, "global_ignore.json")
_global_ignore_lock = threading.Lock()


def load_global_ignore():
    """全局忽略列表：任何记录里忽略过的作品/社团，对所有比对结果生效。
    首次调用时把各旧记录里的忽略合并迁移进来。"""
    with _global_ignore_lock:
        gi = load_json(GLOBAL_IGNORE_PATH, None)
        if gi is None:
            rjs, makers = set(), set()
            os.makedirs(RESULTS_DIR, exist_ok=True)
            for fn in os.listdir(RESULTS_DIR):
                if not fn.endswith(".json"):
                    continue
                rec = load_json(os.path.join(RESULTS_DIR, fn), None) or {}
                rjs.update(rec.get("ignored", []))
                makers.update(rec.get("ignored_makers", []))
            gi = {"ignored": sorted(rjs), "ignored_makers": sorted(makers)}
            save_json(GLOBAL_IGNORE_PATH, gi)
        return gi


def save_global_ignore(gi):
    with _global_ignore_lock:
        save_json(GLOBAL_IGNORE_PATH, gi)


GLOBAL_FAV_PATH = os.path.join(BASE, "global_fav.json")
_global_fav_lock = threading.Lock()
def load_favorites():
    """全局收藏列表：收藏的作品对所有比对结果生效（移入收藏标签）"""
    with _global_fav_lock:
        fav = load_json(GLOBAL_FAV_PATH, None)
        if fav is None:
            fav = {"favorites": []}
            save_json(GLOBAL_FAV_PATH, fav)
        return fav


def save_favorites(fav):
    with _global_fav_lock:
        save_json(GLOBAL_FAV_PATH, fav)


# ---------------- 个人评级（本地库打星 1-5） ----------------
RATINGS_PATH = os.path.join(BASE, "ratings.json")
# 手动评级标记：ratings.json 里值分两类——DLsite 导入的可被再次导入刷新，
# 用户手动打过的记入本表，导入时跳过、永不被覆盖
RATINGS_MANUAL_PATH = os.path.join(BASE, "ratings_manual.json")
_ratings_lock = threading.Lock()

def load_ratings():
    """评级表：{rj: 1-5 整数（手动）或 DLsite 两位小数评分（导入）}，仅存本地库视图，不影响比对逻辑"""
    with _ratings_lock:
        r = load_json(RATINGS_PATH, None)
        if r is None:
            r = {}
            save_json(RATINGS_PATH, r)
        return r


def save_ratings(r):
    with _ratings_lock:
        save_json(RATINGS_PATH, r)


def load_ratings_manual():
    """手动打过分过的 RJ 集合（list 存储，返回 set）"""
    with _ratings_lock:
        return set(load_json(RATINGS_MANUAL_PATH, []))


def save_ratings_manual(s):
    with _ratings_lock:
        save_json(RATINGS_MANUAL_PATH, sorted(s))


# ---------------- 资源站检测 ----------------
import rescheck

RESCACHE_PATH = os.path.join(BASE, "resource_cache.json")
_rescache_lock = threading.Lock()


def load_rescache():
    with _rescache_lock:
        return load_json(RESCACHE_PATH, {})


def save_rescache(c):
    with _rescache_lock:
        save_json(RESCACHE_PATH, c)


def version_rjs_of(it, tinfo=None, alias=None):
    """作品的全部语言版本 RJ：自身 + editions，并递归展开别名和翻译站父子/原始版本。"""
    rj = it["rj"]
    queue = [rj]
    for e in it.get("editions") or []:
        if e.get("workno"):
            queue.append(e["workno"])
    alias = load_alias() if alias is None else alias
    tinfo = load_tinfo() if tinfo is None else tinfo
    group = []
    seen = set()
    while queue:
        cur = queue.pop(0)
        if not cur or cur in seen:
            continue
        seen.add(cur)
        group.append(cur)
        linked = []
        if alias.get(cur):
            linked.append(alias[cur])
        e = tinfo.get(cur) or {}
        for k in ("p", "o"):
            if e.get(k):
                linked.append(e[k])
        linked.extend(e.get("c") or [])
        queue.extend(x for x in linked if x and x not in seen)
    return group


def check_item_resources(it, proxy, force=False):
    """检测一个作品（所有语言版本）在三个资源站的情况，写缓存，返回 {site: {f,u,via}}。
    force=True 时忽略缓存强制重查（有些作品可能后来才发布资源）"""
    group = version_rjs_of(it)
    cache = load_rescache()
    out = {}
    dirty = False

    def cached(site, rj):
        if force:
            return None
        c = (cache.get(rj) or {}).get(site)
        return c if rescheck.cache_valid(site, c) else None

    def one_site(site):
        nonlocal dirty
        notfound = error = None
        for rj in group:
            c = cached(site, rj)
            if c and c.get("f") is True:
                return dict(c, via=rj)
        for rj in group:
            c = cached(site, rj)
            if c is None:
                r = rescheck.check_site(site, rj, proxy)
                cache.setdefault(rj, {})[site] = dict(r, t=int(time.time()))
                dirty = True
            else:
                r = c
            if r.get("f") is True:
                return dict(r, via=rj)
            if r.get("f") is False and notfound is None:
                notfound = dict(r, via=rj)
            if r.get("f") is None and error is None:
                error = dict(r, via=rj)
        return notfound or error or {"f": None, "u": rescheck.search_url(site, group[0])}

    with ThreadPoolExecutor(max_workers=4) as ex:
        for site, res in zip(rescheck.SITE_KEYS, ex.map(lambda s: one_site(s), rescheck.SITE_KEYS)):
            out[site] = res
    if dirty:
        save_rescache(cache)
    return out


def resources_from_cache(it, cache, tinfo=None, alias=None):
    """纯缓存聚合（注入 /api/result 用，不联网）。tinfo/alias 由调用方按请求复用。"""
    group = version_rjs_of(it, tinfo=tinfo, alias=alias)
    out = {}
    for site in rescheck.SITE_KEYS:
        best = None
        for rj in group:
            c = (cache.get(rj) or {}).get(site)
            if not rescheck.cache_valid(site, c):
                continue
            if c.get("f") is True:
                best = dict(c, via=rj)
                break
            if best is None:
                best = dict(c, via=rj)
        if best:
            out[site] = best
    return out


# ---------------- 翻译版 RJ 别名 ----------------# 同一部翻译作品在翻译站的 RJ 号和主站不同（如翻译站 RJ01014281 ↔ 主站 RJ01014280）。
# 翻译站作品页里的 parent-product-id 指向主站版本 RJ，据此建立别名表。
ALIAS_PATH = os.path.join(BASE, "rj_alias.json")
_alias_lock = threading.Lock()
CN_HINT_RE = re.compile(r"中文版|简体|繁体|简中|汉化|漢化|中文化|中文")
PARENT_RE = re.compile(r'parent-product-id="(RJ\d{6,9})"')


def load_alias():
    with _alias_lock:
        return load_json(ALIAS_PATH, {})


def save_alias(alias):
    with _alias_lock:
        save_json(ALIAS_PATH, alias)


def parse_translation_parent(html_text, self_rj):
    """翻译站作品页：返回主站版本 RJ；非翻译版返回 None"""
    if 'is-translation-parent' not in html_text and 'translation-product' not in html_text:
        return None
    m = PARENT_RE.search(html_text)
    if m and m.group(1).upper() != self_rj:
        return m.group(1).upper()
    return None


def build_aliases(proxy, progress_cb=None):
    """扫描本地索引里文件夹名带中文版本提示的 RJ，抓取作品页建立别名表。
    返回 (新增条数, 别名总数, 检查过的候选数)"""
    idx = get_local_index()
    if not idx:
        idx = rescan_local()
    rj_map = idx.get("rj_map", {})
    alias = load_alias()
    before_n = len(alias)
    # 候选：任一路径带中文提示、且还不在别名表里的 RJ
    candidates = []
    for rj, paths in rj_map.items():
        if rj in alias:
            continue
        if any(CN_HINT_RE.search(p) for p in paths):
            candidates.append(rj)
    done_n = 0
    sections = ["maniax", "girls", "bl"]

    def resolve(rj):
        for sec in sections:
            try:
                h = fetch(work_url(sec, rj), proxy)
                return parse_translation_parent(h, rj)
            except Exception:
                continue
        return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(resolve, rj): rj for rj in candidates}
        for fut in as_completed(futs):
            rj = futs[fut]
            try:
                target = fut.result()
                if target:
                    alias[rj] = target
            except Exception:
                pass
            done_n += 1
            if progress_cb:
                progress_cb(done_n, len(candidates))
    save_alias(alias)
    return len(alias) - before_n, len(alias), len(candidates)


# ---------------- 翻译站版本信息（批量 API） ----------------
# /maniax/product/info/ajax 返回的 translation_info 同时给出双向关系：
#   主站版本: is_parent + child_worknos（翻译站 RJ 列表）
#   翻译站版本: is_child + parent_workno
# 缓存 translate_info.json：{rj: {"p": 主站RJ|None, "c": [翻译站RJ,...]}}，
# 空 dict 表示已查询过、无翻译版本关系（避免重复请求）。
TRANSLATE_INFO_PATH = os.path.join(BASE, "translate_info.json")
_tinfo_lock = threading.Lock()
TINFO_BATCH = 40


def load_tinfo():
    with _tinfo_lock:
        return load_json(TRANSLATE_INFO_PATH, {})


def save_tinfo(t):
    with _tinfo_lock:
        save_json(TRANSLATE_INFO_PATH, t)


def fetch_translate_info(rjs, proxy, progress_cb=None, cancel_event=None):
    """批量查询 product/info/ajax，更新缓存，并把 翻译站RJ→主站RJ 写入别名表。
    缓存条目：{"d": 发售日期, "p": 主站RJ, "c": [翻译站RJ], "o": 原始版本RJ,
              "r": DLsite 评分 rate_average_2dp（无评分为 None）}。
    返回本次新查询的 RJ 数。缺发售日期的旧缓存条目会自动重查补齐；
    失败的批次不写入缓存，下次运行自动重试。"""
    tinfo = load_tinfo()
    todo = [r for r in dict.fromkeys(rjs)
            if r not in tinfo
            or "d" not in (tinfo.get(r) or {})
            or "m" not in (tinfo.get(r) or {})
            or "r" not in (tinfo.get(r) or {})]
    if not todo:
        return 0
    alias = load_alias()
    alias_dirty = False
    batches = [todo[i:i + TINFO_BATCH] for i in range(0, len(todo), TINFO_BATCH)]
    done = 0

    def query(batch):
        url = ("https://www.dlsite.com/maniax/product/info/ajax?product_id="
               + ",".join(batch) + "&cdn_cache=3")
        return json.loads(fetch(url, proxy))

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(query, b): b for b in batches}
        for fut in as_completed(futs):
            if cancel_event is not None and cancel_event.is_set():
                break
            batch = futs[fut]
            try:
                data = fut.result()
            except Exception as e:
                sys.stderr.write("tinfo batch failed: %s\n" % e)
                done += len(batch)
                continue
            for rj in batch:
                v = data.get(rj)
                if not v:
                    tinfo[rj] = {}
                    continue
                ti = v.get("translation_info") or {}
                parent = (ti.get("parent_workno") or "").upper() or None
                children = [str(c).upper() for c in (ti.get("child_worknos") or [])]
                original = (ti.get("original_workno") or "").upper() or None
                date = str(v.get("regist_date") or "")[:10]
                maker_id = str(v.get("maker_id") or "").upper() or None
                rate_raw = v.get("rate_average_2dp")
                try:
                    rate = round(float(rate_raw), 2) if rate_raw is not None else None
                except (TypeError, ValueError):
                    rate = None
                entry = {"d": date} if date else {}
                entry["r"] = rate  # 显式存 None 表示"已查过、无评分"，避免重复查询
                if maker_id:
                    entry["m"] = maker_id
                if parent or children or original:
                    entry.update({"p": parent, "c": children, "o": original})
                tinfo[rj] = entry
                if parent and alias.get(rj) != parent:
                    alias[rj] = parent
                    alias_dirty = True
                for c in children:
                    if alias.get(c) != rj:
                        alias[c] = rj
                        alias_dirty = True
            done += len(batch)
            if progress_cb:
                progress_cb(done, len(todo))
    save_tinfo(tinfo)
    if alias_dirty:
        save_alias(alias)
    return len(todo)


# ---------------- 社团名缓存 ----------------
# 批量接口只有 maker_id（RG 号），社团名从社团主页 <span class="original_name"> 提取
MAKER_CACHE_PATH = os.path.join(BASE, "maker_cache.json")
_maker_lock = threading.Lock()
MAKER_NAME_RE = re.compile(r'<span class="original_name">([^<]+)</span>')


def load_maker_cache():
    with _maker_lock:
        return load_json(MAKER_CACHE_PATH, {})


def save_maker_cache(m):
    with _maker_lock:
        save_json(MAKER_CACHE_PATH, m)


def fetch_maker_names(rgs, proxy, progress_cb=None):
    """按 RG 号抓取社团主页提取社团名，缓存到 maker_cache.json。返回新查询数。"""
    cache = load_maker_cache()
    todo = [r for r in dict.fromkeys(rgs) if r and r not in cache]
    if not todo:
        return 0
    done = 0

    def query(rg):
        url = ("https://www.dlsite.com/maniax/circle/profile/=/maker_id/%s.html" % rg)
        h = fetch(url, proxy)
        m = MAKER_NAME_RE.search(h)
        return html_mod.unescape(m.group(1)).strip() if m else ""

    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(query, rg): rg for rg in todo}
        for fut in as_completed(futs):
            rg = futs[fut]
            try:
                name = fut.result()
                cache[rg] = name  # 空串也缓存，避免重复请求失效社团
            except Exception as e:
                sys.stderr.write("maker %s failed: %s\n" % (rg, e))
            done += 1
            if progress_cb:
                progress_cb(done, len(todo))
    save_maker_cache(cache)
    return len(todo)


def release_date_of(rj, tinfo):
    """最初的发售时间：翻译版本取原始版本（original_workno）的日期，否则取自身日期"""
    e = tinfo.get(rj) or {}
    o = e.get("o")
    if o:
        d = (tinfo.get(o) or {}).get("d")
        if d:
            return d
    return e.get("d") or ""


def apply_ignores(rec):
    """根据全局忽略/收藏列表刷新条目状态与统计，并把列表同步进记录。
    同时做旧记录自愈：比对后新增的本地 RJ 或翻译站别名，可把 missing 改判为 owned。"""
    gi = load_global_ignore()
    fav = load_favorites()
    ignored = set(gi.get("ignored", []))
    makers = set(gi.get("ignored_makers", []))
    favs = set(fav.get("favorites", []))
    idx = get_local_index()
    rj_map = idx.get("rj_map", {}) if idx else {}
    local_rj = set(rj_map.keys())
    alias = load_alias()
    alias_to_local = {}
    for a, t in alias.items():
        if a in local_rj:
            alias_to_local.setdefault(t, []).append(a)
    items = rec.get("items", [])
    favs_changed = False
    for it in items:
        # 自愈：missing 的条目用最新本地索引 + 别名表（含翻译站子版本）重新判定
        if it.get("status") == "missing":
            matched = []
            if it["rj"] in local_rj:
                matched.append(it["rj"])
            for e in (it.get("editions") or []):
                if e["workno"] in local_rj and e["workno"] not in matched:
                    matched.append(e["workno"])
                for a in alias_to_local.get(e["workno"], []):
                    if a not in matched:
                        matched.append(a)
            for a in alias_to_local.get(it["rj"], []):
                if a not in matched:
                    matched.append(a)
            t0 = alias.get(it["rj"])
            if t0 and t0 in local_rj and t0 not in matched:
                matched.append(t0)
            if matched:
                it["status"] = "owned"
                it["matched"] = matched
                it["local_paths"] = rj_map.get(matched[0], [])[:1]
        it["ignored"] = it.get("rj") in ignored or bool(
            it.get("maker") and it["maker"] in makers)
        # 收藏 = 想下但暂未有资源的观察清单：已下载的条目自动脱离收藏
        if it.get("status") == "owned" and it.get("rj") in favs:
            favs.discard(it["rj"])
            favs_changed = True
        it["favorited"] = it.get("rj") in favs
    rec["summary"]["owned"] = sum(1 for it in items if it.get("status") == "owned")
    rec["summary"]["missing"] = sum(
        1 for it in items
        if it.get("status") == "missing" and not it.get("ignored") and not it.get("favorited"))
    rec["summary"]["ignored"] = sum(1 for it in items if it.get("ignored"))
    rec["summary"]["favorited"] = sum(
        1 for it in items if it.get("favorited") and not it.get("ignored"))
    if favs_changed:
        fav["favorites"] = sorted(favs)
        save_favorites(fav)
    rec["ignored"] = sorted(ignored)
    rec["ignored_makers"] = sorted(makers)
    rec["favorites"] = sorted(favs)


def reapply_all_records():
    """全局忽略/收藏变更后，重新套用并落盘所有记录，
    保证其他记录的忽略/收藏标签和记录列表统计立即同步。"""
    os.makedirs(RESULTS_DIR, exist_ok=True)
    for fn in os.listdir(RESULTS_DIR):
        if not fn.endswith(".json"):
            continue
        rec = load_json(os.path.join(RESULTS_DIR, fn), None)
        if rec:
            apply_ignores(rec)
            save_record(rec)


def guess_record_name(url):
    """从 URL 提取关键词等作为记录名"""
    hint = ""
    m = re.search(r"/keyword/([^/]+)", url)
    if m:
        try:
            hint = urllib.parse.unquote(m.group(1))
        except Exception:
            hint = m.group(1)
    else:
        m = re.search(r"/work_type\[0\]/([A-Za-z0-9_]+)", url)
        if m:
            hint = m.group(1)
    ts = time.strftime("%m-%d %H:%M")
    return ("%s · %s" % (hint, ts)) if hint else ("搜索结果 · %s" % ts)


# ---------------- 比对任务 ----------------

class CompareJob:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = "idle"  # idle|running|done|error|cancelled
        self.message = ""
        self.phase_done = 0
        self.phase_total = 0
        self.record_id = None
        self.error = None
        self.cancel_event = threading.Event()
        self.started_at = None

    def set(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def snapshot(self):
        with self.lock:
            return {"state": self.state, "message": self.message,
                    "phase_done": self.phase_done, "phase_total": self.phase_total,
                    "record_id": self.record_id, "error": self.error,
                    "started_at": self.started_at}


JOB = CompareJob()
RESJOB = CompareJob()  # 资源检测批量任务（独立于比对任务）


def run_rescheck_batch(record_id):
    RESJOB.set(state="running", message="初始化…", phase_done=0, phase_total=0,
               record_id=record_id, error=None, started_at=time.time())
    RESJOB.cancel_event.clear()
    try:
        cfg = get_config()
        proxy = resolve_proxy(cfg)
        if proxy is None:
            raise RuntimeError("无法连接：未找到可用代理")
        rec = load_record(record_id)
        # 只检测未下载和收藏的作品：已下载的不用再找资源，忽略的不必查
        items = [it for it in rec.get("items", [])
                 if not it.get("ignored")
                 and (it.get("favorited") or it.get("status") == "missing")]
        RESJOB.set(phase_total=len(items))
        for i, it in enumerate(items):
            if RESJOB.cancel_event.is_set():
                RESJOB.set(state="cancelled", message="已取消")
                return
            RESJOB.set(message="检测资源 %d/%d：%s" % (i + 1, len(items), it.get("rj")),
                       phase_done=i)
            try:
                check_item_resources(it, proxy)
            except Exception as e:
                sys.stderr.write("rescheck %s failed: %s\n" % (it.get("rj"), e))
        RESJOB.set(state="done", phase_done=len(items), message="资源检测完成")
    except Exception as e:
        RESJOB.set(state="error", error=str(e), message="出错：" + str(e))


def section_from_url(url):
    m = re.match(r"https?://[^/]+/([a-z_]+)/", url)
    return m.group(1) if m else "maniax"


def work_url(section, rj):
    return "https://www.dlsite.com/%s/work/=/product_id/%s.html" % (section, rj)


def run_compare(url, max_pages, record_id=None, record_name=None):
    JOB.set(state="running", message="初始化…", phase_done=0, phase_total=0,
            record_id=record_id, error=None, started_at=time.time())
    JOB.cancel_event.clear()
    try:
        cfg = get_config()
        proxy = resolve_proxy(cfg)
        if proxy is None:
            raise RuntimeError("无法连接 DLsite：未找到可用代理，也未直连成功。"
                               "请确认代理软件已开启，或在设置里手动填写代理地址。")
        proxy_label = "直连" if proxy == "none" else proxy

        # 每次比对都先重新扫描本地目录，保证新下载的作品被识别
        JOB.set(message="扫描本地目录…")
        idx = rescan_local()
        local_rj = set(idx.get("rj_map", {}).keys())
        rj_map = idx.get("rj_map", {})

        # 自动增量更新翻译站 RJ 别名表（只抓取新增的中文版 RJ，已有对照不重复抓）
        try:
            new_a, total_a, checked_a = build_aliases(
                proxy,
                lambda d, t: JOB.set(phase_done=d, phase_total=t,
                                     message="更新翻译版 RJ 对照 %d/%d…" % (d, t)))
            if new_a:
                sys.stderr.write("alias: +%d (total %d)\n" % (new_a, total_a))
        except Exception as e:
            # 别名更新失败不阻断比对，沿用已有别名表
            sys.stderr.write("alias build failed: %s\n" % e)

        # 老记录：保留名称、创建时间；忽略/收藏列表为全局共享
        old = load_record(record_id) if record_id else None
        gi = load_global_ignore()
        ignored = set(gi.get("ignored", []))
        ignored_makers = set(gi.get("ignored_makers", []))
        favs = set(load_favorites().get("favorites", []))
        alias_map = load_alias()
        # 反向索引：主站 RJ -> [本地翻译站 RJ]
        alias_to_local = {}
        for a, t in alias_map.items():
            if a in local_rj:
                alias_to_local.setdefault(t, []).append(a)
        if old:
            record_name = old.get("name") or record_name

        section = section_from_url(url)
        base, qs = normalize_search_url(url)

        # 第 1 页
        JOB.set(message="抓取搜索结果第 1 页（经 %s）…" % proxy_label)
        html1 = fetch(make_page_url(base, qs, 1), proxy)
        items, last_page = parse_search_page(html1)
        if not items:
            raise RuntimeError("第 1 页没有解析到任何作品，请检查搜索 URL 是否正确。")
        total_pages = last_page if max_pages <= 0 else min(last_page, max_pages)
        JOB.set(phase_done=1, phase_total=total_pages,
                message="共 %d 页，正在翻页抓取…" % total_pages)

        # 后续页
        seen = {it["rj"] for it in items}
        all_items = list(items)
        for p in range(2, total_pages + 1):
            if JOB.cancel_event.is_set():
                JOB.set(state="cancelled", message="已取消")
                return
            JOB.set(message="抓取搜索结果 %d/%d 页…" % (p, total_pages),
                    phase_done=p)
            try:
                h = fetch(make_page_url(base, qs, p), proxy)
                its, _ = parse_search_page(h)
                for it in its:
                    if it["rj"] not in seen:
                        seen.add(it["rj"])
                        all_items.append(it)
            except Exception as e:
                sys.stderr.write("page %d failed: %s\n" % (p, e))
            time.sleep(0.3)

        # 初步比对
        for it in all_items:
            it["url"] = work_url(section, it["rj"])
            it["editions"] = None
            it["ignored"] = it["rj"] in ignored or bool(
                it.get("maker") and it["maker"] in ignored_makers)
            it["favorited"] = it["rj"] in favs
            if it["rj"] in local_rj:
                it["status"] = "owned"
                it["matched"] = [it["rj"]]
                it["local_paths"] = rj_map.get(it["rj"], [])[:1]
            else:
                it["status"] = "unknown"
                it["matched"] = []
                it["local_paths"] = []

        candidates = [it for it in all_items if it["status"] == "unknown"]
        cache = get_editions_cached()
        dirty = False

        def fetch_editions(it):
            rj = it["rj"]
            if rj in cache:
                return rj, cache[rj]
            h = fetch(work_url(section, rj), proxy)
            eds = parse_editions(h, rj)
            return rj, eds

        if candidates:
            JOB.set(phase_done=0, phase_total=len(candidates),
                    message="检查 %d 个疑似未下载作品的多语言版本…" % len(candidates))
            done = 0
            conc = int(cfg.get("concurrency", 4) or 4)
            with ThreadPoolExecutor(max_workers=conc) as ex:
                futs = {ex.submit(fetch_editions, it): it for it in candidates}
                for fut in as_completed(futs):
                    if JOB.cancel_event.is_set():
                        JOB.set(state="cancelled", message="已取消")
                        if dirty:
                            save_editions_cache(cache)
                        return
                    it = futs[fut]
                    try:
                        rj, eds = fut.result()
                        if rj not in cache:
                            cache[rj] = eds
                            dirty = True
                        it["editions"] = eds
                    except Exception as e:
                        it["status"] = "check_failed"
                        it["error_msg"] = str(e)
                    done += 1
                    JOB.set(phase_done=done,
                            message="检查多语言版本 %d/%d…" % (done, len(candidates)))
            if dirty:
                save_editions_cache(cache)

        # 作品信息批量查询：对搜索结果的全部作品（含各语言版本 RJ）查询
        # translation_info（翻译站子版本关系，并入别名表参与判定）和 regist_date（发售日期）
        tinfo = load_tinfo()
        try:
            all_rjs = []
            for it in all_items:
                all_rjs.append(it["rj"])
                for e in (it.get("editions") or []):
                    all_rjs.append(e["workno"])
            new_q = fetch_translate_info(
                all_rjs, proxy,
                lambda d, t: JOB.set(phase_done=d, phase_total=t,
                                     message="查询作品信息（翻译版/发售日）%d/%d…" % (d, t)),
                cancel_event=JOB.cancel_event)
            if JOB.cancel_event.is_set():
                JOB.set(state="cancelled", message="已取消")
                return
            if new_q:
                sys.stderr.write("tinfo: %d queried\n" % new_q)
                # 别名表已扩充，重建反向索引
                alias_map = load_alias()
                alias_to_local = {}
                for a, t in alias_map.items():
                    if a in local_rj:
                        alias_to_local.setdefault(t, []).append(a)
            # 第二遍：补齐翻译版本的原始版本 RJ（用于显示最初发售时间）
            tinfo = load_tinfo()
            orig_rjs = []
            for rj in dict.fromkeys(all_rjs):
                o = (tinfo.get(rj) or {}).get("o")
                if o and o not in tinfo:
                    orig_rjs.append(o)
            if orig_rjs:
                fetch_translate_info(
                    orig_rjs, proxy,
                    lambda d, t: JOB.set(phase_done=d, phase_total=t,
                                         message="查询原始版本发售时间 %d/%d…" % (d, t)),
                    cancel_event=JOB.cancel_event)
                tinfo = load_tinfo()
        except Exception as e:
            # 作品信息查询失败不阻断比对，沿用已有别名表
            sys.stderr.write("tinfo fetch failed: %s\n" % e)

        # 发售时间：翻译版本显示原始版本的最初发售日
        for it in all_items:
            it["release_date"] = release_date_of(it["rj"], tinfo)

        # 统一判定：自身 RJ + 各语言版本 RJ + 翻译站子版本 RJ（别名表）任一命中本地即已下载
        for it in all_items:
            if it["status"] == "check_failed":
                continue
            matched = []
            if it["rj"] in local_rj:
                matched.append(it["rj"])
            for e in (it.get("editions") or []):
                if e["workno"] in local_rj and e["workno"] not in matched:
                    matched.append(e["workno"])
                for a in alias_to_local.get(e["workno"], []):
                    if a not in matched:
                        matched.append(a)
            for a in alias_to_local.get(it["rj"], []):
                if a not in matched:
                    matched.append(a)
            t0 = alias_map.get(it["rj"])
            if t0 and t0 in local_rj and t0 not in matched:
                matched.append(t0)
            if matched:
                it["status"] = "owned"
                it["matched"] = matched
                it["local_paths"] = rj_map.get(matched[0], [])[:1]
            else:
                it["status"] = "missing"
                it["matched"] = []
                it["local_paths"] = []

        owned = sum(1 for it in all_items if it["status"] == "owned")
        missing = sum(1 for it in all_items
                      if it["status"] == "missing" and not it["ignored"] and not it["favorited"])
        ignored_n = sum(1 for it in all_items if it["ignored"])
        fav_n = sum(1 for it in all_items if it["favorited"] and not it["ignored"])
        failed = sum(1 for it in all_items if it["status"] == "check_failed")
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        summary = {"total": len(all_items), "owned": owned,
                   "missing": missing, "ignored": ignored_n, "favorited": fav_n,
                   "check_failed": failed,
                   "pages": total_pages, "url": url, "finished_at": now}
        if not record_id:
            record_id = "r" + time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        rec = {"id": record_id,
               "name": record_name or guess_record_name(url),
               "url": url, "max_pages": max_pages,
               "created_at": (old or {}).get("created_at") or now,
               "updated_at": now,
               "ignored": sorted(ignored),
               "ignored_makers": sorted(ignored_makers),
               "favorites": sorted(favs),
               "summary": summary,
               "items": all_items}
        apply_ignores(rec)  # 统一收尾：含已下载自动脱离收藏、统计口径一致
        save_record(rec)
        JOB.set(state="done",
                message="完成：共 %d 部，已下载 %d，未下载 %d，收藏 %d，忽略 %d" % (
                    len(all_items), owned, missing, fav_n, ignored_n),
                record_id=record_id)
    except Exception as e:
        JOB.set(state="error", error=str(e), message="出错：" + str(e))


def collect_local_rows(network=True):
    """汇总本地库每个 RJ 的信息（导出 CSV 与本地库列表共用）。
    返回 (rows, idx)。rows 字段：rj/title/maker/release_date/lang/url/path/mtime
    （mtime = 作品文件夹的修改时间，即"下载时间"）。
    network=False 时只用本地缓存（社团/发售日可能不全），速度快，适合界面列表。"""
    idx = rescan_local()
    rj_map = idx.get("rj_map", {})
    # 从各比对记录收集标题/社团
    info = {}
    os.makedirs(RESULTS_DIR, exist_ok=True)
    for fn in os.listdir(RESULTS_DIR):
        if not fn.endswith(".json"):
            continue
        rec = load_json(os.path.join(RESULTS_DIR, fn), None) or {}
        for it in rec.get("items", []):
            r = it.get("rj")
            if not r:
                continue
            cur = info.setdefault(r, {"title": "", "maker": ""})
            if not cur["title"] and it.get("title"):
                cur["title"] = it["title"]
            if not cur["maker"] and it.get("maker"):
                cur["maker"] = it["maker"]
    # 发售日期/社团ID：批量接口增量补查（已缓存的不重复请求）
    cfg = get_config()
    proxy = resolve_proxy(cfg) if network else None
    if proxy:
        try:
            fetch_translate_info(list(rj_map.keys()), proxy)
        except Exception as e:
            sys.stderr.write("export tinfo failed: %s\n" % e)
    tinfo = load_tinfo()
    # 社团名：记录里查不到的，用 tinfo 的 maker_id 抓社团主页补全
    maker_ids = set()
    for rj in rj_map:
        if info.get(rj, {}).get("maker"):
            continue
        mid = (tinfo.get(rj) or {}).get("m")
        if mid:
            maker_ids.add(mid)
    if proxy and maker_ids:
        try:
            fetch_maker_names(sorted(maker_ids), proxy)
        except Exception as e:
            sys.stderr.write("export makers failed: %s\n" % e)
    maker_cache = load_maker_cache()
    # 二级回退：翻译者账号等没有公开社团主页的，从作品页提取（按 RJ 缓存）
    no_maker_rjs = [
        rj for rj in rj_map
        if not info.get(rj, {}).get("maker")
        and not maker_cache.get((tinfo.get(rj) or {}).get("m") or "")
        and rj not in maker_cache]
    if proxy and no_maker_rjs:
        def work_maker(rj):
            try:
                h = fetch(work_url("maniax", rj), proxy)
                m = MAKER_RE.search(h)
                return rj, (html_mod.unescape(m.group(1)).strip() if m else "")
            except Exception:
                return rj, ""
        with ThreadPoolExecutor(max_workers=4) as ex:
            for rj, name in ex.map(work_maker, no_maker_rjs):
                maker_cache[rj] = name
        save_maker_cache(maker_cache)
    alias = load_alias()
    eds_cache = get_editions_cached()
    ratings = load_ratings()
    ratings_manual = load_ratings_manual()

    def folder_title(p):
        # 从文件夹名提取标题：去掉 RJ 号；裸 RJ 子文件夹则向上找父目录名
        cur = p
        for _ in range(3):
            name = os.path.basename(cur)
            name = re.sub(r"\[?RJ\d{6,9}\]?", "", name, flags=re.I)
            name = name.strip(" _‐-")
            if name:
                return name
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
        return ""

    rows = []
    for rj in sorted(rj_map.keys(), key=lambda r: int(r[2:])):
        paths = rj_map[rj]
        title = info.get(rj, {}).get("title") or folder_title(paths[0])
        maker = (info.get(rj, {}).get("maker")
                 or maker_cache.get((tinfo.get(rj) or {}).get("m") or "")
                 or maker_cache.get(rj, ""))
        date = release_date_of(rj, tinfo)
        eds = eds_cache.get(rj) or eds_cache.get(alias.get(rj) or "")
        lang = ""
        if eds:
            lang = "、".join(
                ((e.get("display_label") + " ") if e.get("display_label") else "")
                + e["workno"] for e in eds)
        try:
            mt = time.strftime("%Y-%m-%d %H:%M",
                               time.localtime(os.path.getmtime(paths[0])))
        except OSError:
            mt = ""
        rows.append({"rj": rj, "title": title, "maker": maker,
                     "release_date": date, "lang": lang,
                     "url": work_url("maniax", rj), "path": paths[0],
                     "mtime": mt, "rating": ratings.get(rj, 0) or 0,
                     "rating_manual": rj in ratings_manual})
    return rows, idx


# ---------------- HTTP 服务 ----------------

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[web] " + fmt % args + "\n")

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            fp = os.path.join(WEB_DIR, "index.html")
            with open(fp, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            idx = get_local_index()
            self._send_json({
                "config": get_config(),
                "local": ({"count": idx["count"], "scanned_at": idx["scanned_at"],
                           "dirs": idx.get("dirs", [])} if idx else None),
                "alias_count": len(load_alias()),
                "job": JOB.snapshot(),
                "records": list_records(),
            })
        elif path == "/api/job":
            self._send_json(JOB.snapshot())
        elif path == "/api/resjob":
            self._send_json(RESJOB.snapshot())
        elif path == "/api/result":
            qs = urllib.parse.parse_qs(parsed.query)
            rid = (qs.get("id") or [""])[0]
            rec = load_record(rid)
            if rec:
                # 实时套用全局忽略/收藏，保证老记录也反映其他记录里的操作
                apply_ignores(rec)
                # 老记录回填发售时间（只读本地缓存，不发网络请求）
                tinfo = load_tinfo()
                for it in rec.get("items", []):
                    if not it.get("release_date"):
                        d = release_date_of(it["rj"], tinfo)
                        if d:
                            it["release_date"] = d
                save_record(rec)
                # 别名表注入响应（不落盘），用于语言版本列标注翻译站 RJ
                rec_resp = dict(rec)
                alias = load_alias()
                rec_resp["alias"] = alias
                # 本地库个人评级注入响应（不落盘），用于已下载条目显示星级
                ratings = load_ratings()
                # 资源站检测结果注入（不落盘）；tinfo/alias 每次请求只读一次，不能逐作品重复读 JSON
                rescache = load_rescache()
                if ratings or rescache:
                    ratings_manual = load_ratings_manual()
                    items2 = []
                    for it in rec.get("items", []):
                        d = dict(it, rating=ratings.get(it.get("rj"), 0),
                                 rating_manual=it.get("rj") in ratings_manual)
                        res = resources_from_cache(it, rescache, tinfo=tinfo, alias=alias)
                        if res:
                            d["resources"] = res
                        items2.append(d)
                    rec_resp["items"] = items2
                self._send_json(rec_resp)
            else:
                self._send_json({"error": "记录不存在"}, 404)
        elif path == "/api/global_lists":
            gi = load_global_ignore()
            fav = load_favorites()
            # 从各记录里查标题和社团，用于管理页展示
            info = {}
            os.makedirs(RESULTS_DIR, exist_ok=True)
            for fn in os.listdir(RESULTS_DIR):
                if not fn.endswith(".json"):
                    continue
                rec = load_json(os.path.join(RESULTS_DIR, fn), None) or {}
                for it in rec.get("items", []):
                    rj = it.get("rj")
                    if rj and rj not in info:
                        info[rj] = {"title": it.get("title", ""),
                                    "maker": it.get("maker", "")}

            def with_info(rjs):
                return [dict({"rj": rj}, **info.get(rj, {"title": "", "maker": ""}))
                        for rj in rjs]

            self._send_json({
                "ignored": with_info(gi.get("ignored", [])),
                "ignored_makers": gi.get("ignored_makers", []),
                "favorites": with_info(fav.get("favorites", [])),
            })
        elif path == "/api/local_library":
            # 本地库列表（界面用）：默认只用缓存秒开；?full=1 联网补全社团/发售日。
            # 发现从未查询过的 RJ（新下载的作品）时自动联网补一次社团/发售日/评分，
            # 否则新作的信息会一直空白
            qs = urllib.parse.parse_qs(parsed.query)
            full = (qs.get("full") or [""])[0] == "1"
            if not full:
                idx0 = rescan_local()
                tinfo0 = load_tinfo()
                new_rjs = [rj for rj in idx0.get("rj_map", {})
                           if rj not in tinfo0]
                if new_rjs:
                    cfg = get_config()
                    proxy = resolve_proxy(cfg)
                    if proxy:
                        try:
                            fetch_translate_info(new_rjs, proxy)
                        except Exception as e:
                            sys.stderr.write("auto tinfo failed: %s\n" % e)
                        # 顺带把新作的 DLsite 评分也填上（不碰手动评级和已有评分）
                        tinfo1 = load_tinfo()
                        ratings = load_ratings()
                        manual = load_ratings_manual()
                        dirty = False
                        for rj in new_rjs:
                            if rj in ratings or rj in manual:
                                continue
                            s = (tinfo1.get(rj) or {}).get("r")
                            if s:
                                ratings[rj] = s
                                dirty = True
                        if dirty:
                            save_ratings(ratings)
                        full = True  # 让 collect_local_rows 补社团名等
            rows, idx = collect_local_rows(network=full)
            self._send_json({"rows": rows, "count": len(rows),
                             "scanned_at": idx.get("scanned_at", "")})
        elif path == "/api/export_local":
            # 导出本地库 CSV：增量扫描后，为每个本地 RJ 汇总
            # 标题（记录/文件夹名）、社团（记录）、发售日（tinfo）、语言版本（editions 缓存）
            rows, idx = collect_local_rows(network=True)
            csv_rows = [["RJ号", "标题", "社团", "发售日", "语言版本", "链接", "本地目录", "评级"]]
            for r in rows:
                csv_rows.append([r["rj"], r["title"], r["maker"],
                                 r["release_date"], r["lang"], r["url"],
                                 os.path.dirname(r["path"]),
                                 r["rating"] or ""])
            csv = "﻿" + "\r\n".join(
                ",".join('"' + str(c or "").replace('"', '""') + '"' for c in r)
                for r in csv_rows)
            body = csv.encode("utf-8")
            fname = "dlsite_local_%s.csv" % time.strftime("%Y%m%d_%H%M")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition",
                             "attachment; filename=\"%s\"" % fname)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._read_body()
        if path == "/api/scan":
            try:
                idx = rescan_local()
                resp = {"ok": True, "count": idx["count"],
                        "scanned_at": idx["scanned_at"]}
                # 扫描后自动增量建立翻译站 RJ 对照（网络失败不影响扫描结果）
                try:
                    proxy = resolve_proxy(get_config())
                    if proxy:
                        new_a, total_a, checked_a = build_aliases(proxy)
                        resp.update({"alias_new": new_a, "alias_total": total_a})
                except Exception as e:
                    sys.stderr.write("alias build failed: %s\n" % e)
                self._send_json(resp)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/build_alias":
            try:
                cfg = get_config()
                proxy = resolve_proxy(cfg)
                if proxy is None:
                    self._send_json({"ok": False,
                                     "error": "无法连接 DLsite，请检查代理"}, 500)
                    return
                new_n, total, checked = build_aliases(proxy)
                self._send_json({"ok": True, "new": new_n,
                                 "total": total, "checked": checked})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/config":
            cfg = get_config()
            if "local_dirs" in body and isinstance(body["local_dirs"], list):
                cfg["local_dirs"] = [d for d in body["local_dirs"] if d.strip()]
            if "proxy" in body:
                cfg["proxy"] = body["proxy"]
                cfg.pop("proxy_detected", None)
            if "cookie" in body:
                cfg["cookie"] = str(body["cookie"]).strip()
            if "concurrency" in body:
                try:
                    cfg["concurrency"] = max(1, min(8, int(body["concurrency"])))
                except Exception:
                    pass
            save_config(cfg)
            self._send_json({"ok": True, "config": cfg})
        elif path == "/api/compare":
            if JOB.state == "running":
                self._send_json({"ok": False, "error": "已有任务在运行中"}, 409)
                return
            url = (body.get("url") or "").strip()
            if not url.startswith("http"):
                self._send_json({"ok": False, "error": "请输入有效的 DLsite 搜索结果 URL"}, 400)
                return
            try:
                max_pages = int(body.get("max_pages") or 0)
            except Exception:
                max_pages = 0
            t = threading.Thread(target=run_compare, args=(url, max_pages), daemon=True)
            t.start()
            self._send_json({"ok": True})
        elif path == "/api/refresh":
            if JOB.state == "running":
                self._send_json({"ok": False, "error": "已有任务在运行中"}, 409)
                return
            rid = (body.get("id") or "").strip()
            rec = load_record(rid)
            if not rec:
                self._send_json({"ok": False, "error": "记录不存在"}, 404)
                return
            t = threading.Thread(target=run_compare,
                                 args=(rec["url"], int(rec.get("max_pages") or 0)),
                                 kwargs={"record_id": rid}, daemon=True)
            t.start()
            self._send_json({"ok": True})
        elif path == "/api/open_folder":
            # 打开本地文件夹：仅允许打开已配置本地库目录范围内的路径
            p = (body.get("path") or "").strip()
            dirs = [os.path.normcase(os.path.normpath(d))
                    for d in get_config().get("local_dirs", [])]
            norm = os.path.normcase(os.path.normpath(p)) if p else ""
            if (not p or not os.path.isdir(p)
                    or not any(norm == d or norm.startswith(d + os.sep)
                               for d in dirs)):
                self._send_json({"ok": False, "error": "路径不在本地库目录内"}, 400)
                return
            subprocess.Popen(["explorer", norm])
            self._send_json({"ok": True})
        elif path == "/api/rescheck":
            # 单个作品的资源站检测（全部语言版本 RJ 都查）
            rid = (body.get("id") or "").strip()
            rj = (body.get("rj") or "").strip().upper()
            rec = load_record(rid)
            it = rec and next((x for x in rec.get("items", []) if x.get("rj") == rj), None)
            if not it:
                self._send_json({"ok": False, "error": "记录中没有该作品"}, 404)
                return
            proxy = resolve_proxy(get_config())
            if proxy is None:
                self._send_json({"ok": False, "error": "无法连接，请检查代理"}, 500)
                return
            force = bool(body.get("force"))
            out = check_item_resources(it, proxy, force=force)
            self._send_json({"ok": True, "resources": out})
        elif path == "/api/rescheck_batch":
            # 批量检测整条比对记录（后台任务，/api/resjob 轮询进度）
            rid = (body.get("id") or "").strip()
            if not load_record(rid):
                self._send_json({"ok": False, "error": "记录不存在"}, 404)
                return
            if RESJOB.snapshot()["state"] == "running":
                self._send_json({"ok": False, "error": "资源检测正在进行中"}, 400)
                return
            threading.Thread(target=run_rescheck_batch, args=(rid,), daemon=True).start()
            self._send_json({"ok": True})
        elif path == "/api/rescheck_cancel":
            RESJOB.cancel_event.set()
            self._send_json({"ok": True})
        elif path == "/api/recheck":
            # 单个作品重新检测：增量扫描本地库 + 该作品的版本组比对，比整体刷新快得多
            rid = (body.get("id") or "").strip()
            rj = (body.get("rj") or "").strip().upper()
            rec = load_record(rid)
            if not rec:
                self._send_json({"ok": False, "error": "记录不存在"}, 404)
                return
            it = next((x for x in rec.get("items", []) if x.get("rj") == rj), None)
            if not it:
                self._send_json({"ok": False, "error": "记录中没有该作品"}, 404)
                return
            cfg = get_config()
            proxy = resolve_proxy(cfg)
            if proxy is None:
                self._send_json({"ok": False,
                                 "error": "无法连接 DLsite，请检查代理"}, 500)
                return
            idx = rescan_local()
            local_rj = set(idx.get("rj_map", {}).keys())
            rj_map = idx.get("rj_map", {})
            try:
                build_aliases(proxy)  # 增量：新下载的中文版文件夹补建别名
            except Exception as e:
                sys.stderr.write("alias build failed: %s\n" % e)
            # 没有多语言数据就抓一次作品页
            if not it.get("editions"):
                try:
                    h = fetch(work_url(section_from_url(rec.get("url", "")), rj), proxy)
                    eds = parse_editions(h, rj)
                    it["editions"] = eds
                    cache = get_editions_cached()
                    if rj not in cache:
                        cache[rj] = eds
                        save_editions_cache(cache)
                except Exception as e:
                    it["status"] = "check_failed"
                    it["error_msg"] = str(e)
                    apply_ignores(rec)
                    save_record(rec)
                    self._send_json({"ok": True, "item": it,
                                     "summary": rec["summary"],
                                     "local_count": idx["count"]})
                    return
            # 版本组的翻译站关系/发售日期（基本都有缓存，最多一个批次）
            try:
                grp = [rj] + [e["workno"] for e in (it.get("editions") or [])]
                fetch_translate_info(grp, proxy)
                it["release_date"] = release_date_of(rj, load_tinfo())
            except Exception as e:
                sys.stderr.write("tinfo fetch failed: %s\n" % e)
            # 与 run_compare 相同的统一判定
            alias_map = load_alias()
            alias_to_local = {}
            for a, t in alias_map.items():
                if a in local_rj:
                    alias_to_local.setdefault(t, []).append(a)
            matched = []
            if rj in local_rj:
                matched.append(rj)
            for e in (it.get("editions") or []):
                if e["workno"] in local_rj and e["workno"] not in matched:
                    matched.append(e["workno"])
                for a in alias_to_local.get(e["workno"], []):
                    if a not in matched:
                        matched.append(a)
            for a in alias_to_local.get(rj, []):
                if a not in matched:
                    matched.append(a)
            t0 = alias_map.get(rj)
            if t0 and t0 in local_rj and t0 not in matched:
                matched.append(t0)
            if matched:
                it["status"] = "owned"
                it["matched"] = matched
                it["local_paths"] = rj_map.get(matched[0], [])[:1]
            else:
                it["status"] = "missing"
                it["matched"] = []
                it["local_paths"] = []
            apply_ignores(rec)
            save_record(rec)
            self._send_json({"ok": True, "item": it, "summary": rec["summary"],
                             "ignored": rec.get("ignored", []),
                             "ignored_makers": rec.get("ignored_makers", []),
                             "favorites": rec.get("favorites", []),
                             "local_count": idx["count"]})
        elif path == "/api/rename":
            rid = (body.get("id") or "").strip()
            name = (body.get("name") or "").strip()
            rec = load_record(rid)
            if not rec:
                self._send_json({"ok": False, "error": "记录不存在"}, 404)
                return
            if not name:
                self._send_json({"ok": False, "error": "名称不能为空"}, 400)
                return
            rec["name"] = name[:80]
            save_record(rec)
            self._send_json({"ok": True, "name": rec["name"]})
        elif path == "/api/ignore":
            rid = (body.get("id") or "").strip()
            rj = (body.get("rj") or "").strip().upper()
            flag = bool(body.get("ignored"))
            rec = load_record(rid)
            if not rec:
                self._send_json({"ok": False, "error": "记录不存在"}, 404)
                return
            gi = load_global_ignore()
            ignored = set(gi.get("ignored", []))
            if flag:
                ignored.add(rj)
            else:
                ignored.discard(rj)
            gi["ignored"] = sorted(ignored)
            save_global_ignore(gi)
            reapply_all_records()
            rec = load_record(rid)
            self._send_json({"ok": True, "summary": rec["summary"],
                             "ignored": gi["ignored"],
                             "ignored_makers": gi["ignored_makers"]})
        elif path == "/api/ignore_maker":
            rid = (body.get("id") or "").strip()
            maker = (body.get("maker") or "").strip()
            flag = bool(body.get("ignored"))
            rec = load_record(rid)
            if not rec:
                self._send_json({"ok": False, "error": "记录不存在"}, 404)
                return
            if not maker:
                self._send_json({"ok": False, "error": "社团名为空"}, 400)
                return
            gi = load_global_ignore()
            makers = set(gi.get("ignored_makers", []))
            if flag:
                makers.add(maker)
            else:
                makers.discard(maker)
            gi["ignored_makers"] = sorted(makers)
            save_global_ignore(gi)
            reapply_all_records()
            rec = load_record(rid)
            self._send_json({"ok": True, "summary": rec["summary"],
                             "ignored": gi["ignored"],
                             "ignored_makers": gi["ignored_makers"]})
        elif path == "/api/rate":
            # 手动评级 {rj, rating: 0.00-5.00 两位小数，0 = 清除}；
            # 手动打过的记入 ratings_manual，导入时永不覆盖
            rj = (body.get("rj") or "").strip().upper()
            try:
                rating = round(float(body.get("rating")), 2)
            except (TypeError, ValueError):
                rating = -1
            if not rj or not (0 <= rating <= 5):
                self._send_json({"ok": False, "error": "参数无效"}, 400)
                return
            ratings = load_ratings()
            manual = load_ratings_manual()
            if rating == 0:
                ratings.pop(rj, None)
                manual.discard(rj)
            else:
                ratings[rj] = rating
                manual.add(rj)
            save_ratings(ratings)
            save_ratings_manual(manual)
            self._send_json({"ok": True, "rating": rating})
        elif path == "/api/import_ratings":
            # 从 DLsite 批量接口（rate_average_2dp）导入本地库作品的官方评分。
            # DLsite 评分会变：导入来的条目每次刷新为新分数；
            # ratings_manual 里手动打过分的一律跳过，永不被覆盖。
            cfg = get_config()
            proxy = resolve_proxy(cfg)
            if proxy is None:
                self._send_json({"ok": False,
                                 "error": "无法连接 DLsite：未找到可用代理"}, 400)
                return
            rows, _idx = collect_local_rows(network=False)
            rjs = [r["rj"] for r in rows]
            # 强制重查评分：清掉本地 RJ 缓存里的 "r" 字段，
            # 否则 tinfo 缓存命中会永远拿到旧分数
            tinfo = load_tinfo()
            for rj in rjs:
                e = tinfo.get(rj)
                if e is not None and "r" in e:
                    del e["r"]
            save_tinfo(tinfo)
            fetch_translate_info(rjs, proxy)  # 缺评分字段 "r" 的缓存条目会自动重查
            tinfo = load_tinfo()
            ratings = load_ratings()
            manual = load_ratings_manual()
            imported = updated = no_score = kept = 0
            for rj in rjs:
                if rj in manual:
                    kept += 1
                    continue
                score = (tinfo.get(rj) or {}).get("r")
                if score:
                    if rj in ratings:
                        if ratings[rj] != score:
                            ratings[rj] = score
                            updated += 1
                    else:
                        ratings[rj] = score
                        imported += 1
                elif rj not in ratings:
                    no_score += 1
            # 第二遍：无评分作品沿用其语言版本组的评分
            # （常见于翻译站 RJ 无人评分，但日语原版/主站版本有评分）
            alias = load_alias()
            related = {}
            need_fetch = set()
            for rj in rjs:
                if rj in ratings or rj in manual:
                    continue
                e = tinfo.get(rj) or {}
                group = []
                if e.get("o"):
                    group.append(e["o"])  # 原始版本（最早发售的语言版本）
                if e.get("p"):
                    group.append(e["p"])  # 主站版本
                group += e.get("c") or []  # 翻译站子版本
                a = alias.get(rj)
                if a:
                    group.append(a)  # 别名表：翻译站 RJ → 主站 RJ
                group = [g for g in dict.fromkeys(group) if g and g != rj]
                if group:
                    related[rj] = group
                    for g in group:
                        if "r" not in (tinfo.get(g) or {}):
                            need_fetch.add(g)
            if need_fetch:
                fetch_translate_info(sorted(need_fetch), proxy)
                tinfo = load_tinfo()
            inherited = 0
            for rj, group in related.items():
                for g in group:
                    s = (tinfo.get(g) or {}).get("r")
                    if s:
                        ratings[rj] = s
                        no_score -= 1
                        inherited += 1
                        break
            save_ratings(ratings)
            self._send_json({"ok": True, "total": len(rjs),
                             "imported": imported, "updated": updated,
                             "no_score": no_score, "inherited": inherited,
                             "kept": kept})
        elif path == "/api/favorite":
            rid = (body.get("id") or "").strip()
            rj = (body.get("rj") or "").strip().upper()
            flag = bool(body.get("favorited"))
            rec = load_record(rid)
            if not rec:
                self._send_json({"ok": False, "error": "记录不存在"}, 404)
                return
            fav = load_favorites()
            favs = set(fav.get("favorites", []))
            if flag:
                favs.add(rj)
            else:
                favs.discard(rj)
            fav["favorites"] = sorted(favs)
            save_favorites(fav)
            reapply_all_records()
            rec = load_record(rid)
            self._send_json({"ok": True, "summary": rec["summary"],
                             "favorites": fav["favorites"]})
        elif path == "/api/restore_batch":
            typ = body.get("type")
            keys = set(body.get("keys") or [])
            if typ == "ignored":
                gi = load_global_ignore()
                gi["ignored"] = sorted(set(gi.get("ignored", [])) - keys)
                save_global_ignore(gi)
            elif typ == "ignored_makers":
                gi = load_global_ignore()
                gi["ignored_makers"] = sorted(set(gi.get("ignored_makers", [])) - keys)
                save_global_ignore(gi)
            elif typ == "favorites":
                fav = load_favorites()
                fav["favorites"] = sorted(set(fav.get("favorites", [])) - keys)
                save_favorites(fav)
            else:
                self._send_json({"ok": False, "error": "未知类型"}, 400)
                return
            # 重新套用所有记录，保证列表统计立即更新
            reapply_all_records()
            self._send_json({"ok": True})
        elif path == "/api/delete":
            rid = (body.get("id") or "").strip()
            if delete_record(rid):
                self._send_json({"ok": True})
            else:
                self._send_json({"ok": False, "error": "记录不存在"}, 404)
        elif path == "/api/cancel":
            JOB.cancel_event.set()
            self._send_json({"ok": True})
        else:
            self._send_json({"error": "not found"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", "-p", type=int, default=None)
    ap.add_argument("--host", default="127.0.0.1")
    args, _ = ap.parse_known_args()
    port = args.port or int(os.environ.get("PORT", "7100"))
    get_config()
    server = ThreadingHTTPServer((args.host, port), Handler)
    print("DLsite 收藏比对工具已启动: http://%s:%d/" % (args.host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
