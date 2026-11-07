# -*- coding: utf-8 -*-
"""资源站检测：alist-public / asmr.one / anime-sharing。
check_site(site, rj, proxy) 返回 {"f": True找到/False没有/None失败或需人工, "u": 结果页链接}。
结果缓存由 server.py 管理（resource_cache.json），本模块只管网络查询。"""
import http.cookiejar
import json
import re
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

SITES = [
    {"key": "alist", "name": "alist-public",
     "search": "https://alist-public.imoutoheaven.org/"},
    {"key": "animesharing", "name": "anime-sharing",
     "search": "https://www.anime-sharing.com/search/?q={rj}"},
    {"key": "asmrone", "name": "asmr.one",
     "search": "https://www.asmr.one/works?keyword={rj}"},
]
SITE_KEYS = [s["key"] for s in SITES]


def search_url(site_key, rj):
    for s in SITES:
        if s["key"] == site_key:
            return s["search"].replace("{rj}", rj) if "{rj}" in s["search"] else s["search"]
    return ""


def make_opener(proxy, cookiejar=None):
    handlers = []
    if proxy and proxy != "none":
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    if cookiejar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(cookiejar))
    return urllib.request.build_opener(*handlers)


def http_get(opener, url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with opener.open(req, timeout=timeout) as resp:
        return resp.geturl(), resp.read().decode("utf-8", errors="replace")


def rj_hit(text, rj):
    """精确匹配 RJ 号（后面不能再跟数字，防止 RJ285088 命中 RJ2850887 之类）"""
    return re.search(re.escape(rj.upper()) + r"(?![0-9])", (text or "").upper()) is not None


def check_alist(rj, proxy):
    url = "https://alist-public.imoutoheaven.org/api/fs/search"
    body = json.dumps({"parent": "/", "keywords": rj, "scope": 0,
                       "page": 1, "per_page": 50}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json", "User-Agent": UA})
    with make_opener(proxy).open(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8", errors="replace"))
    content = (data.get("data") or {}).get("content") or []
    for it in content:
        if rj_hit(it.get("name", ""), rj):
            # 链接到命中文件所在目录，用户打开就能看到资源。
            # 注意：AList 前端会自己补挂载前缀 /guest，
            # 链接里再带一次会变成 /guest/guest/... 报 storage not found
            parent = it.get("parent") or "/"
            if parent.startswith("/guest/"):
                parent = parent[len("/guest"):]
            elif parent == "/guest":
                parent = "/"
            return {"f": True,
                    "u": "https://alist-public.imoutoheaven.org"
                         + urllib.parse.quote(parent)}
    return {"f": False, "u": search_url("alist", rj)}


def check_asmrone(rj, proxy):
    # asmr.one 前端实际使用 /api/search/{keyword}；/api/works 会忽略 keyword，
    # 只返回普通作品列表，不能用于搜索。保留 source_id 精确匹配，避免相邻 RJ 误判。
    url = ("https://api.asmr.one/api/search/" + urllib.parse.quote(rj)
           + "?page=1&pageSize=20&includeTranslationWorks=true")
    _u, text = http_get(make_opener(proxy), url)
    data = json.loads(text)
    for w in data.get("works") or []:
        if (w.get("source_id") or "").upper() == rj.upper():
            return {"f": True, "u": search_url("asmrone", rj)}
    return {"f": False, "u": search_url("asmrone", rj)}


def check_animesharing(rj, proxy):
    """XenForo 游客搜索：取表单 token → POST 创建搜索 → 从 og:url 拿结果页 → 解析标题"""
    jar = http.cookiejar.CookieJar()
    opener = make_opener(proxy, jar)
    _u, form = http_get(opener, "https://www.anime-sharing.com/search/")
    m = re.search(r'name="_xfToken" value="([^"]+)"', form)
    if not m:
        raise RuntimeError("anime-sharing 搜索表单未取到 token")
    data = urllib.parse.urlencode({
        "keywords": rj, "_xfToken": m.group(1),
        "c[search_order]": "date",  # 无所谓排序，减少差异
    }).encode()
    req = urllib.request.Request(
        "https://www.anime-sharing.com/search/search", data=data,
        headers={"User-Agent": UA,
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with opener.open(req, timeout=30) as resp:
            final_url, page = resp.geturl(), resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # 创建搜索后 XenForo 可能回 400 错误页，但 og:url 里有结果地址
        page = e.read().decode("utf-8", errors="replace")
        final_url = ""
    result_url = final_url if "/search/" in final_url and re.search(r"/search/\d+/", final_url) else ""
    if not result_url:
        m2 = re.search(r'property="og:url" content="([^"]*/search/\d+/[^"]*)"', page)
        if m2:
            result_url = m2.group(1).replace("&amp;", "&")
    if not result_url:
        raise RuntimeError("anime-sharing 未返回搜索结果页")
    _u, results = http_get(opener, result_url)
    # 每个结果块：h3.contentRow-title 标题 + contentRow-minor 里 Forum: <a>版块</a>
    # 求资源专区 Voice Works Requests 的帖子不算资源（有些没加 [Request] 前缀）
    valid = 0
    for ch in re.split(r'<li class="block-row', results)[1:]:
        tm = re.search(r'<h3 class="contentRow-title">(.*?)</h3>', ch, re.S)
        if not tm:
            continue
        title = re.sub(r"<[^>]+>", " ", tm.group(1))
        fm = re.search(r'Forum:\s*<a href="([^"]+)"[^>]*>([^<]+)</a>', ch)
        forum = ((fm.group(1) + " " + fm.group(2)).lower()) if fm else ""
        if "voice works requests" in forum or "voice-works-requests" in forum:
            continue
        if "[request]" in title.lower():
            continue
        if rj_hit(title, rj):
            valid += 1
    if valid:
        return {"f": True, "u": result_url}
    if "Search results" in results:
        return {"f": False, "u": result_url}
    raise RuntimeError("anime-sharing 结果页解析失败")


CHECKERS = {
    "alist": check_alist,
    "animesharing": check_animesharing,
    "asmrone": check_asmrone,
}

# 搜索实现发生不兼容变化时递增，旧缓存会被自动忽略并在下次检测时重查。
CACHE_VERSION = {"asmrone": 2}


def cache_valid(site, entry):
    if not entry:
        return False
    return entry.get("v", 1) == CACHE_VERSION.get(site, 1)


def check_site(site, rj, proxy):
    try:
        result = CHECKERS[site](rj, proxy)
    except Exception:
        result = {"f": None, "u": search_url(site, rj)}
    if site in CACHE_VERSION:
        result["v"] = CACHE_VERSION[site]
    return result
