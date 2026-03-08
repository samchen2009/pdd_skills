"""
pdd_skills：拼多多定制能力（PDDAgent 继承 MobileAgent）。
- open_app / search：通过 PDDAgent 定制。
- dump_products / dump_store_page / parse_store_page_xml：解析商品与店铺首页。
- dump_store_page(store_keyword)：完整流程：打开搜索页 → 搜店铺 → 依次点商品进详情 → 找目标店铺进店 → 店内滑到底并采集 → 结构化可输出 CSV。

命令行入口见 pdd_skills_cli.py。
"""

import csv
import io
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from mobile_agent import MobileAgent

# 拼多多 Android 包名
PDD_PACKAGE = "com.xunmeng.pinduoduo"

def _log(msg: str) -> None:
    """流程日志，便于定位 dump_store_page 卡点。"""
    print(f"[pdd_store] {msg}", flush=True)

# 店铺首页结构（参考 store.xml）：
# - 顶部 [0~600]：店铺头像、店名、全店总售、好评/推荐/逛过、关注/客服、保障条、Tab（全部商品等）
# - 商品区：RecyclerView 内 ViewGroup 为商品卡，含 iv_image、tv_title、价格(¥+数字)、tv_sales(已抢x件)、标签
TITLE_RID = "com.xunmeng.pinduoduo:id/tv_title"
SALES_DESC = "tv_sales"
STORE_HEADER_Y_MAX = 600
PRODUCT_CARD_MIN_HEIGHT = 400
# 商品卡特征：海报图最小面积、卡片宽高范围
POSTER_MIN_AREA = 40000  # 200x200，海报图较大
PRODUCT_CARD_MAX_HEIGHT = 1200
PRODUCT_CARD_MIN_WIDTH = 200
PRODUCT_CARD_MAX_WIDTH = 600  # 单列卡片宽度（双列布局下每卡约 540）


def _u():
    """获取当前设备单例（mobile_agent UIAutomator）。"""
    from mobile_agent.uiautomator import UIAutomator
    return UIAutomator.get_instance()


# ----- PDDAgent：扩展类，定制 search/open，init() 返回类型仍为 MobileAgent -----

class PDDAgent(MobileAgent):
    """
    拼多多定制扩展：继承 MobileAgent，定制 search()（默认搜索按钮「搜索」）、open()（无参打开拼多多）。
    使用方式：ma = PDDAgent.init()  # 返回类型可注解为 MobileAgent，实际为 PDDAgent
    ui = ma.getUIAutomator()
    ma.open()           # 打开拼多多
    ma.search("3CE")    # 拼多多内搜索
    ui.click(...)
    """
    _instance: Optional["PDDAgent"] = None  # 子类自己的单例，与基类 _instance 分离

    @classmethod
    def init(cls, device: Optional[str] = None) -> MobileAgent:
        """连接设备并返回 PDDAgent 单例。返回类型为 MobileAgent（PDDAgent 是子类）。"""
        if cls._instance is not None:
            return cls._instance
        base = MobileAgent.init(device=device)
        cls._instance = cls(base.getUIAutomator())
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """清空 PDDAgent 单例。"""
        cls._instance = None

    def search(
        self,
        keyword: Optional[str] = None,
        *,
        input_resource_id: Optional[str] = None,
        search_button_text: Optional[str] = None,
        clear: bool = False,
        keyboard_done_text: Optional[str] = None,
        search_input: Optional[Dict[str, Any]] = None,
        search_button: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """拼多多定制：默认搜索按钮为「搜索」。"""
        return super().search(
            keyword,
            input_resource_id=input_resource_id,
            search_button_text=search_button_text or "搜索",
            clear=clear,
            keyboard_done_text=keyboard_done_text,
            search_input=search_input,
            search_button=search_button,
        )

    def open(
        self,
        app: str = PDD_PACKAGE,
        *,
        uri: Optional[str] = None,
        stop: bool = False,
        activity: Optional[str] = None,
    ) -> dict:
        """拼多多定制：无参时打开拼多多。"""
        return self.open_app(app, uri=uri, stop=stop, activity=activity)


def open_app(stop: bool = False) -> dict:
    """打开拼多多。内部使用 PDDAgent.init().open()。"""
    return PDDAgent.init().open(PDD_PACKAGE, stop=stop)


def search(
    keyword: str,
    input_resource_id: Optional[str] = None,
    search_button_text: Optional[str] = None,
    clear: bool = False,
    keyboard_done_text: Optional[str] = None,
    search_input: Optional[Dict[str, Any]] = None,
    search_button: Optional[Dict[str, Any]] = None,
) -> dict:
    """在拼多多内执行搜索。可传入 ensure_search_page() 返回的 search_input/search_button。"""
    return PDDAgent.init().search(
        keyword,
        input_resource_id=input_resource_id,
        search_button_text=search_button_text or "搜索",
        clear=clear,
        keyboard_done_text=keyboard_done_text,
        search_input=search_input,
        search_button=search_button,
    )


def ensure_search_page() -> dict:
    """
    打开拼多多并进入搜索页，dump 解析后返回搜索框、搜索按钮位置（供 search 使用）。
    使用 mobile_agent 通用 ensure_search_page；拼多多无特殊结构时不必覆盖。
    返回 {"ok": True, "result": {"search_input": {...}, "search_button": {...}}} 或错误 dict。
    """
    from mobile_agent import ensure_search_page as ma_ensure_search_page
    out = ma_ensure_search_page(app=PDD_PACKAGE, package=PDD_PACKAGE)
    r = out.get("result") or out.get("parsed") or {}
    return out


# 搜索中间页：搜索框左侧有「商品」/「店铺」选择器；目标为店铺则要点「店铺」，目标为商品则要点「商品」
STORE_TAB_TEXT = "店铺"
PRODUCT_TAB_TEXT = "商品"


def _find_tab_center_in_xml(xml_str: str, tab_text: str, package: str = PDD_PACKAGE) -> Optional[Tuple[int, int]]:
    """
    在 hierarchy XML 中查找 text 或 content-desc 为 tab_text 的节点，
    优先取可点击的父节点（下拉触发器多为父 ViewGroup），返回 bounds 中心 (cx, cy)。
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None
    want = (tab_text or "").strip()
    if not want:
        return None
    # 先找所有 text 或 content-desc 匹配的节点
    candidates: List[ET.Element] = []
    for n in root.iter():
        if (n.get("package") or "").strip() != package:
            continue
        t = (n.get("text") or "").strip()
        d = (n.get("content-desc") or "").strip()
        if t != want and d != want:
            continue
        b = _parse_bounds(n.get("bounds") or "")
        if b and len(b) >= 4:
            candidates.append(n)
    if not candidates:
        return None
    # 优先选可点击的节点（或可点击的父节点）
    for n in candidates:
        clickable = (n.get("clickable") or "").strip() == "true"
        if clickable:
            b = _parse_bounds(n.get("bounds") or "")
            if b and len(b) >= 4:
                return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
    # 否则找其可点击的祖先（搜索页上「商品」在 ViewGroup 内，父 ViewGroup 可点击）
    for n in candidates:
        node = n
        for _ in range(10):
            parent = None
            for p in root.iter():
                if node in list(p):
                    parent = p
                    break
            if parent is None:
                break
            node = parent
            if (node.get("clickable") or "").strip() == "true":
                b = _parse_bounds(node.get("bounds") or "")
                if b and len(b) >= 4:
                    return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
    # 回退：用第一个候选的 bounds 中心
    b = _parse_bounds(candidates[0].get("bounds") or "")
    if b and len(b) >= 4:
        return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
    return None


def select_search_tab(store: bool = True) -> dict:
    """
    当前页为搜索中间页时，先点当前 tab 弹出下拉，再点目标选项：
    - 目标为店铺(store=True)：点击「商品」→ 出现下拉 → 点击「店铺」；
    - 目标为商品(store=False)：点击「店铺」→ 出现下拉 → 点击「商品」。
    使用 dump + XML 解析坐标再 click(x,y)，避免 u.click(text=...) 长时间阻塞。
    返回 {"ok": True} 或错误 dict。
    """
    u = _u()
    open_tab = PRODUCT_TAB_TEXT if store else STORE_TAB_TEXT
    target_tab = STORE_TAB_TEXT if store else PRODUCT_TAB_TEXT
    _log(f"select_search_tab: 目标={'店铺' if store else '商品'}，先点 {open_tab!r} 弹出下拉...")
    u.dump()
    out = u.last_result
    if not out.get("ok"):
        _log(f"select_search_tab: dump 失败")
        return out
    xml_str = (out.get("result") or {}).get("xml") or ""
    cen = _find_tab_center_in_xml(xml_str, open_tab)
    if not cen:
        _log(f"select_search_tab: 未找到 {open_tab!r}")
        return {"ok": False, "error": "tab_not_found", "detail": f"未找到 {open_tab!r}"}
    u.click(x=cen[0], y=cen[1])
    time.sleep(0.7)
    # 第二步：点目标「店铺」或「商品」（下拉出现后可能需再 dump）
    _log(f"select_search_tab: 点选项 {target_tab!r}...")
    for attempt in range(3):
        u.dump()
        out = u.last_result
        if not out.get("ok"):
            break
        xml_str = (out.get("result") or {}).get("xml") or ""
        cen2 = _find_tab_center_in_xml(xml_str, target_tab)
        if cen2:
            u.click(x=cen2[0], y=cen2[1])
            time.sleep(0.6)
            _log(f"select_search_tab: 已选 {target_tab!r}")
            return {"ok": True}
        time.sleep(0.5)
    _log(f"select_search_tab: 未找到选项 {target_tab!r}")
    return {"ok": False, "error": "target_tab_not_found", "detail": f"下拉后未找到 {target_tab!r}"}


def select_store_search_tab() -> dict:
    """切换到店铺搜索 tab。等价于 select_search_tab(store=True)。"""
    return select_search_tab(store=True)


def _parse_store_search_result_xml(xml_str: str) -> List[Dict[str, Any]]:
    """
    解析店铺搜索结果页 XML（参考 mocks/store_search_result.xml）。
    列表项为 ViewGroup，顶部有店铺名 TextView（含「店」），右侧有「进店」。
    返回 [{"store_name": str, "center": (x,y), "bounds": ..., "enter_center": (x,y)}, ...]；进入商店必须点「进店」。
    """
    result: List[Dict[str, Any]] = []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return result
    # 找所有包含「进店」且包含店铺名（含「店」、较长）的 ViewGroup 作为店铺项，并记录「进店」按钮中心
    for n in root.iter():
        if (n.get("package") or "").strip() != PDD_PACKAGE:
            continue
        if (n.get("class") or "").strip() != "android.view.ViewGroup":
            continue
        b = _parse_bounds(n.get("bounds") or "")
        if not b or len(b) < 4:
            continue
        h = b[3] - b[1]
        if h < 400 or h > 800:
            continue
        w = b[2] - b[0]
        if w < 500:
            continue
        store_name = ""
        enter_center: Optional[Tuple[int, int]] = None
        for child in n.iter():
            if child == n:
                continue
            t = (child.get("text") or "").strip()
            if t == "进店":
                eb = _parse_bounds(child.get("bounds") or "")
                if eb and len(eb) >= 4:
                    enter_center = ((eb[0] + eb[2]) // 2, (eb[1] + eb[3]) // 2)
            if "店" in t and t != "进店" and 2 <= len(t) <= 50 and not store_name:
                store_name = t
        if store_name and enter_center is not None:
            cx = (b[0] + b[2]) // 2
            cy = (b[1] + b[3]) // 2
            result.append({
                "store_name": store_name,
                "center": (cx, cy),
                "bounds": b,
                "enter_center": enter_center,
            })
    # 按 y 排序，去重店名（同屏可能重复）
    seen: Set[str] = set()
    unique: List[Dict[str, Any]] = []
    for item in sorted(result, key=lambda x: x["center"][1]):
        name = (item.get("store_name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            unique.append(item)
    return unique


def search_store(store_name: str) -> dict:
    """
    完整店铺搜索并进入目标店铺：
    1. ensure_search_page → 2. 点击「店铺」→ 3. 输入关键词 → 4. 点击「搜索」→ 5. 进入店铺列表 → 6. 找到目标店铺并点击。
    返回 {"ok": True, "result": {"store_name": 匹配的店名}} 或错误 dict。
    """
    _log(f"search_store: 开始，目标={store_name!r}")
    page_out = ensure_search_page()
    if not page_out.get("ok"):
        _log(f"search_store: ensure_search_page 失败 {page_out}")
        return page_out
    page_el = page_out.get("result") or {}
    # 切换到店铺搜索
    tab_out = select_store_search_tab()
    if not tab_out.get("ok"):
        return tab_out
    time.sleep(0.5)
    # 输入关键词并点击搜索
    search(
        store_name.strip(),
        search_input=page_el.get("search_input"),
        search_button=page_el.get("search_button"),
    )
    time.sleep(1.5)
    u = _u()
    u.dump()
    out = u.last_result
    if not out.get("ok"):
        _log(f"search_store: dump 失败 {out}")
        return out
    xml_str = (out.get("result") or {}).get("xml") or ""
    if not xml_str:
        return {"ok": False, "error": "no_xml", "detail": "店铺搜索结果页无 xml"}
    items = _parse_store_search_result_xml(xml_str)
    _log(f"search_store: 解析到 {len(items)} 个店铺")
    target_store_name = store_name.strip()
    for item in items:
        name = (item.get("store_name") or "").strip()
        if not name:
            continue
        if target_store_name in name or name in target_store_name:
            cx, cy = item.get("enter_center") or item.get("center") or (0, 0)
            _log(f"search_store: 点击目标店铺「进店」 {name!r} at ({cx},{cy})")
            u.click(x=cx, y=cy)
            time.sleep(1.2)
            return {"ok": True, "result": {"store_name": name}}
    _log("search_store: 未找到目标店铺，尝试下滑再找")
    win = u.window_size()
    w = (win.get("result") or {}).get("width") or 540
    h = (win.get("result") or {}).get("height") or 960
    for _ in range(5):
        u.swipe(w // 2, int(h * 0.7), w // 2, int(h * 0.3), duration=0.3)
        time.sleep(0.9)
        u.dump()
        out = u.last_result
        if not out.get("ok"):
            break
        xml_str = (out.get("result") or {}).get("xml") or ""
        items = _parse_store_search_result_xml(xml_str)
        for item in items:
            name = (item.get("store_name") or "").strip()
            if target_store_name in name or name in target_store_name:
                cx, cy = item.get("enter_center") or item.get("center") or (0, 0)
                _log(f"search_store: 下滑后点击目标店铺「进店」 {name!r}")
                u.click(x=cx, y=cy)
                time.sleep(1.2)
                return {"ok": True, "result": {"store_name": name}}
    return {
        "ok": False,
        "error": "store_not_found",
        "detail": f"店铺搜索结果中未找到「{target_store_name}」",
    }


def dump_store_page_xml() -> dict:
    """
    当前页为店铺首页时，dump 当前屏 XML。
    返回 {"ok": True, "result": {"xml": str}} 或错误 dict。
    """
    u = _u()
    u.dump()
    out = u.last_result
    if not out.get("ok"):
        return out
    payload = out.get("result") or {}
    xml_str = payload.get("xml") or ""
    return {"ok": True, "result": {"xml": xml_str}}


def _parse_bounds(bounds: str) -> Optional[tuple]:
    """解析 bounds 字符串 '[x1,y1][x2,y2]' -> (x1, y1, x2, y2)。"""
    if not bounds:
        return None
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)))


def _is_price_text(text: str) -> bool:
    """是否为价格类文案（¥ 或 元 等）。"""
    if not text or len(text) > 20:
        return False
    return bool(re.search(r"¥|元|\d+\.?\d*元", text))


def _collect_text_nodes(root: ET.Element) -> List[Dict[str, Any]]:
    """从 hierarchy 根节点递归收集所有带 text 的节点，返回 [{text, bounds, resource_id}, ...]。"""
    items: List[Dict[str, Any]] = []
    for node in root.iter():
        text = (node.get("text") or "").strip()
        if not text:
            continue
        bounds = _parse_bounds(node.get("bounds") or "")
        resource_id = (node.get("resource-id") or "").strip()
        items.append({
            "text": text,
            "bounds": bounds,
            "resource_id": resource_id,
        })
    return items


def _find_card_containing_node(root: ET.Element, node: ET.Element, package: str = PDD_PACKAGE) -> Optional[ET.Element]:
    """找到包含 node 的最小 ViewGroup 卡片（高度在 [PRODUCT_CARD_MIN_HEIGHT, 900] 内）。"""
    node_b = _bounds_attrs(node)
    if not node_b or node_b[3] <= node_b[1]:
        return None
    best: Optional[ET.Element] = None
    best_area: Optional[float] = None
    for n in root.iter():
        if n.get("class") != "android.view.ViewGroup" or n.get("package") != package:
            continue
        b = _bounds_attrs(n)
        if len(b) < 4:
            continue
        h = b[3] - b[1]
        if h < PRODUCT_CARD_MIN_HEIGHT or h > 900:
            continue
        if b[0] <= node_b[0] and b[1] <= node_b[1] and b[2] >= node_b[2] and b[3] >= node_b[3]:
            area = (b[2] - b[0]) * (b[3] - b[1])
            if best is None or area < (best_area or 0):
                best = n
                best_area = area
    return best


def _has_product_card_features(node: ET.Element) -> Tuple[bool, str]:
    """
    判断节点及其子节点是否具备商品卡特征：1）有较大海报图 2）有价钱 3）含关键字「店」。
    返回 (是否商品卡, 标题文案)。
    """
    texts: List[Tuple[str, str]] = []  # (text, resource_id)
    has_large_image = False
    has_price = False
    has_店 = False
    title = ""
    for n in node.iter():
        if n.get("package") != PDD_PACKAGE:
            continue
        b = _bounds_attrs(n)
        if len(b) < 4:
            continue
        cls = (n.get("class") or "").strip()
        text = (n.get("text") or "").strip()
        rid = (n.get("resource-id") or "").strip()
        if "ImageView" in cls:
            area = (b[2] - b[0]) * (b[3] - b[1])
            if area >= POSTER_MIN_AREA:
                has_large_image = True
        if text:
            texts.append((text, rid))
            if _is_price_text(text) or re.match(r"^\d+\.?\d*$", text):
                has_price = True
            if "店" in text:
                has_店 = True
    if not has_large_image:
        return (False, "")
    if not (has_price or has_店):
        return (False, "")
    # 取标题：优先 tv_title，否则取最长非价格文案
    for t, rid in texts:
        if rid == TITLE_RID and t:
            title = t
            break
    if not title:
        for t, _ in sorted(texts, key=lambda x: -len(x[0])):
            if not _is_price_text(t) and "¥" not in t and "店" not in t and len(t) > 4:
                title = t
                break
    return (True, title or "")


def _find_product_cards_by_features(root: ET.Element) -> List[Tuple[ET.Element, str]]:
    """
    按商品特征从 dump 中找出商品卡：有海报图(大)、价钱、关键字「店」。
    返回 [(card_node, title), ...]，按卡片面积升序（便于后面取最小卡去重）。
    """
    candidates: List[Tuple[ET.Element, str]] = []
    for n in root.iter():
        pkg = (n.get("package") or "").strip()
        if pkg != PDD_PACKAGE:
            continue
        cls = (n.get("class") or "").strip()
        if cls not in ("android.view.ViewGroup", "android.widget.LinearLayout"):
            continue
        b = _bounds_attrs(n)
        if len(b) < 4:
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        if h < PRODUCT_CARD_MIN_HEIGHT or h > PRODUCT_CARD_MAX_HEIGHT:
            continue
        if w < PRODUCT_CARD_MIN_WIDTH or w > PRODUCT_CARD_MAX_WIDTH:
            continue
        ok, title = _has_product_card_features(n)
        if not ok:
            continue
        area = w * h
        candidates.append((n, title))
    # 按面积升序，保留每个“区域”最小的卡（同一商品可能有多层 ViewGroup）
    by_area = sorted(candidates, key=lambda x: (_bounds_attrs(x[0])[2] - _bounds_attrs(x[0])[0]) * (_bounds_attrs(x[0])[3] - _bounds_attrs(x[0])[1]))
    seen_centers: Set[Tuple[int, int]] = set()
    result: List[Tuple[ET.Element, str]] = []
    for card, title in by_area:
        b = _bounds_attrs(card)
        if len(b) < 4:
            continue
        cx = (b[0] + b[2]) // 2
        cy = (b[1] + b[3]) // 2
        key = (cx // 50 * 50, cy // 50 * 50)
        if key in seen_centers:
            continue
        seen_centers.add(key)
        result.append((card, title))
    return result


def get_product_click_targets(limit: int = 20) -> dict:
    """
    从当前页面 dump 中解析商品卡片，返回可点击位置列表（用于依次点击进详情）。
    按商品特征识别：有海报图(较大)、价钱(左下)、含关键字「店」；若无则回退到 tv_title 定位。
    返回 {"ok": True, "result": [{"center": (x,y), "bounds": (x1,y1,x2,y2), "title": "..."}, ...]}。
    """
    _log("get_product_click_targets: dump 当前页...")
    u = _u()
    u.dump()
    out = u.last_result
    if not out.get("ok"):
        _log(f"get_product_click_targets: dump 失败 {out}")
        return out
    payload = out.get("result") or {}
    xml_str = payload.get("xml")
    if not xml_str:
        return {"ok": False, "error": "no_xml", "detail": "dump 结果中无 xml 字段"}
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as e:
        return {"ok": False, "error": "parse_error", "detail": str(e)}
    targets: List[Dict[str, Any]] = []
    # 优先按特征识别：海报图 + 价钱/店
    cards = _find_product_cards_by_features(root)
    for card, title in cards:
        if len(targets) >= limit:
            break
        b = _bounds_attrs(card)
        if len(b) < 4:
            continue
        cx = (b[0] + b[2]) // 2
        cy = (b[1] + b[3]) // 2
        targets.append({
            "center": (cx, cy),
            "bounds": b,
            "title": title or "",
        })
    # 回退：用 tv_title 定位
    if not targets:
        seen_centers: Set[Tuple[int, int]] = set()
        for node in root.iter():
            if (node.get("resource-id") or "").strip() != TITLE_RID:
                continue
            text = (node.get("text") or node.get("content-desc") or "").strip()
            card = _find_card_containing_node(root, node)
            if card is None:
                continue
            b = _bounds_attrs(card)
            if len(b) < 4:
                continue
            cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
            if (cx, cy) in seen_centers:
                continue
            seen_centers.add((cx, cy))
            targets.append({"center": (cx, cy), "bounds": b, "title": text or ""})
            if len(targets) >= limit:
                break
    _log(f"get_product_click_targets: 解析到 {len(targets)} 个可点击商品")
    return {"ok": True, "result": targets}


def _parse_product_detail_xml(xml_str: str) -> Dict[str, Any]:
    """
    解析商品详情页 XML：提取商品标题/价格、店铺名、「进店」按钮中心坐标。
    返回 {"product": {"title": "", "price": ""}, "store": {"name": "", "enter_center": (x,y) or None}}。
    """
    result: Dict[str, Any] = {
        "product": {"title": "", "price": ""},
        "store": {"name": "", "enter_center": None},
    }
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return result
    texts: List[Dict[str, Any]] = []
    enter_bounds: Optional[Tuple[int, int, int, int]] = None
    for node in root.iter():
        t = (node.get("text") or "").strip()
        b = _parse_bounds(node.get("bounds") or "")
        if t == "进店" and b and len(b) >= 4:
            enter_bounds = b
        if t:
            texts.append({"text": t, "bounds": b, "y": b[1] if b and len(b) >= 4 else 0})
    if enter_bounds:
        cx = (enter_bounds[0] + enter_bounds[2]) // 2
        cy = (enter_bounds[1] + enter_bounds[3]) // 2
        result["store"]["enter_center"] = (cx, cy)
    for item in texts:
        t = item["text"]
        if not t:
            continue
        if t == "进店":
            continue
        if re.search(r"^¥\d+\.?\d*$", t) or (t.startswith("¥") and re.match(r"^¥\s*\d+", t)):
            if not result["product"]["price"]:
                result["product"]["price"] = t
        elif re.match(r"^\d+\.?\d*$", t) and len(t) <= 10 and item.get("y", 0) > 200:
            if not result["product"]["price"]:
                result["product"]["price"] = "¥" + t
        elif "店" in t and t != "进店" and len(t) >= 2 and len(t) <= 30:
            if not result["store"]["name"] and (t.endswith("店") or "店铺" in t):
                result["store"]["name"] = t
    for item in sorted(texts, key=lambda x: (x.get("y") or 0)):
        t = item["text"]
        if len(t) > 10 and not _is_price_text(t) and "¥" not in t and "进店" not in t:
            if not result["product"]["title"]:
                result["product"]["title"] = t
                break
    return result


def dump_product_detail(max_scrolls: int = 5) -> dict:
    """
    从当前设备页面（商品详情页）dump 并解析：商品信息 + 店铺信息（含「进店」位置）。
    若首屏无「进店」，会下滑最多 max_scrolls 次再解析。
    返回 {"ok": True, "result": {"product": {...}, "store": {"name": "", "enter_center": (x,y)?}}} 或错误 dict。
    """
    _log("dump_product_detail: 开始（获取窗口尺寸）...")
    u = _u()
    win = u.window_size()
    if not win.get("ok"):
        _log("dump_product_detail: window_size 失败")
        return {"ok": False, "error": "window_size", "detail": str(win)}
    w = (win.get("result") or {}).get("width") or 540
    h = (win.get("result") or {}).get("height") or 960
    fx, fy = w // 2, int(h * 0.75)
    tx, ty = w // 2, int(h * 0.25)
    last_parsed: Dict[str, Any] = {"product": {}, "store": {}}
    for i in range(max_scrolls):
        _log(f"dump_product_detail: 第 {i+1}/{max_scrolls} 次 dump...")
        u.dump()
        out = u.last_result
        if not out.get("ok"):
            _log("dump_product_detail: dump 失败")
            return out
        xml_str = (out.get("result") or {}).get("xml") or ""
        if not xml_str:
            _log("dump_product_detail: 无 xml")
            return {"ok": False, "error": "no_xml", "detail": "dump 结果中无 xml"}
        parsed = _parse_product_detail_xml(xml_str)
        last_parsed = parsed
        store_name = (parsed.get("store") or {}).get("name") or ""
        has_enter = bool((parsed.get("store") or {}).get("enter_center"))
        _log(f"dump_product_detail: 店铺名={store_name!r}, 有进店按钮={has_enter}")
        if has_enter:
            _log("dump_product_detail: 找到进店按钮，返回")
            return {"ok": True, "result": parsed}
        if i < max_scrolls - 1:
            _log("dump_product_detail: 下滑...")
            u.swipe(fx, fy, tx, ty, duration=0.25)
            time.sleep(1)
    _log("dump_product_detail: 达到最大下滑次数，返回最后解析结果")
    return {"ok": True, "result": last_parsed}


STORE_END_MARKER = "本店暂无更多商品"


def _xml_has_end_marker_in_bottom(xml_str: str, end_marker: str, screen_h: int) -> bool:
    """检查 dump 的 XML 中是否在底部区域（y > 0.7*screen_h）出现结束提示词。"""
    if not end_marker or end_marker not in xml_str:
        return False
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return True
    threshold_y = int(screen_h * 0.7)
    for n in root.iter():
        t = (n.get("text") or n.get("content-desc") or "").strip()
        if end_marker not in t:
            continue
        b = _parse_bounds(n.get("bounds") or "")
        if b and len(b) >= 4 and b[1] >= threshold_y:
            return True
    return False


def scroll_store_to_end(
    no_new_limit: int = 5,
    end_marker: str = STORE_END_MARKER,
    no_dedup: bool = False,
    max_cart_scrolls: int = 2,
    max_products_scrolls: Optional[int] = None,
) -> dict:
    """
    当前页为店铺首页时，逐屏下滑并合并（只合入有价商品，按 title+价格 去重）：
    parsed_list 存放每屏新商品批次，parsed_in_last_screen 为上一屏新商品，parsed_in_current_screen 为当前屏有价商品。
    while: dump -> tmp = merge_products(last, current) -> parsed_list.append(tmp) -> parsed_in_last_screen = tmp -> 退出判断 -> 下滑。
    停止条件：底部 end_marker、连续 no_new_limit 次本屏新增为 0、或达到 max_products_scrolls。
    返回 {"merged": {store, products}, "parsed_list": [batch1, batch2, ...]}。
    """
    _log("scroll_store_to_end: 开始...")
    u = _u()
    win = u.window_size()
    if not win.get("ok"):
        _log("scroll_store_to_end: window_size 失败")
        return {"ok": False, "error": "window_size", "detail": str(win)}
    w = (win.get("result") or {}).get("width") or 540
    h = (win.get("result") or {}).get("height") or 960
    fx, fy = w // 2, int(h * 0.75)
    tx, ty = w // 2, int(h * 0.25)
    _log(f"scroll_store_to_end: 屏幕 {w}x{h}, 下滑坐标 from=({fx},{fy}) to=({tx},{ty}), no_new_limit={no_new_limit}, max_products_scrolls={max_products_scrolls}")
    # parsed_list: 存放每屏 merge 结果（每项是一批新商品）；parsed_in_last_screen: 上一屏新商品，初始为空
    parsed_list: List[List[Dict[str, Any]]] = []
    parsed_in_last_screen: List[Dict[str, Any]] = []
    no_slide_count = 0
    round_no = 0
    scroll_count = 0
    store: Dict[str, Any] = {}
    while True:
        round_no += 1
        _log(f"scroll_store_to_end: 第 {round_no} 轮 dump...")
        u.dump()
        out = u.last_result
        if not out.get("ok"):
            _log("scroll_store_to_end: dump 失败")
            return out
        xml_str = (out.get("result") or {}).get("xml") or ""
        parsed = parse_store_page_xml(xml_str)
        if not store and (parsed.get("store") or {}):
            store = parsed.get("store") or {}
        parsed_in_current_screen = parsed.get("products") or []

        if _xml_has_end_marker_in_bottom(xml_str, end_marker, h):
            _log(f"scroll_store_to_end: 底部出现结束文案 {end_marker!r}，停止")
            break

        tmp = merge_products(parsed_in_last_screen, parsed_in_current_screen)
        if tmp:
            _enrich_cart_remark_for_products(u, tmp, w, h, max_cart_scrolls=max_cart_scrolls)
        parsed_list.append(tmp)
        for j, p in enumerate(tmp):
            tit = (p.get("title_short") or p.get("title") or "").strip()[:40]
            pr = p.get("price")
            _log(f"scroll_store_to_end:   [{j}] {tit!r} price={pr!r}")
        parsed_in_last_screen = tmp

        total_now = sum(len(batch) for batch in parsed_list)
        """
        _log(f"scroll_store_to_end: 本屏有价={len([p for p in parsed_in_current_screen if _normalize_price_for_key(p.get('price'))])}，本屏新增={len(tmp)}，累计 {total_now} 条，连续无新增={no_slide_count}/{no_new_limit}")
        """
        if len(tmp) == 0:
            no_slide_count += 1
        else:
            no_slide_count = 0
        if no_slide_count >= no_new_limit:
            _log("scroll_store_to_end: 连续滑不动达到上限，停止")
            break

        scroll_count += 1
        if max_products_scrolls is not None and scroll_count >= max_products_scrolls:
            _log(f"scroll_store_to_end: 店内下滑次数已达 {max_products_scrolls}，停止")
            break

        u.swipe(fx, fy, tx, ty, duration=0.25)
        """
            must sleep enough time to let the screen freeze
            otherwise, click(x,y) will fail
        """
        time.sleep(1.5)

    merged_products: List[Dict[str, Any]] = []
    for batch in parsed_list:
        merged_products.extend(batch)
    merged = {"store": store, "products": merged_products}
    _log(f"scroll_store_to_end: 共 {len(parsed_list)} 屏，合并商品数={len(merged_products)}，完成")
    return {"ok": True, "result": {"merged": merged, "parsed_list": parsed_list}}


def merge_store_parsed_list(parsed_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    store: Dict[str, Any] = {}
    products: List[Dict[str, Any]] = []
    seen_keys: Set[str] = set()
    key_to_index: Dict[str, int] = {}
    for p in parsed_list:
        s = p.get("store") or {}
        if s and not store:
            store = s
        for prod in p.get("products") or []:
            key = _product_seen_key(prod)
            if key not in seen_keys:
                seen_keys.add(key)
                key_to_index[key] = len(products)
                products.append(prod)
            else:
                remark = (prod.get("备注") or "").strip()
                if remark:
                    idx = key_to_index.get(key)
                    if idx is not None and not (products[idx].get("备注") or "").strip():
                        products[idx]["备注"] = remark
    return {"store": store, "products": products}


def merge_store_parsed_list_keep_all(parsed_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """不去重：按出现顺序保留所有商品；同一 xml_node_id 只保留一条并合并备注（同屏重复卡只记一次）。"""
    store: Dict[str, Any] = {}
    products: List[Dict[str, Any]] = []
    seen_id_to_idx: Dict[str, int] = {}
    for p in parsed_list:
        s = p.get("store") or {}
        if s and not store:
            store = s
        for prod in p.get("products") or []:
            nid = (prod.get("xml_node_id") or "").strip()
            if nid and nid in seen_id_to_idx:
                idx = seen_id_to_idx[nid]
                remark = (prod.get("备注") or "").strip()
                if remark and not (products[idx].get("备注") or "").strip():
                    products[idx]["备注"] = remark
                continue
            if nid:
                seen_id_to_idx[nid] = len(products)
            products.append(prod)
    return {"store": store, "products": products}


def merge_products(
    parsed_in_last_screen: List[Dict[str, Any]],
    parsed_in_current_screen: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    按 title+价格 去重：返回「当前屏有价商品」中不在「上一屏新商品」里的列表（B - A）。
    只合入有价格的商品；同一商品以 _same_product_by_name_price 判定。
    """
    current_with_price = [
        p for p in parsed_in_current_screen
        if _normalize_price_for_key(p.get("price"))
    ]
    return [
        p for p in current_with_price
        if not any(_same_product_by_name_price(p, q) for q in parsed_in_last_screen)
    ]


def merge_store_parsed_list_by_name_price(parsed_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    总表 T。A = 上一屏新出现的商品（滑动前），B = 当前屏所有有价商品（滑动后）。
    第一屏：A = 本屏有价列表，A => T，A = A（本屏即「新出现」）；
    第二屏起：B - A => T（滑动后新出现的合入总表），A = B - A（下一屏用）。
    同一商品以 name+价格 判定（支持名字前缀一致）。
    """
    store: Dict[str, Any] = {}
    T: List[Dict[str, Any]] = []
    A: List[Dict[str, Any]] = []
    for i in range(len(parsed_list)):
        s = (parsed_list[i].get("store") or {}) if parsed_list[i] else {}
        if s and not store:
            store = s
        B = [p for p in (parsed_list[i].get("products") or []) if _normalize_price_for_key(p.get("price"))]
        if i == 0:
            T.extend(B)
            A = B
            _log(f"合并: 第1屏 有价商品数={len(B)} => T，累计 {len(T)} 条")
        else:
            new_list = [p for p in B if not any(_same_product_by_name_price(p, q) for q in A)]
            _log(f"合并: 第{i+1}屏 B有价={len(B)}，B-A={len(new_list)} => T，累计 {len(T) + len(new_list)} 条")
            for p in new_list:
                tit = (p.get("title_short") or p.get("title") or "").strip()[:30]
                _log(f"合并:   比较 => 新增 屏{i+1} 商品 {tit!r} price={p.get('price')!r}")
            T.extend(new_list)
            A = new_list
    return {"store": store, "products": T}


# tags 中出现以下任一关键词则标记为缺货
OUT_OF_STOCK_KEYWORDS = ("售罄", "缺货", "仅剩", "最后")


def _out_of_stock_flag(tags: List[str]) -> str:
    """若 tags 中出现售罄/缺货/仅剩/最后等则返回 'Y'，否则返回 ''。"""
    if not tags:
        return ""
    joined = "|".join(tags) if isinstance(tags, list) else str(tags)
    return "Y" if any(kw in joined for kw in OUT_OF_STOCK_KEYWORDS) else ""


_ONLY_LEFT_RE = re.compile(r"仅剩\s*(\d+)\s*件")


def _extract_only_left(product: Dict[str, Any]) -> str:
    """从商品信息中提取「仅剩X件」中的数字，未出现则返回空字符串。"""
    texts: List[str] = []
    for key in ("tags", "title", "title_short", "sales"):
        v = product.get(key)
        if isinstance(v, list):
            texts.extend(str(x) for x in v)
        elif v:
            texts.append(str(v).strip())
    for s in texts:
        m = _ONLY_LEFT_RE.search(s)
        if m:
            return m.group(1)
    return ""


def _should_open_cart_for_remark(product: Dict[str, Any]) -> bool:
    """是否需要点开购物车取备注：商品带「仅剩X件」或「即将售罄」时返回 True。"""
    if _extract_only_left(product):
        return True
    texts: List[str] = []
    for key in ("tags", "title", "title_short", "sales"):
        v = product.get(key)
        if isinstance(v, list):
            texts.extend(str(x) for x in v)
        elif v:
            texts.append(str(v).strip())
    joined = "|".join(texts)
    return "即将售罄" in joined


def _find_card_for_product(root: ET.Element, product: Dict[str, Any], package: str = PDD_PACKAGE) -> Optional[ET.Element]:
    """
    在店铺首页 dump 的 root 中，根据商品 title/title_short 找到对应商品卡 ViewGroup。
    用于后续在该卡内找右下角「+」按钮。
    """
    title_short = (product.get("title_short") or product.get("title") or "").strip()
    if not title_short:
        return None
    title_node: Optional[ET.Element] = None
    for node in root.iter():
        if (node.get("resource-id") or "").strip() != TITLE_RID or node.get("package") != package:
            continue
        t = (node.get("text") or node.get("content-desc") or "").strip()
        if not t:
            continue
        # 匹配标题（允许截断或完全一致）
        if title_short in t or t in title_short or title_short[:20] in t:
            title_node = node
            break
    if title_node is None:
        return None
    title_b = _bounds_attrs(title_node)
    if not title_b or title_b[3] <= title_b[1]:
        return None
    card: Optional[ET.Element] = None
    for n in root.iter():
        if n.get("class") != "android.view.ViewGroup" or n.get("package") != package:
            continue
        b = _bounds_attrs(n)
        if len(b) < 4:
            continue
        h = b[3] - b[1]
        if h < PRODUCT_CARD_MIN_HEIGHT or h > 900:
            continue
        if b[0] <= title_b[0] and b[1] <= title_b[1] and b[2] >= title_b[2] and b[3] >= title_b[3]:
            if card is None or (b[3] - b[1]) < (_bounds_attrs(card)[3] - _bounds_attrs(card)[1]):
                card = n
    return card


def _find_plus_button_in_card(card: ET.Element) -> Optional[Tuple[int, int, int, int]]:
    """
    在商品卡内用 bounds 定位「右下角」区域，找该区域内可点击的 ImageView（加号为图片，无文字）。
    参考 store.xml：加号为 ImageView clickable=true，如 [971,1643][1060,1701]。
    策略：用卡片 bounds 划出右下角（右约 25% 宽、下约 25% 高），在该区域内找 clickable 的
    ImageView；若无则找任意 clickable 节点；取右边缘、下边缘最大者（最靠右下角）。
    """
    card_b = _bounds_attrs(card)
    if len(card_b) < 4:
        return None
    c_x1, c_y1, c_x2, c_y2 = card_b
    card_w = c_x2 - c_x1
    card_h = c_y2 - c_y1
    # 右下角区域：卡片右 25% 宽、下 25% 高（加号图片固定在此角）
    right_zone_x1 = c_x1 + int(card_w * 0.75)
    bottom_zone_y1 = c_y1 + int(card_h * 0.75)
    # 节点需与此区域有交集：节点右缘 >= 区域左缘 且 节点下缘 >= 区域上缘
    def in_corner(b: Tuple[int, int, int, int]) -> bool:
        return b[2] >= right_zone_x1 and b[3] >= bottom_zone_y1

    image_candidates: List[Tuple[int, int, int, int]] = []
    any_clickable: List[Tuple[int, int, int, int]] = []
    for n in card.iter():
        if (n.get("clickable") or "").strip() != "true":
            continue
        b = _bounds_attrs(n)
        if len(b) < 4 or not in_corner(b):
            continue
        area = (b[2] - b[0]) * (b[3] - b[1])
        if area < 400 or area > 50000:  # 按钮级大小，排除整卡或噪点
            continue
        cls = (n.get("class") or "").strip()
        if "ImageView" in cls:
            image_candidates.append(b)
        any_clickable.append(b)
    # 优先用右下角内的可点击 ImageView；若无则用任意可点击
    candidates = image_candidates if image_candidates else any_clickable
    if not candidates:
        return None
    return max(candidates, key=lambda rect: (rect[2], rect[3]))


# 购物车弹窗内「型号/款式」下的「最后x件」正则（参考 pdd_skills/mocks/cart.xml）
_LAST_PIECES_RE = re.compile(r"最后\s*(\d+)\s*件")

# 弹窗中需排除的文案（非规格行）；规格区标题用于定位，不参与收集
_CART_POPUP_SKIP_TEXTS = frozenset({
    "型号", "款式", "颜色", "关闭", "取消", "×", "X",
    "手动添加地址", "优惠", "添加", "提交订单", "减少数量", "增加数量",
    "已选", "商品主图", "用多多支付", "使用#微信支付", "更换支付方式", "打开大图",
})


# 只保留含「最后X件」的规格行（型号/款式）
_LAST_PIECES_IN_LINE_RE = re.compile(r"最后\s*\d+\s*件")
# 排除仅「最后X件」无规格名的行（row 可能单独成行）
_ONLY_LAST_PIECES_RE = re.compile(r"^最后\s*\d+\s*件\s*$")


# 规格区标题（型号/款式/颜色），用于定位规格列表起点（参考 cart.xml 款式、cart2 颜色）
_CART_SECTION_HEADERS = ("型号", "款式", "颜色")


def _spec_name_from_view_group(node: ET.Element) -> Optional[str]:
    """从 ViewGroup 节点取规格名：content-desc 或子节点 tv_content 的 text。"""
    desc = (node.get("content-desc") or "").strip()
    if desc and desc not in _CART_POPUP_SKIP_TEXTS:
        return desc
    for c in node.iter():
        if "tv_content" in (c.get("resource-id") or ""):
            t = (c.get("text") or "").strip()
            if t and t not in _CART_POPUP_SKIP_TEXTS:
                return t
    return None


def _parse_cart_popup_xml_to_last_pieces_lines(xml_str: str) -> List[str]:
    """
    解析加购弹窗 XML（参考 mocks/cart.xml, cart1, cart2, cart3）。
    cart：款式 + 规格行内 text「最后3件」；cart2/cart3：颜色 + RecyclerView 内 content-desc 规格名 + 独立 TextView「最后X件」。
    只提取含有「最后X件」的规格行，返回行列表（每行一条，如 "规格A 最后2件"）。
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return []
    package = PDD_PACKAGE
    section_y: Optional[int] = None
    for n in root.iter():
        if n.get("package") != package:
            continue
        t = (n.get("text") or "").strip()
        if t not in _CART_SECTION_HEADERS:
            continue
        b = _parse_bounds(n.get("bounds") or "")
        if b and len(b) >= 4:
            section_y = (b[1] + b[3]) // 2
            break
    bottom_cutoff = 2200
    seen_lines: Set[str] = set()

    # cart2/cart3：含「最后X件」的 TextView 在其 ViewGroup 内，规格名在 content-desc 或 tv_content
    parent_map: Dict[ET.Element, ET.Element] = {}
    for p in root.iter():
        for c in p:
            parent_map[c] = p
    for n in root.iter():
        if n.get("package") != package:
            continue
        text = (n.get("text") or "").strip()
        if not _LAST_PIECES_IN_LINE_RE.search(text):
            continue
        b = _parse_bounds(n.get("bounds") or "")
        if not b or len(b) < 4:
            continue
        y_c = (b[1] + b[3]) // 2
        if section_y is not None and y_c <= section_y:
            continue
        if y_c >= bottom_cutoff:
            continue
        cur = n
        while cur in parent_map:
            cur = parent_map[cur]
            if (cur.get("class") or "").find("ViewGroup") == -1:
                continue
            bcur = _parse_bounds(cur.get("bounds") or "")
            if not bcur or len(bcur) < 4 or (section_y is not None and (bcur[1] + bcur[3]) // 2 <= section_y):
                continue
            spec = _spec_name_from_view_group(cur)
            if spec:
                line = f"{spec.strip()} {text}".strip()
                if line and line not in seen_lines:
                    seen_lines.add(line)
                break
    lines_from_cards: List[str] = sorted(seen_lines)

    # cart 风格：按 y 分行，同一行内规格名 + 最后X件 在一起
    items: List[Tuple[str, int, int]] = []
    for n in root.iter():
        if n.get("package") != package:
            continue
        b = _parse_bounds(n.get("bounds") or "")
        if not b or len(b) < 4:
            continue
        y_c = (b[1] + b[3]) // 2
        if section_y is not None and y_c <= section_y:
            continue
        if y_c >= bottom_cutoff:
            continue
        x_c = (b[0] + b[2]) // 2
        for raw in (n.get("text") or "", n.get("content-desc") or ""):
            t = raw.strip()
            if not t or t in _CART_POPUP_SKIP_TEXTS:
                continue
            if any(skip in t for skip in ("已选:", "¥", "减", "更换支付")):
                continue
            items.append((t, y_c, x_c))
    if items:
        items.sort(key=lambda x: (x[1], x[2]))
        row_y: Optional[int] = None
        row_threshold = 55
        current: List[str] = []
        for t, y, _ in items:
            if row_y is not None and abs(y - row_y) > row_threshold and current:
                line = " ".join(current).strip()
                if line and _LAST_PIECES_IN_LINE_RE.search(line) and not _ONLY_LAST_PIECES_RE.match(line) and line not in seen_lines:
                    seen_lines.add(line)
                current = []
            row_y = y
            if t not in current:
                current.append(t)
        if current:
            line = " ".join(current).strip()
            if line and _LAST_PIECES_IN_LINE_RE.search(line) and not _ONLY_LAST_PIECES_RE.match(line) and line not in seen_lines:
                seen_lines.add(line)

    # 仅「最后X件」无规格名的行不纳入
    result = [ln for ln in lines_from_cards if not _ONLY_LAST_PIECES_RE.match(ln)]
    for ln in sorted(seen_lines):
        if ln not in result and not _ONLY_LAST_PIECES_RE.match(ln):
            result.append(ln)
    return result


def _parse_cart_popup_xml(xml_str: str) -> str:
    """
    解析加购弹窗 XML，只保留含「最后X件」的型号/款式行，每行一条，用换行符连接。
    """
    lines = _parse_cart_popup_xml_to_last_pieces_lines(xml_str)
    return "\n".join(lines) if lines else ""


def _close_cart_popup(u: Any, screen_w: int = 540, screen_h: int = 960) -> bool:
    """仅在当前确认为购物车弹窗时才按 back 关闭，避免误退上级页。"""
    u.dump()
    out = u.last_result
    if not out.get("ok"):
        _log("购物车: 关弹窗 dump 失败，不按 back")
        return False
    xml_str = (out.get("result") or {}).get("xml") or ""
    is_cart = "关闭" in xml_str or any(h in xml_str for h in _CART_SECTION_HEADERS)
    if not is_cart:
        _log("===============购物车: 关弹窗 当前不在购物车弹窗内，不按 back==========")
        return True
    u.press("back")
    time.sleep(0.5)
    return True


def _find_scrollable_spec_recycler_bounds(
    xml_str: str, package: str = PDD_PACKAGE
) -> Optional[Tuple[Tuple[int, int, int, int], str]]:
    """
    在弹窗 XML 中找规格区的可滑动 RecyclerView，返回 (bounds, orientation)。
    orientation: "vertical" 竖滑（高>宽）、"horizontal" 横滑（宽>=高），根据 bounds 宽高比推断。
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None
    section_y: Optional[int] = None
    for n in root.iter():
        if n.get("package") != package:
            continue
        t = (n.get("text") or "").strip()
        if t not in _CART_SECTION_HEADERS:
            continue
        b = _parse_bounds(n.get("bounds") or "")
        if b and len(b) >= 4:
            section_y = (b[1] + b[3]) // 2
            break
    for n in root.iter():
        if n.get("package") != package:
            continue
        if "RecyclerView" not in (n.get("class") or ""):
            continue
        if (n.get("scrollable") or "").strip() != "true":
            continue
        b = _parse_bounds(n.get("bounds") or "")
        if len(b) < 4:
            continue
        if section_y is not None and b[1] < section_y - 200:
            continue
        if b[3] > 2200:
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        orientation = "vertical" if h > w else "horizontal"
        return (tuple(b), orientation)
    return None


def _collect_cart_remark_with_scroll(
    u: Any,
    screen_w: int,
    screen_h: int,
    max_scrolls: int = 2,
    max_scrolls_horizontal: int = 3,
) -> str:
    """
    当前已在加购弹窗内：先 dump 解析一次；若存在可滑动的规格区 RecyclerView（款式/型号/颜色很多时需横滑或竖滑），
    则在其内竖滑、横滑各若干次，每次 dump 解析，合并所有「最后X件」行后去重，用换行连接返回。
    """
    all_lines: List[str] = []
    seen: Set[str] = set()

    def add_from_xml(xml_str: str) -> None:
        for line in _parse_cart_popup_xml_to_last_pieces_lines(xml_str):
            if line and line not in seen:
                seen.add(line)
                all_lines.append(line)

    u.dump()
    out = u.last_result
    if not out.get("ok"):
        _log("购物车: dump 失败，返回空备注")
        return ""
    popup_xml = (out.get("result") or {}).get("xml") or ""
    add_from_xml(popup_xml)

    # 仅当确认为购物车弹窗（含关闭或规格区标题）时才在弹窗内滑动，避免误滑店铺列表
    is_cart = "关闭" in popup_xml or any(h in popup_xml for h in _CART_SECTION_HEADERS)
    found = _find_scrollable_spec_recycler_bounds(popup_xml) if is_cart else None
    if found:
        scroll_bounds, orientation = found
        sx1, sy1, sx2, sy2 = scroll_bounds
        cx = (sx1 + sx2) // 2
        cy = (sy1 + sy2) // 2
        """
        _log(f"购物车: 步骤4 找到规格区可滑区域 bounds={scroll_bounds}，推断方向={orientation}（高>宽竖滑，宽>=高横滑）")
        """
        if orientation == "vertical":
            # 竖滑：区域高>宽，规格多行需下滑看全
            from_y = sy2 - 150
            to_y = sy1 + 150
            for i in range(max_scrolls):
                u.swipe(cx, from_y, cx, to_y, duration=0.25)
                time.sleep(1)
                u.dump()
                o = u.last_result
                if not o.get("ok"):
                    _log("购物车: 竖滑后 dump 失败，停止竖滑")
                    break
                add_from_xml((o.get("result") or {}).get("xml") or "")
        else:
            # 横滑：区域宽>=高，规格横向排列需左滑看全
            from_x = sx2 - 100
            to_x = sx1 + 100
            for i in range(max_scrolls_horizontal):
                u.swipe(from_x, cy, to_x, cy, duration=0.25)
                time.sleep(1)
                u.dump()
                o = u.last_result
                if not o.get("ok"):
                    _log("购物车: 横滑后 dump 失败，停止横滑")
                    break
                add_from_xml((o.get("result") or {}).get("xml") or "")
    else:
        """_log("购物车: 无规格区可滑区域或非购物车弹窗，不滑动")"""
    return "\n".join(all_lines) if all_lines else ""


def _product_seen_key(p: Dict[str, Any]) -> str:
    """判重键：优先用 xml_node_id（XML index/bounds），不依赖商品名称价格。"""
    nid = (p.get("xml_node_id") or "").strip()
    if nid:
        return nid
    title = (p.get("title") or p.get("title_short") or "").strip()
    return (title or "") + "|" + str(p.get("price") or "")


def _normalize_price_for_key(price: Any) -> str:
    """价格规范化为数字串，便于前后两屏同一商品能匹配（¥88 / 88 / ¥12.5 -> 88 / 12.5）。"""
    if price is None:
        return ""
    s = str(price).strip().replace("¥", "").replace("￥", "").strip()
    m = re.match(r"^(\d+\.?\d*)", s)
    return m.group(1) if m else s


def _normalize_name_for_key(name: str) -> str:
    """名字规范化：去首尾空白、多空格合并为一，便于前后两屏匹配。"""
    if not name:
        return ""
    return re.sub(r"\s+", " ", (name or "").strip())


def _name_price_key(p: Dict[str, Any]) -> str:
    """仅按名字+价格判重：同名同价视为同一商品（用于下滑前后两屏去重）。名字和价格均做规范化。"""
    name = (p.get("title") or p.get("title_short") or "").strip()
    norm_name = _normalize_name_for_key(name)
    norm_price = _normalize_price_for_key(p.get("price"))
    return (norm_name or "") + "|" + (norm_price or "")


def _same_product_by_name_price(prod: Dict[str, Any], existing: Dict[str, Any]) -> bool:
    """
    两商品是否视为同一（用于同轮两屏去重）：title+价格。
    价格：都为空视为相同，否则必须规范化后相等。
    名字：优先用 title_short（两屏一致），相同或一方为另一方前缀即视为同商品。
    """
    price_a = _normalize_price_for_key(prod.get("price"))
    price_b = _normalize_price_for_key(existing.get("price"))
    if price_a != price_b:
        if not (not price_a and not price_b):
            return False
    # 优先 title_short，保证两屏用同一字段，避免一屏 title 一屏 title_short 导致不匹配
    name_a = _normalize_name_for_key(prod.get("title_short") or prod.get("title") or "")
    name_b = _normalize_name_for_key(existing.get("title_short") or existing.get("title") or "")
    if not name_a or not name_b:
        return (name_a or "") == (name_b or "")
    if name_a == name_b:
        return True
    if name_a.startswith(name_b) or name_b.startswith(name_a):
        return True
    return False


def _enrich_cart_remark_on_screen(
    u: Any,
    parsed: Dict[str, Any],
    xml_str: str,
    screen_w: int,
    screen_h: int,
    seen_with_remark: Set[str],
    enrich_every_product: bool = False,
    max_cart_scrolls: int = 2,
) -> None:
    """
    对当前屏商品：若 enrich_every_product 为 True 则每个都点进购物车取备注；
    否则仅对「仅剩/即将售罄」且未处理过的商品点进购物车。
    是否已处理过以 xml_node_id（或 title|price 回退）为准，不依赖名称价格判重。
    弹窗内支持多种样式（cart/cart1/cart2/cart3）；规格多时竖滑 max_cart_scrolls 次（默认 2）+横滑 5 次收集「最后X件」。
    备注只记录含「最后X件」的型号/款式，每行一条（换行分隔）。
    """
    products = parsed.get("products") or []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return
    for p in products:
        key = _product_seen_key(p)
        if not enrich_every_product:
            if not _should_open_cart_for_remark(p):
                continue
            if key in seen_with_remark:
                continue
        else:
            if key in seen_with_remark:
                continue
        seen_with_remark.add(key)
        title_short = (p.get("title_short") or p.get("title") or "").strip()[:30]
        _log(f"购物车: [商品] 打开加购弹窗取备注: {title_short!r}...")
        u.dump()
        out = u.last_result
        if not out.get("ok"):
            _log("购物车: [商品] dump 失败，跳过该商品")
            continue
        fresh_xml = (out.get("result") or {}).get("xml") or ""
        if not fresh_xml:
            _log("购物车: [商品] 无 xml，跳过该商品")
            continue
        try:
            fresh_root = ET.fromstring(fresh_xml)
        except ET.ParseError:
            _log("购物车: [商品] xml 解析失败，跳过该商品")
            continue
        card = _find_card_for_product(fresh_root, p)
        if card is None:
            _log("购物车: [商品] 未找到该商品卡片，跳过")
            continue
        plus_bounds = _find_plus_button_in_card(card)
        if not plus_bounds:
            _log("购物车: [商品] 未找到卡片内「+」按钮，跳过")
            continue
        # 点加号最右上角，避免被悬浮窗挡住
        cx = plus_bounds[2] - 2
        cy = plus_bounds[1] + 2
        u.click(x=cx, y=cy)
        time.sleep(1)
        remark = _collect_cart_remark_with_scroll(u, screen_w, screen_h, max_scrolls=max_cart_scrolls)
        p["备注"] = remark or ""
        if remark:
            _log(f"购物车: [商品] {remark[:60]}{'...' if len(remark) > 60 else ''!r}")
        _close_cart_popup(u, screen_w, screen_h)
        time.sleep(0.5)


def _enrich_cart_remark_for_products(
    u: Any,
    products: List[Dict[str, Any]],
    screen_w: int,
    screen_h: int,
    max_cart_scrolls: int = 2,
) -> None:
    """
    仅对给定的商品列表逐个进购物车取备注；每关闭弹窗只 press back 一次，避免退出上级页。
    """

    for p in products:
        title_short = (p.get("title_short") or p.get("title") or "").strip()[:30]
        u.dump()
        out = u.last_result
        if not out.get("ok"):
            _log("购物车: [tmp商品] dump 失败，跳过该商品")
            continue
        fresh_xml = (out.get("result") or {}).get("xml") or ""
        if not fresh_xml:
            _log("购物车: [tmp商品] 无 xml，跳过该商品")
            continue
        try:
            fresh_root = ET.fromstring(fresh_xml)
        except ET.ParseError:
            _log("购物车: [tmp商品] xml 解析失败，跳过该商品")
            continue
        card = _find_card_for_product(fresh_root, p)
        if card is None:
            _log("购物车: [tmp商品] 未找到该商品卡片，跳过")
            continue
        plus_bounds = _find_plus_button_in_card(card)
        if not plus_bounds:
            _log("购物车: [tmp商品] 未找到卡片内「+」按钮，跳过")
            continue
        # 点加号最右上角，避免被悬浮窗挡住
        cx = plus_bounds[2] - 2
        cy = plus_bounds[1] + 2
        u.click(x=cx, y=cy)
        time.sleep(1)
        remark = _collect_cart_remark_with_scroll(u, screen_w, screen_h, max_scrolls=max_cart_scrolls)
        p["备注"] = remark or ""
        if remark:
            """_log(f"备注={remark[:60]}{'...' if len(remark) > 60 else ''!r}")"""
        _close_cart_popup(u, screen_w, screen_h)
        time.sleep(0.5)        

def to_csv_rows(store: Dict[str, Any], products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将店铺 + 商品列表转为可写 CSV 的扁平行列表。每行包含店铺字段 + 单商品字段 + 缺货标志 + 仅剩。"""
    rows: List[Dict[str, Any]] = []
    store_name = (store.get("store_name") or "").strip()
    total_sales = (store.get("total_sales") or "").strip()
    good_reviews = (store.get("good_reviews") or "").strip()
    for p in products:
        tag_list = p.get("tags") or []
        tags_str = "|".join(tag_list)
        rows.append({
            "store_name": store_name,
            "total_sales": total_sales,
            "good_reviews": good_reviews,
            "product_title": (p.get("title") or p.get("title_short") or "").strip(),
            "price": p.get("price"),
            "sales": p.get("sales"),
            "tags": tags_str,
            "缺货标志": _out_of_stock_flag(tag_list),
            "仅剩": _extract_only_left(p),
            "备注": (p.get("备注") or "").strip(),
        })
    return rows


def write_store_csv(rows: List[Dict[str, Any]], path: str) -> None:
    """将 to_csv_rows 得到的行写入 CSV 文件。"""
    if not rows:
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _group_into_products(items: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """
    将收集到的文本节点按位置和价格启发式分组为商品条。
    策略：按 bounds 的 y 中心排序，相邻且含价格的行合并为一条商品。
    """
    if not items:
        return []

    def y_center(b: Optional[tuple]) -> float:
        if not b or len(b) < 4:
            return 0.0
        return (b[1] + b[3]) / 2.0

    # 按纵坐标排序
    sorted_items = sorted(items, key=lambda x: y_center(x["bounds"]))
    products: List[Dict[str, Any]] = []
    current: List[Dict[str, Any]] = []
    row_y: Optional[float] = None
    ROW_THRESHOLD = 80  # 同一行/同一商品块 y 差阈值（像素）

    for it in sorted_items:
        y = y_center(it["bounds"])
        if row_y is not None and abs(y - row_y) > ROW_THRESHOLD and current:
            # 新的一行/块，把 current 合成一条商品
            title = " ".join(
                i["text"] for i in current
                if not _is_price_text(i["text"]) and len(i["text"]) > 1
            )
            price = next(
                (i["text"] for i in current if _is_price_text(i["text"])),
                None,
            )
            if title or price:
                products.append({"title": title or "", "price": price})
            if len(products) >= limit:
                break
            current = []
        row_y = y
        current.append(it)

    if current and len(products) < limit:
        title = " ".join(
            i["text"] for i in current
            if not _is_price_text(i["text"]) and len(i["text"]) > 1
        )
        price = next(
            (i["text"] for i in current if _is_price_text(i["text"])),
            None,
        )
        if title or price:
            products.append({"title": title or "", "price": price})

    return products[:limit]


def dump_products(limit: int = 10) -> dict:
    """
    从当前页面 dump 的层级 XML 中解析商品列表。
    依赖已通过 mobile_agent 连接设备且当前在搜索结果/列表页。
    返回 {"ok": True, "result": [{"title": "...", "price": "..."}, ...]} 或错误 dict。
    """
    u = _u()
    u.dump()
    out = u.last_result
    if not out.get("ok"):
        return out
    payload = out.get("result") or {}
    xml_str = payload.get("xml")
    if not xml_str:
        return {"ok": False, "error": "no_xml", "detail": "dump 结果中无 xml 字段"}
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError as e:
        return {"ok": False, "error": "parse_error", "detail": str(e)}
    items = _collect_text_nodes(root)
    products = _group_into_products(items, limit=limit)
    return {"ok": True, "result": products}


def _bounds_attrs(node: ET.Element) -> tuple:
    b = _parse_bounds(node.get("bounds") or "")
    if not b or len(b) < 4:
        return (0, 0, 0, 0)
    return b


def _bounds_str(node: ET.Element) -> str:
    """返回节点 bounds 的规范字符串，用于 xml_node_id（同屏内唯一）。"""
    raw = (node.get("bounds") or "").strip()
    if raw:
        return raw
    b = _bounds_attrs(node)
    if b != (0, 0, 0, 0):
        return f"[{b[0]},{b[1]}][{b[2]},{b[3]}]"
    return ""


def _text_in_card(card: ET.Element) -> List[Dict[str, Any]]:
    """收集卡片内所有带 text 的节点。"""
    out: List[Dict[str, Any]] = []
    for n in card.iter():
        t = (n.get("text") or "").strip()
        if not t:
            continue
        out.append({
            "text": t,
            "content_desc": (n.get("content-desc") or "").strip(),
            "resource_id": (n.get("resource-id") or "").strip(),
        })
    return out


def _extract_store_from_texts(text_nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """从顶部区域文本节点中提取店铺信息。"""
    store: Dict[str, Any] = {
        "store_name": "",
        "total_sales": "",
        "good_reviews": "",
        "recommend_text": "",
        "visit_count": "",
        "guarantees": [],
    }
    for item in text_nodes:
        t = item.get("text") or ""
        if not t:
            continue
        if re.search(r"全店总售.+", t):
            store["total_sales"] = t
        elif "好评" in t and t not in store["good_reviews"]:
            store["good_reviews"] = t
        elif "拼单多次并推荐" in t:
            store["recommend_text"] = t
        elif "人逛过该店铺" in t:
            store["visit_count"] = t
        elif t in ("7天无理由退货", "全场包邮"):
            if t not in store["guarantees"]:
                store["guarantees"].append(t)
        elif t not in ("关注", "客服", "分享", "保障", "全部商品", "分类", "店铺保障") and re.search(r".*店$", t) and len(t) > 4 and not store["store_name"]:
            store["store_name"] = t
    return store


# 百亿补贴标签：标题所在行为固定尺寸 [536x66]，且该行内除 tv_title 外还有 RecyclerView（图标区）
BAIYI_TAG_WIDTH_MIN, BAIYI_TAG_WIDTH_MAX = 400, 700
BAIYI_TAG_HEIGHT_MIN, BAIYI_TAG_HEIGHT_MAX = 40, 100


def _card_has_baiyi_subsidy_tag(card: ET.Element, title_node: ET.Element) -> bool:
    """检测该商品是否为百亿补贴：标题的父节点为固定尺寸 536x66 且该行内包含 RecyclerView。"""
    parent_map = {c: p for p in card.iter() for c in p}
    parent = parent_map.get(title_node)
    if parent is None:
        return False
    b = _bounds_attrs(parent)
    if len(b) < 4:
        return False
    w = b[2] - b[0]
    h = b[3] - b[1]
    if not (BAIYI_TAG_WIDTH_MIN <= w <= BAIYI_TAG_WIDTH_MAX and BAIYI_TAG_HEIGHT_MIN <= h <= BAIYI_TAG_HEIGHT_MAX):
        return False
    for child in parent:
        cls = (child.get("class") or "").strip()
        if "RecyclerView" in cls:
            return True
    return False


def _extract_product_from_card(card: ET.Element, title_node: ET.Element) -> Dict[str, Any]:
    """从商品卡节点及其中的 tv_title 节点提取一条商品信息。"""
    title_short = (title_node.get("text") or "").strip()
    title_full = (title_node.get("content-desc") or "").strip() or title_short
    title_b = _bounds_attrs(title_node)
    rows = _text_in_card(card)
    price_str: Optional[str] = None
    sales_str: Optional[str] = None
    tags: List[str] = []
    if _card_has_baiyi_subsidy_tag(card, title_node):
        tags.append("百亿补贴")
    tag_patterns = ("正品险", "假一赔十", "即将售罄", "国内现货", "24小时发货")
    sales_re = re.compile(r"已抢\d+件")
    only_re = re.compile(r"仅剩\d+件")
    collect_re = re.compile(r"\d+人收藏")
    saw_yuan = False
    for r in rows:
        t = (r.get("text") or "").strip()
        if not t:
            continue
        if r.get("content_desc") == SALES_DESC or sales_re.match(t):
            sales_str = t
        elif t == "¥":
            saw_yuan = True
        elif saw_yuan and price_str is None and re.match(r"^\d+\.?\d*$", t):
            price_str = "¥" + t
            saw_yuan = False
        elif only_re.match(t) or collect_re.match(t):
            tags.append(t)
        else:
            for p in tag_patterns:
                if p in t and p not in tags:
                    tags.append(p)
                    break
    if price_str is None:
        for r in rows:
            t = (r.get("text") or "").strip()
            if re.match(r"^\d+\.?\d*$", t) and len(t) <= 10:
                price_str = "¥" + t
                break
    return {
        "title": title_full or title_short,
        "title_short": title_short,
        "price": price_str,
        "sales": sales_str,
        "tags": tags,
    }


def parse_store_page_xml(xml_str: str) -> Dict[str, Any]:
    """
    解析拼多多店铺首页 hierarchy XML，提取店铺信息与全部商品。
    返回 {"store": {...}, "products": [...]}。
    可传入 dump 得到的 xml 字符串或从文件读取的内容。
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return {"store": {}, "products": []}
    package = "com.xunmeng.pinduoduo"
    # 顶部区域文本（店铺信息）
    header_texts: List[Dict[str, Any]] = []
    # 所有 tv_title 节点 -> 对应商品卡
    title_nodes: List[ET.Element] = []
    for node in root.iter():
        if node.get("package") != package:
            continue
        bounds = _bounds_attrs(node)
        if len(bounds) >= 4:
            y1 = bounds[1]
            text = (node.get("text") or "").strip()
            if y1 < STORE_HEADER_Y_MAX and text:
                header_texts.append({"text": text, "bounds": bounds})
            rid = (node.get("resource-id") or "").strip()
            if rid == TITLE_RID and text:
                title_nodes.append(node)
    store = _extract_store_from_texts(header_texts)
    products: List[Dict[str, Any]] = []
    for title_node in title_nodes:
        title_b = _bounds_attrs(title_node)
        card = None
        for n in root.iter():
            if n.get("class") != "android.view.ViewGroup" or n.get("package") != package:
                continue
            b = _bounds_attrs(n)
            if len(b) < 4:
                continue
            h = b[3] - b[1]
            if h < PRODUCT_CARD_MIN_HEIGHT or h > 900:
                continue
            # 是否包含 title_node 的 bounds（title 在此节点内）
            if b[0] <= title_b[0] and b[1] <= title_b[1] and b[2] >= title_b[2] and b[3] >= title_b[3]:
                # 取最小包含的 card（子 ViewGroup 可能也包含，取当前为候选）
                if card is None or (b[3] - b[1]) < (_bounds_attrs(card)[3] - _bounds_attrs(card)[1]):
                    card = n
        if card is not None:
            prod = _extract_product_from_card(card, title_node)
            prod["xml_node_id"] = _bounds_str(card)  # 同屏内唯一，用于判重（不依赖标题价格）
            prod["xml_node_index"] = card.get("index") or ""
            products.append(prod)
        else:
            products.append({
                "title": (title_node.get("content-desc") or title_node.get("text") or "").strip(),
                "title_short": (title_node.get("text") or "").strip(),
                "price": None,
                "sales": None,
                "tags": [],
                "xml_node_id": _bounds_str(title_node),
                "xml_node_index": title_node.get("index") or "",
            })
    return {"store": store, "products": products}


def dump_store_page(
    store_keyword: Optional[str] = None,
    *,
    max_products_to_try: int = 20,
    store_scroll_no_new_limit: int = 5,
    end_marker: str = STORE_END_MARKER,
    output_csv: Optional[str] = None,
    no_dedup: bool = True,
    max_scrolls: int = 5,
    max_products_scrolls: Optional[int] = None,
) -> dict:
    """
    若提供 store_keyword：search_store(店铺名) 进入目标店铺 → 滑到底 dump 并合并 → 结构化（可输出 CSV）。
    流程：ensure_search_page → 点「店铺」→ 输入关键词 → 点「搜索」→ 店铺列表找目标并点击 → scroll_store_to_end。
    no_dedup=True（默认）：商品不去重、不合并，每个都记录且每个都点进购物车取备注。
    若不提供 store_keyword：仅从当前页 dump 并解析店铺首页。
    返回 {"ok": True, "result": {"store", "products", "rows"?}} 或错误 dict。
    """
    if store_keyword is None or store_keyword.strip() == "":
        _log("dump_store_page: 无 store_keyword，仅解析当前页")
        u = _u()
        u.dump()
        out = u.last_result
        if not out.get("ok"):
            return out
        payload = out.get("result") or {}
        xml_str = payload.get("xml")
        if not xml_str:
            return {"ok": False, "error": "no_xml", "detail": "dump 结果中无 xml 字段"}
        parsed = parse_store_page_xml(xml_str)
        return {"ok": True, "result": parsed}

    _log(f"dump_store_page: 开始（店铺搜索流程），目标={store_keyword!r}，no_dedup={no_dedup}")
    search_out = search_store(store_keyword.strip())
    if not search_out.get("ok"):
        _log(f"dump_store_page: search_store 失败 {search_out}")
        if search_out.get("error") in ("no_search_elements", "tab_not_found"):
            _log("dump_store_page: 搜索页异常（未解析到搜索框/按钮或未找到 tab），重启拼多多后返回")
            open_app(stop=True)
            time.sleep(1.2)
        return search_out
    _log("dump_store_page: 已进入目标店铺，等待 1.2s 后 scroll_store_to_end...")
    time.sleep(1.2)
    scroll_out = scroll_store_to_end(
        no_new_limit=store_scroll_no_new_limit,
        end_marker=end_marker,
        no_dedup=no_dedup,
        max_cart_scrolls=max_scrolls,
        max_products_scrolls=max_products_scrolls,
    )
    if not scroll_out.get("ok"):
        _log(f"dump_store_page: scroll_store_to_end 失败: {scroll_out}")
        return scroll_out
    merged = (scroll_out.get("result") or {}).get("merged") or {}
    store = merged.get("store") or {}
    products = merged.get("products") or []
    rows = to_csv_rows(store, products)
    if output_csv:
        _log(f"dump_store_page: 写入 CSV {output_csv}")
        write_store_csv(rows, output_csv)
    _log(f"dump_store_page: 完成 store={bool(store)} products={len(products)} rows={len(rows)}")
    return {
        "ok": True,
        "result": {
            "store": store,
            "products": products,
            "rows": rows,
        },
    }


def dump_stores_to_csv(
    store_keywords: List[str],
    output_csv: str,
    *,
    max_products_to_try: int = 20,
    store_scroll_no_new_limit: int = 5,
    end_marker: str = STORE_END_MARKER,
    no_dedup: bool = True,
) -> dict:
    """
    依次对多个店铺执行 dump_store_page，将各店 rows 合并后写入一个 CSV。
    no_dedup=True（默认）：每店商品不去重，每个商品都记录且都点进购物车取备注。
    单店失败时跳过并记录，不中断后续店铺；汇总 CSV 仅含成功店铺数据。
    返回 {"ok": True, "result": {"success_stores", "failed_stores", "total_rows", "output_csv"}}。
    """
    all_rows: List[Dict[str, Any]] = []
    success_stores: List[str] = []
    failed_stores: List[Dict[str, Any]] = []  # [{"store": kw, "error": ..., "detail": ...}, ...]
    # 默认先重启 app，从干净首页开始，避免残留页面导致搜索框/tab 解析失败
    _log("dump_stores_to_csv: 重启拼多多，从首页开始...")
    open_app(stop=True)
    time.sleep(1.2)
    for i, kw in enumerate(store_keywords):
        kw = (kw or "").strip()
        if not kw:
            continue
        # 从第二家店铺起：先重启 app 回到首页，再搜下一家，否则当前仍在上一家店铺内无法进入搜索
        if i > 0:
            _log("dump_stores_to_csv: 返回首页以搜索下一店铺（重启拼多多）...")
            open_app(stop=True)
            time.sleep(3.2)
        _log(f"dump_stores_to_csv: 处理店铺 {kw!r} ({len(success_stores) + len(failed_stores) + 1}/{len(store_keywords)})")
        out = dump_store_page(
            store_keyword=kw,
            max_products_to_try=max_products_to_try,
            store_scroll_no_new_limit=store_scroll_no_new_limit,
            end_marker=end_marker,
            output_csv=None,
            no_dedup=no_dedup,
        )
        if out.get("ok"):
            rows = (out.get("result") or {}).get("rows") or []
            all_rows.extend(rows)
            success_stores.append(kw)
            _log(f"dump_stores_to_csv: 店铺 {kw!r} 成功，rows={len(rows)}，累计 {len(all_rows)} 行")
        else:
            failed_stores.append({
                "store": kw,
                "error": out.get("error", "unknown"),
                "detail": out.get("detail", ""),
            })
            _log(f"dump_stores_to_csv: 店铺 {kw!r} 失败，跳过: {out.get('error')} {out.get('detail', '')}")
            if out.get("error") in ("no_search_elements", "tab_not_found"):
                _log("dump_stores_to_csv: 搜索页异常（未解析到搜索框/按钮或未找到 tab），重启拼多多以便下一店铺正常搜索")
                open_app(stop=True)
                time.sleep(1.2)
    if all_rows:
        _log(f"dump_stores_to_csv: 写入汇总 CSV {output_csv}，共 {len(all_rows)} 行")
        write_store_csv(all_rows, output_csv)
    else:
        _log("dump_stores_to_csv: 无成功店铺数据，不写入 CSV")
    return {
        "ok": True,
        "result": {
            "success_stores": success_stores,
            "failed_stores": failed_stores,
            "total_rows": len(all_rows),
            "output_csv": output_csv if all_rows else None,
        },
    }


def dump_store_page_by_product(
    store_keyword: Optional[str] = None,
    *,
    max_products_to_try: int = 20,
    max_detail_scrolls: int = 5,
    store_scroll_no_new_limit: int = 5,
    end_marker: str = STORE_END_MARKER,
    output_csv: Optional[str] = None,
) -> dict:
    """
    若提供 store_keyword：完整流程——
      1. 打开搜索页（ensure_search_page）
      2. 搜索 store_keyword（商品列表）
      3. 依次点击商品进详情，dump_product_detail；找到店铺名匹配目标则点「进店」
      4. 进店后下滑到结尾（连续滑不动或出现 end_marker），过程中多次 dump 并合并
      5. 结构化为 store + products + rows（可输出 CSV）
    若不提供 store_keyword：仅从当前页 dump 并解析店铺首页（兼容旧用法）。
    返回 {"ok": True, "result": {"store", "products", "rows"?}} 或错误 dict。
    """
    if store_keyword is None or store_keyword.strip() == "":
        _log("dump_store_page: 无 store_keyword，仅解析当前页")
        u = _u()
        u.dump()
        out = u.last_result
        if not out.get("ok"):
            return out
        payload = out.get("result") or {}
        xml_str = payload.get("xml")
        if not xml_str:
            return {"ok": False, "error": "no_xml", "detail": "dump 结果中无 xml 字段"}
        parsed = parse_store_page_xml(xml_str)
        return {"ok": True, "result": parsed}

    # 完整流程：搜索商品 → 点商品找进店 → 进店后滑到底并采集
    _log(f"dump_store_page_by_product: 开始完整流程，目标店铺关键词={store_keyword!r}")
    page_out = ensure_search_page()
    if not page_out.get("ok"):
        _log(f"dump_store_page_by_product: ensure_search_page 失败 {page_out}")
        return page_out
    page_el = page_out.get("result") or {}
    _log("dump_store_page_by_product: 切换到「商品」搜索")
    select_search_tab(store=False)
    time.sleep(1)
    search(
        store_keyword.strip(),
        search_input=page_el.get("search_input"),
        search_button=page_el.get("search_button"),
    )
    time.sleep(1.2)
    u = _u()
    seen_centers: Set[Tuple[int, int]] = set()
    targets: List[Dict[str, Any]] = []
    win = u.window_size()
    w = (win.get("result") or {}).get("width") or 540
    h = (win.get("result") or {}).get("height") or 960
    fx, fy = w // 2, int(h * 0.7)
    tx, ty = w // 2, int(h * 0.3)
    max_scrolls = 8
    for scroll_round in range(max_scrolls):
        targets_out = get_product_click_targets(limit=max_products_to_try)
        if not targets_out.get("ok"):
            _log(f"dump_store_page: get_product_click_targets 失败: {targets_out}")
            if not targets:
                return targets_out
            break
        new_list = targets_out.get("result") or []
        added = 0
        for t in new_list:
            cen = t.get("center")
            if not cen:
                continue
            key = (cen[0] // 40 * 40, cen[1] // 40 * 40)
            if key in seen_centers:
                continue
            seen_centers.add(key)
            targets.append(t)
            added += 1
        if len(targets) >= max_products_to_try:
            break
        if scroll_round < max_scrolls - 1:
            u.swipe(fx, fy, tx, ty, duration=0.3)
            time.sleep(1.0)
    targets = targets[:max_products_to_try]
    entered = False
    for idx, t in enumerate(targets):
        cx, cy = t.get("center") or (0, 0)
        u.click(x=cx, y=cy)
        time.sleep(1.2)
        detail_out = dump_product_detail(max_scrolls=max_detail_scrolls)
        if not detail_out.get("ok"):
            u.press("back")
            time.sleep(1.0)
            continue
        detail = detail_out.get("result") or {}
        store_block = detail.get("store") or {}
        store_name = (store_block.get("name") or "").strip()
        if store_name and (store_keyword in store_name or store_name in store_keyword):
            cen = store_block.get("enter_center")
            if cen:
                u.click(x=cen[0], y=cen[1])
                entered = True
                break
        u.press("back")
        time.sleep(1.0)
    if not entered:
        _log("dump_store_page: 未找到目标店铺，退出")
        return {
            "ok": False,
            "error": "store_not_found",
            "detail": f"在 {max_products_to_try} 个商品详情页未找到目标店铺「{store_keyword}」",
        }
    time.sleep(1.2)
    scroll_out = scroll_store_to_end(no_new_limit=store_scroll_no_new_limit, end_marker=end_marker)
    if not scroll_out.get("ok"):
        _log(f"dump_store_page: scroll_store_to_end 失败: {scroll_out}")
        return scroll_out
    merged = (scroll_out.get("result") or {}).get("merged") or {}
    store = merged.get("store") or {}
    products = merged.get("products") or []
    rows = to_csv_rows(store, products)
    if output_csv:
        _log(f"dump_store_page: 写入 CSV {output_csv}")
        write_store_csv(rows, output_csv)
    _log(f"dump_store_page: 完成 store={bool(store)} products={len(products)} rows={len(rows)}")
    return {
        "ok": True,
        "result": {
            "store": store,
            "products": products,
            "rows": rows,
        },
    }


if __name__ == "__main__":
    _repo_root = Path(__file__).resolve().parent.parent
    if str(_repo_root) not in sys.path:
        sys.path.insert(0, str(_repo_root))
    from pdd_skills_cli import main
    sys.exit(main())
