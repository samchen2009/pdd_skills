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
        if name == target_store_name:
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
            if name == target_store_name:
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
    use_detail_flow: bool = False,
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
    # 进入前先关掉可能存在的运行中干扰弹窗（如「确定放弃吗?」「先去逛逛」）
    for _ in range(2):
        u.dump()
        out = u.last_result
        if not out.get("ok"):
            break
        xml_str = (out.get("result") or {}).get("xml") or ""
        if not _is_intrusive_popup(xml_str):
            break
        _dismiss_intrusive_popup(u, w, h)
        time.sleep(0.3)
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
        # 若为运行中干扰弹窗则关闭，再 dump 一次用新界面解析
        for _ in range(2):
            if not _is_intrusive_popup(xml_str):
                break
            _dismiss_intrusive_popup(u, w, h)
            time.sleep(0.3)
            u.dump()
            out = u.last_result
            if not out.get("ok"):
                _log("scroll_store_to_end: 关弹窗后 dump 失败")
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
            if use_detail_flow:
                _enrich_products_via_detail_flow(u, tmp, w, h, max_cart_scrolls=max_cart_scrolls)
            else:
                _enrich_cart_remark_for_products(u, tmp, w, h, max_cart_scrolls=max_cart_scrolls)
        parsed_list.append(tmp)
        for j, p in enumerate(tmp):
            tit = (p.get("title_short") or p.get("title") or "").strip()[:40]
            pr = p.get("price")
            _log(f"scroll_store_to_end:   [{j}] {tit!r} price={pr!r}")
        # BUG FIX: 应该用本屏所有有价商品作比较，而不是本屏新增商品
        # 否则当 tmp=[] 时，下一屏的任何商品都会被误认为"新"商品，导致无限循环
        parsed_in_last_screen = [
            p for p in parsed_in_current_screen
            if _normalize_price_for_key(p.get("price"))
        ]

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


# 运行中干扰弹窗（如「确定放弃吗?」「先去逛逛」），与购物车弹窗区分，需点击关闭/先去逛逛
_INTRUSIVE_POPUP_MARKERS = ("确定放弃吗?", "先去逛逛")

# 底部优惠/专区条（如「专区满49减5」）盖住加购按钮，不点击，用 swipe 绕过（参考 mocks/coupon.xml）
_BOTTOM_COUPON_PATTERN = re.compile(r"专区|满\d+减\d+", re.IGNORECASE)
BOTTOM_ZONE_Y_RATIO = 0.85  # 屏幕高度比例，以上视为底部区域


def _has_bottom_coupon_bar(xml_str: str, screen_h: int, package: str = PDD_PACKAGE) -> bool:
    """
    检测底部是否存在「专区满X减Y」类按钮（盖住购物车加号）。仅当节点在屏幕底部区域（y >= BOTTOM_ZONE_Y_RATIO * screen_h）
    且 text/content-desc 匹配专区/满X减Y 时返回 True，避免把顶部 tab「满49减5专区」误判为底部条。
    """
    if not xml_str or screen_h <= 0:
        return False
    bottom_y_min = int(screen_h * BOTTOM_ZONE_Y_RATIO)
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return False
    for n in root.iter():
        pkg = (n.get("package") or "").strip()
        if pkg and pkg != package:
            continue
        text = (n.get("text") or n.get("content-desc") or "").strip()
        if not text or not _BOTTOM_COUPON_PATTERN.search(text):
            continue
        b = _bounds_attrs(n)
        if len(b) < 4:
            continue
        # 节点在底部区域（上边 y1 进入底部即算）
        if b[1] >= bottom_y_min:
            return True
    return False


def _swipe_to_bypass_bottom_bar(u: Any, screen_w: int, screen_h: int) -> None:
    """在列表区向上滑动，试图把底部优惠条滑走或让加号露出，避免点到「专区满X减Y」按钮。"""
    x = screen_w // 2
    from_y = int(screen_h * 0.75)
    to_y = int(screen_h * 0.45)
    u.swipe(x, from_y, x, to_y, duration=0.25)
    time.sleep(0.4)


def _is_intrusive_popup(xml_str: str) -> bool:
    """当前 dump 是否为干扰弹窗（非购物车弹窗）。有「确定放弃吗?」或「先去逛逛」且非购物车即视为干扰弹窗。"""
    if not xml_str:
        return False
    is_cart = "关闭" in xml_str or any(h in xml_str for h in _CART_SECTION_HEADERS)
    if is_cart:
        return False
    return any(m in xml_str for m in _INTRUSIVE_POPUP_MARKERS)


def _find_popup_dismiss_target(
    xml_str: str, screen_w: int, screen_h: int, package: str = PDD_PACKAGE
) -> Optional[Tuple[int, int]]:
    """
    在干扰弹窗 XML 中找关闭点击坐标。优先「先去逛逛」按钮中心；否则找右上角可点击节点（如关闭 X）。
    返回 (x, y) 或 None。
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None
    # 1) 优先：含「先去逛逛」的节点（可点击的用其 bounds，否则用文案节点自身 bounds 中心）
    for n in root.iter():
        if n.get("package") != package:
            continue
        t = (n.get("text") or "").strip()
        if t != "先去逛逛":
            continue
        b = _parse_bounds(n.get("bounds") or "")
        if b and len(b) >= 4:
            return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
    for n in root.iter():
        if n.get("package") != package:
            continue
        if (n.get("clickable") or "").strip() != "true":
            continue
        for child in n.iter():
            if (child.get("text") or "").strip() == "先去逛逛":
                b = _parse_bounds(n.get("bounds") or "")
                if b and len(b) >= 4:
                    return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
                break
    # 2) 右上角可点击节点：右 15% 宽、上 35% 高，面积不宜过大
    right_min = int(screen_w * 0.85)
    top_max = int(screen_h * 0.35)
    best: Optional[Tuple[int, int, int, int]] = None
    for n in root.iter():
        if n.get("package") != package:
            continue
        if (n.get("clickable") or "").strip() != "true":
            continue
        b = _parse_bounds(n.get("bounds") or "")
        if not b or len(b) < 4:
            continue
        if b[2] < right_min or b[1] > top_max:
            continue
        area = (b[2] - b[0]) * (b[3] - b[1])
        if area < 400 or area > 120000:
            continue
        if best is None or (b[2], -b[1]) > (best[2], -best[1]):
            best = b
    if best:
        return ((best[0] + best[2]) // 2, (best[1] + best[3]) // 2)
    return None


def _dismiss_intrusive_popup(u: Any, screen_w: int, screen_h: int) -> bool:
    """
    若当前为干扰弹窗则关闭：dump → 检测 → 点「先去逛逛」或右上角关闭 → 返回 True；否则返回 False。
    最多尝试关闭一次（不在此内循环，由调用方决定是否再次 dump 后重试）。
    """
    u.dump()
    out = u.last_result
    if not out.get("ok"):
        return False
    xml_str = (out.get("result") or {}).get("xml") or ""
    if not _is_intrusive_popup(xml_str):
        return False
    target = _find_popup_dismiss_target(xml_str, screen_w, screen_h)
    if target:
        _log("运行中弹窗: 检测到干扰弹窗，点击关闭")
        u.click(x=target[0], y=target[1])
        time.sleep(0.6)
        return True
    _log("运行中弹窗: 检测到干扰弹窗但未找到关闭按钮，尝试 back")
    u.press("back")
    time.sleep(0.5)
    return True


def _dismiss_intrusive_popups_if_present(
    u: Any, screen_w: int, screen_h: int, max_tries: int = 3
) -> bool:
    """若当前存在干扰弹窗，循环关闭后再返回。"""
    dismissed_any = False
    for _ in range(max_tries):
        u.dump()
        out = u.last_result
        if not out.get("ok"):
            break
        xml_str = (out.get("result") or {}).get("xml") or ""
        if not _is_intrusive_popup(xml_str):
            break
        dismissed_any = True
        _dismiss_intrusive_popup(u, screen_w, screen_h)
        time.sleep(0.4)
    return dismissed_any


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
    """价格规范化为数字串，便于前后两屏同一商品能匹配（¥88 / 88 / 88.00 -> 88；¥12.5 / 12.50 -> 12.5）。"""
    if price is None:
        return ""
    s = str(price).strip().replace("¥", "").replace("￥", "").strip()
    m = re.match(r"^(\d+\.?\d*)", s)
    if not m:
        return s
    num_str = m.group(1)
    try:
        v = float(num_str)
        return str(int(v)) if v == int(v) else str(v)
    except (ValueError, OverflowError):
        return num_str


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
        # 若突然出现干扰弹窗（如关购物车后弹出的「确定放弃吗?」），先关掉再解析
        skip_product = False
        for _ in range(3):
            if not _is_intrusive_popup(fresh_xml):
                break
            _dismiss_intrusive_popup(u, screen_w, screen_h)
            time.sleep(0.3)
            u.dump()
            out = u.last_result
            if not out.get("ok"):
                _log("购物车: [商品] 关弹窗后 dump 失败，跳过该商品")
                skip_product = True
                break
            fresh_xml = (out.get("result") or {}).get("xml") or ""
        if skip_product:
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
        # 底部「专区满X减Y」条盖住加号时不点击，先 swipe 绕过再重试
        bottom_y_min = int(screen_h * BOTTOM_ZONE_Y_RATIO)
        for _bypass in range(3):
            if not _has_bottom_coupon_bar(fresh_xml, screen_h) or plus_bounds[1] < bottom_y_min:
                break
            _log("购物车: [商品] 检测到底部优惠条盖住加号，swipe 绕过")
            _swipe_to_bypass_bottom_bar(u, screen_w, screen_h)
            u.dump()
            out = u.last_result
            if not out.get("ok"):
                break
            fresh_xml = (out.get("result") or {}).get("xml") or ""
            if not fresh_xml:
                break
            try:
                fresh_root = ET.fromstring(fresh_xml)
            except ET.ParseError:
                break
            card = _find_card_for_product(fresh_root, p)
            if card is None:
                break
            plus_bounds = _find_plus_button_in_card(card)
            if not plus_bounds:
                break
        if _has_bottom_coupon_bar(fresh_xml, screen_h) and plus_bounds[1] >= bottom_y_min:
            _log("购物车: [商品] 绕过后仍被底部优惠条遮挡，跳过点击")
            continue
        if card is None or not plus_bounds:
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
        # 若突然出现干扰弹窗，先关掉再解析，避免点到弹窗上
        skip_product = False
        for _ in range(3):
            if not _is_intrusive_popup(fresh_xml):
                break
            _dismiss_intrusive_popup(u, screen_w, screen_h)
            time.sleep(0.3)
            u.dump()
            out = u.last_result
            if not out.get("ok"):
                _log("购物车: [tmp商品] 关弹窗后 dump 失败，跳过该商品")
                skip_product = True
                break
            fresh_xml = (out.get("result") or {}).get("xml") or ""
        if skip_product:
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
        # 底部「专区满X减Y」条盖住加号时不点击，先 swipe 绕过再重试
        bottom_y_min = int(screen_h * BOTTOM_ZONE_Y_RATIO)
        for _bypass in range(3):
            if not _has_bottom_coupon_bar(fresh_xml, screen_h) or plus_bounds[1] < bottom_y_min:
                break
            _log("购物车: [tmp商品] 检测到底部优惠条盖住加号，swipe 绕过")
            _swipe_to_bypass_bottom_bar(u, screen_w, screen_h)
            u.dump()
            out = u.last_result
            if not out.get("ok"):
                break
            fresh_xml = (out.get("result") or {}).get("xml") or ""
            if not fresh_xml:
                break
            try:
                fresh_root = ET.fromstring(fresh_xml)
            except ET.ParseError:
                break
            card = _find_card_for_product(fresh_root, p)
            if card is None:
                break
            plus_bounds = _find_plus_button_in_card(card)
            if not plus_bounds:
                break
        if _has_bottom_coupon_bar(fresh_xml, screen_h) and plus_bounds[1] >= bottom_y_min:
            _log("购物车: [tmp商品] 绕过后仍被底部优惠条遮挡，跳过点击")
            continue
        if card is None or not plus_bounds:
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
            "link": (p.get("link") or "").strip(),
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


# ----- 按店铺+商品列表进店搜商品、详情页分享复制链接、产出 banya_hotspots CSV -----


def _get_clipboard_pdd(u: Any) -> str:
    """读取剪贴板，直接使用 uiautomator2 的 d.clipboard（参考 xiaohongshu_skills）。"""
    try:
        dev = getattr(u, "_dev", None)
        d = getattr(dev, "_d", None) if dev else None
        if d is not None and hasattr(d, "clipboard"):
            text = d.clipboard
            if isinstance(text, str) and text.strip():
                if "no shell command implementation" not in text.lower() and "inaccessible" not in text.lower():
                    return text.strip()
    except Exception:
        pass
    return ""


def _node_click_debug(
    *,
    stage: str,
    center: Optional[Tuple[int, int]] = None,
    bounds: Optional[Tuple[int, int, int, int]] = None,
    node_class: str = "",
    text: str = "",
    content_desc: str = "",
    clickable: Optional[bool] = None,
    score: Optional[int] = None,
    extra: str = "",
) -> None:
    """打印点击目标的详细信息，便于定位误点。"""
    fields = [f"stage={stage}"]
    if center:
        fields.append(f"center={center}")
    if bounds:
        fields.append(f"bounds={bounds}")
    if node_class:
        fields.append(f"class={node_class}")
    if text:
        fields.append(f"text={text[:40]!r}")
    if content_desc:
        fields.append(f"desc={content_desc[:60]!r}")
    if clickable is not None:
        fields.append(f"clickable={clickable}")
    if score is not None:
        fields.append(f"score={score}")
    if extra:
        fields.append(extra)
    _log("click_debug: " + ", ".join(fields))


def _is_share_panel_open_xml(xml_str: str, screen_h: int) -> bool:
    """
    判断是否已打开分享面板（避免把详情页顶部「分享到拼小圈」误判为分享浮层）。
    规则：在屏幕中下部出现至少 2 个分享面板选项（如 复制链接/微信/QQ/朋友圈/更多）才算打开。
    """
    if not xml_str:
        return False
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return False
    bottom_min_y = int(screen_h * 0.45)
    option_hits: Set[str] = set()
    for n in root.iter():
        t = (n.get("text") or "").strip()
        d = (n.get("content-desc") or "").strip()
        shown = t or d
        if not shown:
            continue
        b = _parse_bounds(n.get("bounds") or "")
        if not b or len(b) < 4:
            continue
        if b[1] < bottom_min_y:
            continue
        if "复制链接" in shown:
            option_hits.add("复制链接")
        elif shown in ("微信", "QQ", "朋友圈", "更多", "复制"):
            option_hits.add(shown)
        elif ("微信" in shown) or ("QQ" in shown) or ("朋友圈" in shown):
            option_hits.add("social")
    return len(option_hits) >= 2


def _click_share_and_copy_link_pdd(u: Any, screen_w: int, screen_h: int) -> str:
    """
    当前在商品详情页：点击右上角分享 → 在下方浮层中找「复制链接」并点击（必要时右滑），再读剪贴板。
    返回链接字符串，失败返回空。
    """
    _dismiss_intrusive_popups_if_present(u, screen_w, screen_h)
    # 进入详情后可能仍在过渡动画，给分享按钮 3 次识别机会
    share_center: Optional[Tuple[int, int]] = None
    share_meta: Dict[str, Any] = {}
    for _ in range(3):
        u.dump()
        out = u.last_result
        if not out.get("ok"):
            time.sleep(0.35)
            continue
        xml_str = (out.get("result") or {}).get("xml") or ""
        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError:
            time.sleep(0.35)
            continue
        # 按 product1/2/3.xml：顶部工具栏内，content-desc="分享" 的 FrameLayout，bounds 约 [951,95][1066,228]
        # 先严格命中，再做轻量回退。
        best_score = -10**9
        for n in root.iter():
            if n.get("package") != PDD_PACKAGE:
                continue
            clickable = (n.get("clickable") or "").strip() == "true"
            desc = (n.get("content-desc") or "").strip()
            text = (n.get("text") or "").strip()
            if desc != "分享" and ("分享" not in desc and "分享" not in text):
                continue
            b = _parse_bounds(n.get("bounds") or "")
            if not b or len(b) < 4:
                continue
            # 顶栏右上角：y 在上部工具栏，x 在右侧
            if b[1] > int(screen_h * 0.15) or b[2] < int(screen_w * 0.85):
                continue
            cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
            score = cx * 10 - cy * 3 + (120 if desc == "分享" else 0) + (50 if clickable else 0)
            if score > best_score:
                best_score = score
                share_center = (cx, cy)
                share_meta = {
                    "bounds": b,
                    "class": (n.get("class") or "").strip(),
                    "text": text,
                    "desc": desc,
                    "clickable": clickable,
                    "score": score,
                }
        if share_center:
            break
        time.sleep(0.35)
    fallback = (int(screen_w * 0.935), int(screen_h * 0.067))
    if not share_center:
        # 回退：直接点右上分享固定区中心（product*.xml 约 [951,95][1066,228]）
        _node_click_debug(stage="share_fallback", center=fallback, extra="reason=no_share_node")
        _log(f"banya_hotspots: 未精确识别分享图标，回退点击右上角 {fallback}")
        u.click(x=fallback[0], y=fallback[1])
    else:
        _node_click_debug(
            stage="share",
            center=share_center,
            bounds=share_meta.get("bounds"),
            node_class=share_meta.get("class", ""),
            text=share_meta.get("text", ""),
            content_desc=share_meta.get("desc", ""),
            clickable=share_meta.get("clickable"),
            score=share_meta.get("score"),
        )
        _log("banya_hotspots: 点击分享")
        u.click(x=share_center[0], y=share_center[1])
    time.sleep(0.8)
    # 点击后验收：若分享面板未拉起，自动重试（同点位 + 轻微偏移 + 回退点）
    panel_open = False
    for retry_idx in range(4):
        u.dump()
        out = u.last_result
        if out.get("ok"):
            xml_now = (out.get("result") or {}).get("xml") or ""
            panel_open_now = _is_share_panel_open_xml(xml_now, screen_h)
            _log(f"click_debug: stage=share_panel_check, retry_idx={retry_idx}, open={panel_open_now}")
            if panel_open_now:
                panel_open = True
                break
        _dismiss_intrusive_popups_if_present(u, screen_w, screen_h)
        base = share_center or fallback
        if not base:
            base = fallback
        offset = [(0, 0), (-18, 0), (18, 0), (0, 18)][retry_idx]
        retry_pt = (base[0] + offset[0], base[1] + offset[1])
        _node_click_debug(stage="share_retry", center=retry_pt, extra=f"retry_idx={retry_idx}")
        u.click(x=retry_pt[0], y=retry_pt[1])
        time.sleep(0.7)
    if not panel_open:
        _log("banya_hotspots: 点击分享后未拉起分享面板，返回空链接")
        return ""
    # 浮层中找「复制链接」，可能需右滑
    for swipe_attempt in range(3):
        u.dump()
        out = u.last_result
        if not out.get("ok"):
            break
        xml_str = (out.get("result") or {}).get("xml") or ""
        try:
            root = ET.fromstring(xml_str)
        except ET.ParseError:
            break
        option_preview: List[str] = []
        copied = False
        for n in root.iter():
            t = (n.get("text") or "").strip()
            desc = (n.get("content-desc") or "").strip()
            shown = t or desc
            if shown and len(option_preview) < 8:
                option_preview.append(shown[:20])
            # 兼容不同文案：复制链接 / 复制 / 链接
            is_copy_link = (
                (t == "复制链接" or desc == "复制链接")
                or ("复制链接" in t or "复制链接" in desc)
                or (("复制" in t or "复制" in desc) and ("链接" in t or "链接" in desc))
                or (t == "复制" or desc == "复制")
            )
            if not is_copy_link:
                continue
            b = _parse_bounds(n.get("bounds") or "")
            if not b or len(b) < 4:
                continue
            cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
            _node_click_debug(
                stage="copy_link",
                center=(cx, cy),
                bounds=b,
                node_class=(n.get("class") or "").strip(),
                text=t,
                content_desc=desc,
                clickable=((n.get("clickable") or "").strip() == "true"),
            )
            _log("banya_hotspots: 点击复制链接")
            u.click(x=cx, y=cy)
            time.sleep(0.8)
            link = _get_clipboard_pdd(u)
            if link and "no shell command" not in link.lower() and "inaccessible" not in link.lower():
                return link.strip()
            copied = True
        _log(
            "click_debug: stage=copy_link_panel_scan, "
            f"swipe_attempt={swipe_attempt}, found_copy_node={copied}, "
            f"options={option_preview}"
        )
        # 未找到则右滑浮层再试
        u.swipe(int(screen_w * 0.7), int(screen_h * 0.7), int(screen_w * 0.3), int(screen_h * 0.7), duration=0.2)
        time.sleep(0.5)
    return ""


def _dismiss_share_panel_if_present(u: Any, screen_h: int) -> None:
    """若当前仍在分享浮层（可见「复制链接」），按一次 back 关闭。"""
    u.dump()
    out = u.last_result
    if not out.get("ok"):
        return
    xml_str = (out.get("result") or {}).get("xml") or ""
    if _is_share_panel_open_xml(xml_str, screen_h):
        _log("click_debug: stage=dismiss_share_panel, action=back")
        u.press("back")
        time.sleep(0.5)


def _open_cart_from_detail(u: Any, screen_w: int, screen_h: int) -> bool:
    """
    当前在商品详情页时，尝试点击右下角进入购物车/加购规格区。
    优先点击底部右侧带「购物车/加入购物车/选规格」语义的可点击节点；否则回退到右下角坐标。
    """
    _dismiss_intrusive_popups_if_present(u, screen_w, screen_h)
    u.dump()
    out = u.last_result
    if not out.get("ok"):
        return False
    xml_str = (out.get("result") or {}).get("xml") or ""
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return False
    # 按 product1/2/3.xml：底部购买区固定在 y=[2198,2354]，右侧 x=[388,1080]。
    # 优先找该区域内的 clickable ViewGroup（content-desc 常含“¥xx单独购买/免拼购买/团专享”，也可能仅“¥76”）。
    best_center: Optional[Tuple[int, int]] = None
    best_meta: Dict[str, Any] = {}
    best_score = -1
    candidates: List[Dict[str, Any]] = []
    right_min_x = int(screen_w * 0.35)
    bottom_min_y = int(screen_h * 0.9)
    for n in root.iter():
        if n.get("package") != PDD_PACKAGE:
            continue
        if n.get("class") != "android.view.ViewGroup":
            continue
        if (n.get("clickable") or "").strip() != "true":
            continue
        b = _parse_bounds(n.get("bounds") or "")
        if not b or len(b) < 4:
            continue
        x1, y1, x2, y2 = b
        # 必须命中右下购买区
        if x1 < right_min_x or y1 < bottom_min_y:
            continue
        cx, cy = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
        desc = (n.get("content-desc") or "").strip()
        bw, bh = x2 - x1, y2 - y1
        area = bw * bh
        if area < int(screen_w * screen_h * 0.005):
            continue
        # 语义优先，但 product1 可能只有“¥76”，所以只要含价格也算
        has_price = bool(re.search(r"¥\s*\d+(\.\d+)?", desc))
        has_buy_word = ("购买" in desc) or ("免拼" in desc) or ("专享" in desc)
        # 过滤“直接成团”这类非底部购买主按钮候选
        if ("成团" in desc) and not has_buy_word:
            continue
        if not has_price:
            continue
        # 在候选容器内部再找更精确的“购买语义子节点”点击点（如“仅剩8件 免拼购买”）
        sub_click_center: Optional[Tuple[int, int]] = None
        sub_click_bounds: Optional[Tuple[int, int, int, int]] = None
        sub_click_text = ""
        sub_click_score = -1
        for child in n.iter():
            ct = (child.get("text") or "").strip()
            cd = (child.get("content-desc") or "").strip()
            if not ct and not cd:
                continue
            cb = _parse_bounds(child.get("bounds") or "")
            if not cb or len(cb) < 4:
                continue
            # 子节点需在候选容器内
            if not (cb[0] >= x1 and cb[1] >= y1 and cb[2] <= x2 and cb[3] <= y2):
                continue
            txt = ct or cd
            if ("购买" not in txt) and ("免拼" not in txt) and ("团专享" not in txt) and ("仅剩" not in txt):
                continue
            ccx, ccy = (cb[0] + cb[2]) // 2, (cb[1] + cb[3]) // 2
            s = ccx * 10 + ccy  # 越靠右下越优先
            if s > sub_click_score:
                sub_click_score = s
                sub_click_center = (ccx, ccy)
                sub_click_bounds = cb
                sub_click_text = txt
        final_center = sub_click_center or (cx, cy)
        score = 0
        if has_price:
            score += 40
        if has_buy_word:
            score += 30
        # 多按钮时优先点更右侧（通常右侧是主要购买按钮）
        score += final_center[0] // 10
        if sub_click_center:
            score += 25
        candidates.append({
            "center": final_center,
            "bounds": b,
            "class": (n.get("class") or "").strip(),
            "text": (n.get("text") or "").strip(),
            "desc": desc,
            "clickable": True,
            "score": score,
            "extra": (
                f"has_price={has_price},has_buy_word={has_buy_word},area={area},"
                f"sub_click_text={sub_click_text!r},sub_click_bounds={sub_click_bounds},"
                f"source={'sub_node' if sub_click_center else 'container_center'}"
            ),
        })
        if score > best_score:
            best_score = score
            best_center = final_center
            best_meta = {
                "bounds": b,
                "class": (n.get("class") or "").strip(),
                "text": (n.get("text") or "").strip(),
                "desc": desc,
                "clickable": True,
                "score": score,
                "extra": (
                    f"has_price={has_price},has_buy_word={has_buy_word},area={area},"
                    f"sub_click_text={sub_click_text!r},sub_click_bounds={sub_click_bounds},"
                    f"source={'sub_node' if sub_click_center else 'container_center'}"
                ),
            }
    if candidates:
        top3 = sorted(candidates, key=lambda x: x.get("score", 0), reverse=True)[:3]
        for i, c in enumerate(top3, start=1):
            _node_click_debug(
                stage=f"open_cart_candidate_top{i}",
                center=c.get("center"),
                bounds=c.get("bounds"),
                node_class=c.get("class", ""),
                text=c.get("text", ""),
                content_desc=c.get("desc", ""),
                clickable=c.get("clickable"),
                score=c.get("score"),
                extra=c.get("extra", ""),
            )
    else:
        _log("click_debug: stage=open_cart_candidates, none")
    if best_center:
        _node_click_debug(
            stage="open_cart",
            center=best_center,
            bounds=best_meta.get("bounds"),
            node_class=best_meta.get("class", ""),
            text=best_meta.get("text", ""),
            content_desc=best_meta.get("desc", ""),
            clickable=best_meta.get("clickable"),
            score=best_meta.get("score"),
            extra=best_meta.get("extra", ""),
        )
        _log(f"详情页: 点击右下角入口 {best_center}")
        u.click(x=best_center[0], y=best_center[1])
        time.sleep(1.0)
        return True
    # 回退：购买区中心（与 product*.xml 区域一致）
    fallback = (int(screen_w * 0.68), int(screen_h * 0.95))
    _node_click_debug(stage="open_cart_fallback", center=fallback, extra="reason=no_bottom_buy_node")
    _log(f"详情页: 未识别到右下入口，最终回退点击 {fallback}")
    u.click(x=fallback[0], y=fallback[1])
    time.sleep(1.0)
    return True


def _center_from_xml_node_id(xml_node_id: str) -> Optional[Tuple[int, int]]:
    b = _parse_bounds((xml_node_id or "").strip())
    if not b or len(b) < 4:
        return None
    return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)


def _is_product_detail_page_xml(xml_str: str, screen_h: int) -> bool:
    """
    判断当前 XML 是否为商品详情页（用于避免在详情页误做“商品列表重定位”）。
    依据 product1~4.xml：顶部有「分享」，底部有「店铺/收藏/客服」工具栏。
    """
    if not xml_str:
        return False
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return False
    has_share_top = False
    has_bottom_actions = False
    bottom_y_min = int(screen_h * 0.85)
    for n in root.iter():
        if n.get("package") != PDD_PACKAGE:
            continue
        desc = (n.get("content-desc") or "").strip()
        b = _parse_bounds(n.get("bounds") or "")
        if not b or len(b) < 4:
            continue
        if desc == "分享" and b[1] <= int(screen_h * 0.2):
            has_share_top = True
        if desc in ("店铺", "收藏", "客服") and b[1] >= bottom_y_min:
            has_bottom_actions = True
        if has_share_top and has_bottom_actions:
            return True
    return False


def _ensure_on_product_list_page(
    u: Any, screen_w: int, screen_h: int, max_back: int = 3
) -> bool:
    """
    若当前仍在详情页，自动 back 回到商品列表页。返回 True 表示已不在详情页。
    """
    for _ in range(max_back):
        _dismiss_intrusive_popups_if_present(u, screen_w, screen_h)
        u.dump()
        out = u.last_result
        if not out.get("ok"):
            return False
        xml_str = (out.get("result") or {}).get("xml") or ""
        if not _is_product_detail_page_xml(xml_str, screen_h):
            return True
        u.press("back")
        time.sleep(0.8)
    u.dump()
    out = u.last_result
    if not out.get("ok"):
        return False
    xml_str = (out.get("result") or {}).get("xml") or ""
    return not _is_product_detail_page_xml(xml_str, screen_h)


def _find_product_center_on_current_screen(
    u: Any, prod: Dict[str, Any], screen_h: int
) -> Optional[Tuple[int, int]]:
    """
    在当前列表页重新匹配目标商品坐标，减少因回退后列表轻微位移导致点偏。
    优先按 name+price 匹配，失败时回退 xml_node_id 坐标。
    """
    u.dump()
    out = u.last_result
    if not out.get("ok"):
        return _center_from_xml_node_id((prod.get("xml_node_id") or "").strip())
    xml_str = (out.get("result") or {}).get("xml") or ""
    if not xml_str:
        return _center_from_xml_node_id((prod.get("xml_node_id") or "").strip())
    # 仍在详情页时不要解析“店铺商品列表”，否则会误点到详情内可点击模块（如“参与可直接成团”）
    if _is_product_detail_page_xml(xml_str, screen_h=screen_h):
        return None
    parsed = parse_store_page_xml(xml_str)
    current_products = parsed.get("products") or []
    for cp in current_products:
        if _same_product_by_name_price(prod, cp):
            cen = _center_from_xml_node_id((cp.get("xml_node_id") or "").strip())
            if cen:
                return cen
    return _center_from_xml_node_id((prod.get("xml_node_id") or "").strip())


def _enrich_products_via_detail_flow(
    u: Any,
    products: List[Dict[str, Any]],
    screen_w: int,
    screen_h: int,
    max_cart_scrolls: int = 2,
) -> None:
    """
    对当前屏新增商品逐个执行详情流程：
    1) 点击商品进详情；2) 分享并复制链接；3) 点右下角进购物车/规格区；
    4) 解析「最后X件」明细写入备注；最后返回商品列表页。
    """
    for p in products:
        if not _ensure_on_product_list_page(u, screen_w, screen_h):
            _log("详情流程: 未能回到商品列表页，跳过当前商品")
            continue
        center = _find_product_center_on_current_screen(u, p, screen_h)
        if not center:
            _log("详情流程: 当前不在商品列表页或未匹配到商品中心，跳过")
            continue
        title_short = (p.get("title_short") or p.get("title") or "").strip()[:30]
        _node_click_debug(
            stage="open_product",
            center=center,
            bounds=_parse_bounds((p.get("xml_node_id") or "").strip() or ""),
            text=title_short,
            content_desc=(p.get("title") or "").strip(),
            extra=f"price={p.get('price')!r}",
        )
        _log(f"详情流程: 打开商品 {title_short!r}")
        u.click(x=center[0], y=center[1])
        time.sleep(1.2)
        _dismiss_intrusive_popups_if_present(u, screen_w, screen_h)

        link = _click_share_and_copy_link_pdd(u, screen_w, screen_h)
        p["link"] = link or ""
        if link:
            _log(f"详情流程: 已复制链接，长度={len(link)}")
        _dismiss_share_panel_if_present(u, screen_h)
        _dismiss_intrusive_popups_if_present(u, screen_w, screen_h)

        remark = ""
        if _open_cart_from_detail(u, screen_w, screen_h):
            remark = _collect_cart_remark_with_scroll(u, screen_w, screen_h, max_scrolls=max_cart_scrolls)
            if remark:
                _log(f"详情流程: 缺货明细 {remark[:60]!r}")
            # 返回详情页（购物车弹窗/页）
            u.press("back")
            time.sleep(0.6)
        p["备注"] = remark or ""

        # 返回商品列表页
        u.press("back")
        time.sleep(1.0)
        _ensure_on_product_list_page(u, screen_w, screen_h)


def _bounds_contain(outer: Optional[Tuple[int, int, int, int]], inner: Optional[Tuple[int, int, int, int]]) -> bool:
    if not outer or len(outer) < 4 or not inner or len(inner) < 4:
        return False
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _find_store_search_box_center(
    xml_str: str, screen_h: int, screen_w: int = 1080, package: str = PDD_PACKAGE
) -> Optional[Tuple[int, int]]:
    """
    店铺首页顶栏「搜索」区域。
    参考 store.xml：右上角两个图标，左边是搜索、右边是分享。
    优先：text=搜索 或 content-desc=搜索。
    其次：先找 content-desc=分享 的节点，再在顶栏中找「在分享左侧」的可点击节点（即右上角左图标=搜索）。
    最后：顶栏中部可点击节点（中间空白搜索框）。
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None
    top_max_y = 300

    for n in root.iter():
        if n.get("package") != package:
            continue
        b = _parse_bounds(n.get("bounds") or "")
        if not b or len(b) < 4 or b[1] > top_max_y:
            continue
        t = (n.get("text") or "").strip()
        desc = (n.get("content-desc") or "").strip()
        if t != "搜索" and desc != "搜索":
            continue
        if (n.get("clickable") or "").strip() == "true":
            return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
        for parent in root.iter():
            if n in list(parent.iter()) and parent != n and (parent.get("clickable") or "").strip() == "true":
                pb = _parse_bounds(parent.get("bounds") or "")
                if pb and len(pb) >= 4 and pb[1] <= top_max_y:
                    return ((pb[0] + pb[2]) // 2, (pb[1] + pb[3]) // 2)
        return ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)

    # 右上角：先找「分享」图标（排除整屏根节点：只要 bounds 较小的，如宽高均 < 300），再找其左侧的可点击节点（即搜索图标 [873,931]）
    share_left: Optional[int] = None
    for n in root.iter():
        if n.get("package") != package:
            continue
        if (n.get("content-desc") or "").strip() != "分享":
            continue
        b = _parse_bounds(n.get("bounds") or "")
        if not b or len(b) < 4 or b[1] > top_max_y:
            continue
        w, h = b[2] - b[0], b[3] - b[1]
        if w > 300 or h > 300:
            continue
        share_left = b[0]
        break
    if share_left is not None:
        right_half = int(screen_w * 0.5)
        best_right = -1
        best_center: Optional[Tuple[int, int]] = None
        for n in root.iter():
            if n.get("package") != package:
                continue
            if (n.get("clickable") or "").strip() != "true":
                continue
            b = _parse_bounds(n.get("bounds") or "")
            if not b or len(b) < 4 or b[1] > top_max_y:
                continue
            if b[0] < right_half:
                continue
            if b[2] >= share_left - 5:
                continue
            if b[2] > best_right:
                best_right = b[2]
                best_center = ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
        if best_center:
            return best_center

    # 回退：顶栏中部可点击节点（中间空白搜索框）
    mid_x_min = int(screen_w * 0.12)
    mid_x_max = int(screen_w * 0.85)
    best: Optional[Tuple[int, int, int]] = None  # (cx, cy, width)
    for n in root.iter():
        if n.get("package") != package:
            continue
        if (n.get("clickable") or "").strip() != "true":
            continue
        b = _parse_bounds(n.get("bounds") or "")
        if not b or len(b) < 4 or b[1] > top_max_y:
            continue
        cx = (b[0] + b[2]) // 2
        if cx < mid_x_min or cx > mid_x_max:
            continue
        width = b[2] - b[0]
        if width < 100:
            continue
        if best is None or width > best[2]:
            best = (cx, (b[1] + b[3]) // 2, width)
    if best:
        return (best[0], best[1])
    return None


def _find_in_store_search_input_and_button(
    xml_str: str, top_max_y: int = 500, package: str = PDD_PACKAGE
) -> Tuple[Optional[Tuple[int, int]], Optional[Tuple[int, int]]]:
    """
    点击店铺内搜索框后的页面：找输入框中心与「搜索」按钮中心。
    返回 (input_center, button_center)，用于 search_in_store。
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return (None, None)
    input_center: Optional[Tuple[int, int]] = None
    button_center: Optional[Tuple[int, int]] = None
    for n in root.iter():
        if n.get("package") != package:
            continue
        b = _parse_bounds(n.get("bounds") or "")
        if not b or len(b) < 4 or b[1] > top_max_y:
            continue
        cls = (n.get("class") or "").strip()
        t = (n.get("text") or "").strip()
        desc = (n.get("content-desc") or "").strip()
        if "EditText" in cls or desc == "搜索":
            if not input_center:
                input_center = ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
        if t == "搜索" and (n.get("clickable") or "").strip() == "true":
            button_center = ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
            break
    if not button_center:
        for n in root.iter():
            if n.get("package") != package:
                continue
            b = _parse_bounds(n.get("bounds") or "")
            if not b or len(b) < 4 or b[1] > top_max_y:
                continue
            for c in n.iter():
                if c == n:
                    continue
                t = (c.get("text") or "").strip()
                if t == "搜索":
                    button_center = ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2)
                    break
            if button_center:
                break
    return (input_center, button_center)


def _find_first_product_poster_center(
    xml_str: str,
    product_keyword: str,
    expected_price: Optional[str] = None,
    package: str = PDD_PACKAGE,
) -> Optional[Tuple[int, int]]:
    """
    在店铺商品列表/搜索结果 XML 中找第一个标题包含 product_keyword（模糊匹配）且价格与 expected_price 相同（忽略货币符号）的商品卡，
    返回该卡「海报区」中心（卡片上半部分中心，避免点到加号）。
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None
    keyword = (product_keyword or "").strip()
    if not keyword:
        return None
    # 标题匹配：统一符号并做模糊匹配（避免“眼影/猫影”这类轻微差异导致漏匹配）
    def _norm_title(s: str) -> str:
        return (s or "").replace("\uff03", "#")  # fullwidth # -> #

    def _clean_for_match(s: str) -> str:
        s = _norm_title((s or "").lower())
        # 去掉空白和常见分隔符，提升“标题 vs 关键词”的可比性
        return re.sub(r"[\s#\-\|·•,，。!！?？:：;；'\"`~【】\[\]\(\)（）/\\_]+", "", s)

    def _bigrams(s: str) -> Set[str]:
        if not s:
            return set()
        if len(s) < 2:
            return {s}
        return {s[i : i + 2] for i in range(len(s) - 1)}

    def _title_match_score(k: str, txt: str) -> float:
        """返回 0~1 匹配分：包含命中优先，否则走 2-gram 重叠 + token 命中。"""
        k_clean = _clean_for_match(k)
        t_clean = _clean_for_match(txt)
        if not k_clean or not t_clean:
            return 0.0
        if k_clean in t_clean or t_clean in k_clean:
            return 1.0
        k2 = _bigrams(k_clean)
        if not k2:
            return 0.0
        t2 = _bigrams(t_clean)
        overlap = len(k2 & t2) / max(1, len(k2))
        # token 补充：按空格切词后，命中比例越高分越高
        raw_tokens = [x for x in re.split(r"\s+", k) if x]
        tokens = [_clean_for_match(x) for x in raw_tokens if _clean_for_match(x)]
        long_tokens = [x for x in tokens if len(x) >= 2]
        token_hit = 0
        for tk in long_tokens:
            if tk in t_clean:
                token_hit += 1
        token_score = (token_hit / len(long_tokens)) if long_tokens else 0.0
        return max(overlap, token_score * 0.9)
    keyword_norm = _norm_title(keyword)
    norm_expected = _normalize_price_for_key(expected_price) if expected_price else None
    _log(
        "match_product: 开始匹配 "
        f"keyword={keyword!r} -> norm={keyword_norm!r}, "
        f"expected_price={expected_price!r} -> norm={norm_expected!r}"
    )
    # 打印搜索结果商品预览，便于定位为何没匹配上
    try:
        page_preview = parse_store_page_xml(xml_str)
        preview_products = page_preview.get("products") or []
        if preview_products:
            preview_lines: List[str] = []
            for i, p in enumerate(preview_products[:8]):
                t = (p.get("title") or p.get("title_short") or "")[:40]
                pr = p.get("price")
                tg = ",".join((p.get("tags") or [])[:2])
                preview_lines.append(f"#{i+1} title={t!r} price={pr!r} tags={tg!r}")
            _log(f"match_product: 搜索结果商品预览({len(preview_products)}条，最多展示8条): {preview_lines}")
        else:
            _log("match_product: 搜索结果商品预览为空（parse_store_page_xml 未解析到商品）")
    except Exception as e:
        _log(f"match_product: 搜索结果商品预览解析异常: {e}")
    title_rid = TITLE_RID
    candidates: List[Tuple[ET.Element, Tuple[int, int]]] = []
    title_nodes_total = 0
    title_hits = 0
    price_mismatch_samples: List[str] = []
    no_card_samples: List[str] = []
    matched_title_samples: List[str] = []
    title_nonmatch_samples: List[str] = []
    for node in root.iter():
        if node.get("package") != package:
            continue
        rid = (node.get("resource-id") or "").strip()
        if rid != title_rid:
            continue
        title_nodes_total += 1
        t = (node.get("text") or "").strip()
        d = (node.get("content-desc") or "").strip()
        # 列表里是完整商品名，搜索结果里 text 常被截断，完整标题可能在 content-desc
        title_candidates = [x for x in (t, d) if x]
        if not title_candidates:
            continue
        best_score = 0.0
        for txt in title_candidates:
            score = _title_match_score(keyword_norm, txt)
            if score > best_score:
                best_score = score
        # 经验阈值：0.45 可覆盖轻微错字/符号差异，避免过宽误匹配
        if best_score < 0.45:
            if len(title_nonmatch_samples) < 8:
                title_nonmatch_samples.append(f"{(d or t)[:60]!r}(score={best_score:.2f})")
            continue
        title_hits += 1
        if len(matched_title_samples) < 5:
            matched_title_samples.append(f"{(d or t)[:60]!r}(score={best_score:.2f})")
        title_b = _parse_bounds(node.get("bounds") or "")
        if not title_b or len(title_b) < 4:
            continue
        # 店铺首页商品卡多为 ViewGroup，店内搜索结果页多为 FrameLayout（参考 search_result.xml）
        card = None
        for n in root.iter():
            if n.get("package") != package:
                continue
            cls = (n.get("class") or "").strip()
            if "ViewGroup" not in cls and "FrameLayout" not in cls:
                continue
            b = _parse_bounds(n.get("bounds") or "")
            if len(b) < 4:
                continue
            h = b[3] - b[1]
            if h < PRODUCT_CARD_MIN_HEIGHT or h > 900:
                continue
            if b[0] <= title_b[0] and b[1] <= title_b[1] and b[2] >= title_b[2] and b[3] >= title_b[3]:
                card_h = b[3] - b[1]
                if card is None:
                    card = n
                else:
                    cb = _parse_bounds(card.get("bounds") or "")
                    if cb and len(cb) >= 4 and card_h < (cb[3] - cb[1]):
                        card = n
        if card is not None:
            if norm_expected is not None:
                prod = _extract_product_from_card(card, node)
                card_price = prod.get("price")
                if _normalize_price_for_key(card_price) != norm_expected:
                    if len(price_mismatch_samples) < 5:
                        price_mismatch_samples.append(
                            f"title={(prod.get('title') or '')[:28]!r}, card_price={card_price!r}"
                        )
                    continue
            cb = _parse_bounds(card.get("bounds") or "")
            if cb and len(cb) >= 4:
                x1, y1, x2, y2 = cb
                h = y2 - y1
                poster_y2 = y1 + int(h * 0.55)
                cx = (x1 + x2) // 2
                cy = (y1 + poster_y2) // 2
                candidates.append((card, (cx, cy)))
        else:
            if len(no_card_samples) < 5:
                no_card_samples.append((d or t)[:80])
    _log(
        "match_product: 标题节点总数="
        f"{title_nodes_total}, 标题命中={title_hits}, "
        f"坐标候选={len(candidates)}"
    )
    if matched_title_samples:
        _log(f"match_product: 标题命中样例={matched_title_samples}")
    if no_card_samples:
        _log(f"match_product: 标题命中但未找到卡片样例={no_card_samples}")
    if price_mismatch_samples:
        _log(f"match_product: 价格不匹配样例={price_mismatch_samples}")
    if title_nonmatch_samples:
        _log(f"match_product: 标题未命中样例={title_nonmatch_samples}")
    if not candidates:
        # 明确失败原因，便于快速定位
        fail_reasons: List[str] = []
        if title_nodes_total == 0:
            fail_reasons.append("页面未识别到商品标题节点(TITLE_RID)")
        if title_nodes_total > 0 and title_hits == 0:
            fail_reasons.append("商品标题匹配失败(keyword 与标题不匹配)")
        if title_hits > 0 and no_card_samples:
            fail_reasons.append("标题命中但卡片容器识别失败")
        if title_hits > 0 and norm_expected is not None and price_mismatch_samples:
            fail_reasons.append("标题命中但价格过滤不通过(expected_price 不一致)")
        if not fail_reasons:
            fail_reasons.append("未命中坐标（可能页面结构变化）")
        _log(f"match_product: 未匹配到可点击商品海报坐标，失败原因={fail_reasons}")
        return None
    _log(f"match_product: 命中坐标={candidates[0][1]}")
    return candidates[0][1]


def search_in_store(u: Any, product_keyword: str) -> bool:
    """
    当前已在店铺内且处于「点击了右上角搜索框」后的输入页时，在店内搜索商品。
    dump 当前页 → 解析输入框与「搜索」按钮 → 输入关键词并点击搜索。
    成功执行搜索返回 True，未找到搜索按钮返回 False。
    """
    u.dump()
    out = u.last_result
    if not out.get("ok"):
        return False
    xml_str = (out.get("result") or {}).get("xml") or ""
    in_center, btn_center = _find_in_store_search_input_and_button(xml_str)
    if not btn_center:
        _log("search_in_store: 未找到店内搜索按钮")
        return False
    search_input = {"center": in_center} if in_center else None
    search_button = {"center": btn_center}
    search(product_keyword, search_input=search_input, search_button=search_button)
    return True


def _find_product_poster_center_with_scroll(
    u: Any,
    product_keyword: str,
    expected_price: Optional[str],
    screen_w: int,
    screen_h: int,
    max_scrolls: int = 10,
) -> Optional[Tuple[int, int]]:
    """
    在店内搜索结果页查找商品卡海报坐标；若当前屏未命中则持续下滑继续查找。
    通过页面签名重复判定“基本到底”，避免只看首屏导致漏匹配。
    """
    seen_signatures: Set[str] = set()
    repeated_signature_count = 0
    for i in range(max_scrolls + 1):
        u.dump()
        out = u.last_result
        if not out.get("ok"):
            _log(f"match_product_scroll: dump 失败，停止 (round={i+1})")
            return None
        xml_str = (out.get("result") or {}).get("xml") or ""
        if not xml_str:
            _log(f"match_product_scroll: 无 xml，停止 (round={i+1})")
            return None

        # 若命中“搜索无商品结果页”，直接返回，不再继续下滑
        if _is_no_product_search_result(xml_str):
            _log("match_product_scroll: 命中无商品结果页（没有更多商品了/其他店铺精选推荐），直接返回未找到")
            return None

        # 每一屏都尝试匹配一次
        cen = _find_first_product_poster_center(xml_str, product_keyword, expected_price=expected_price)
        if cen:
            _log(f"match_product_scroll: 第 {i+1} 屏命中坐标 {cen}")
            return cen

        # 页面签名：前几条标题+价格，若连续重复，认为到底
        try:
            parsed = parse_store_page_xml(xml_str)
            products = parsed.get("products") or []
            sig_parts: List[str] = []
            for p in products[:6]:
                t = (p.get("title_short") or p.get("title") or "")[:20]
                pr = str(p.get("price") or "")
                sig_parts.append(f"{t}|{pr}")
            signature = "||".join(sig_parts)
        except Exception:
            signature = ""

        if signature and signature in seen_signatures:
            repeated_signature_count += 1
            _log(f"match_product_scroll: 页面签名重复({repeated_signature_count})，继续尝试")
        else:
            if signature:
                seen_signatures.add(signature)
            repeated_signature_count = 0

        if i >= max_scrolls:
            break
        if repeated_signature_count >= 2:
            _log("match_product_scroll: 连续重复页面，判定已到底")
            break

        # 下滑到下一屏（小幅，减少跳过）
        u.swipe(screen_w // 2, int(screen_h * 0.72), screen_w // 2, int(screen_h * 0.33), duration=0.25)
        time.sleep(1.0)
    _log("match_product_scroll: 多屏查找后仍未命中")
    return None


def _is_no_product_search_result(xml_str: str, package: str = PDD_PACKAGE) -> bool:
    """
    判断是否为“店内搜索无商品”结果页（参考 mocks/no_product.xml）。
    特征文案：
    - 没有更多商品了
    - 其他店铺精选推荐
    两者同时出现时判定更可靠，避免误伤普通列表页。
    """
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return False
    has_no_more = False
    has_other_store_reco = False
    for n in root.iter():
        if n.get("package") != package:
            continue
        t = (n.get("text") or "").strip()
        if not t:
            continue
        if t == "没有更多商品了":
            has_no_more = True
        elif "其他店铺精选推荐" in t:
            has_other_store_reco = True
        if has_no_more and has_other_store_reco:
            return True
    return False


def dump_products_by_list(
    product_list: List[Dict[str, Any]],
    *,
    output_csv: Optional[str] = None,
) -> dict:
    """
    按列表进店搜商品、进详情、分享复制链接，最后生成 banya_hotspots_YYMMDD.csv。
    product_list 每项为 {"store": str, "product": str, "price": str}。
    流程：1) 按 store 排序  2) 对每个 store：search_store 进店 → 点右上角搜索 → 搜 product → 点海报进详情 →
    点分享 → 复制链接 → 记录 → 返回；重复该 store 下其余 product；再下一家 store。
    """
    from datetime import datetime
    _log("dump_products_by_list: 重启拼多多，从首页开始...")
    open_app(stop=True)
    time.sleep(1.2)
    u = _u()
    win = u.window_size()
    if not win.get("ok"):
        return {"ok": False, "error": "window_size", "detail": str(win)}
    w = (win.get("result") or {}).get("width") or 540
    h = (win.get("result") or {}).get("height") or 960
    rows: List[Dict[str, Any]] = []
    normalized: List[Dict[str, Any]] = []
    for item in product_list:
        store = (item.get("store") or "").strip()
        product = (item.get("product") or "").strip()
        price = (item.get("price") or "").strip()
        if not store or not product:
            continue
        normalized.append({"store": store, "product": product, "price": price})
    normalized.sort(key=lambda x: (x["store"], x["product"]))
    by_store: Dict[str, List[Dict[str, Any]]] = {}
    for item in normalized:
        s = item["store"]
        if s not in by_store:
            by_store[s] = []
        by_store[s].append(item)
    _log(f"dump_products: 共 {len(normalized)} 条，{len(by_store)} 家店铺")
    _log(f"dump_products: 店铺顺序={list(by_store.keys())}")
    first_store = True
    for store_name, items in by_store.items():
        _log(f"dump_products: 店铺 {store_name!r}，商品数 {len(items)}")
        # 从第二家店起：当前在上家店铺首页，search_store 需要应用级搜索页，先多次返回回到首页
        if not first_store:
            _log("dump_products: 返回应用首页以便搜索下一家店铺")
            for _ in range(4):
                u.press("back")
                time.sleep(0.5)
            time.sleep(0.5)
        first_store = False
        out = search_store(store_name)
        if not out.get("ok"):
            _log(f"dump_products: search_store 失败 {out}，跳过当前店铺继续下一家")
            for it in items:
                rows.append({"store": store_name, "product": it["product"], "price": it["price"], "link": ""})
            continue
        time.sleep(1.2)
        store_skip = False
        for idx, it in enumerate(items):
            product_keyword = it["product"]
            price_str = it["price"]
            _log(f"dump_products: [{store_name}] 搜索商品 {product_keyword!r}")
            # 同一店铺内：第一个商品从店铺首页点搜索框进入；后续商品从搜索结果页返回一次到搜索输入页即可，无需回首页再点搜索
            if idx > 0:
                u.press("back")
                time.sleep(0.6)
            else:
                u.dump()
                out = u.last_result
                if not out.get("ok"):
                    rows.append({"store": store_name, "product": product_keyword, "price": price_str, "link": ""})
                    continue
                xml_str = (out.get("result") or {}).get("xml") or ""
                cen = _find_store_search_box_center(xml_str, h, w)
                if not cen:
                    _log("dump_products: 未找到店铺内搜索框，跳过当前店铺继续下一家")
                    rows.append({"store": store_name, "product": product_keyword, "price": price_str, "link": ""})
                    for rest in items[idx + 1 :]:
                        rows.append({"store": store_name, "product": rest["product"], "price": rest["price"], "link": ""})
                    store_skip = True
                    break
                u.click(x=cen[0], y=cen[1])
                time.sleep(0.8)
            if not search_in_store(u, product_keyword):
                rows.append({"store": store_name, "product": product_keyword, "price": price_str, "link": ""})
                for rest in items[idx + 1 :]:
                    rows.append({"store": store_name, "product": rest["product"], "price": rest["price"], "link": ""})
                _log("dump_products: 店内搜索失败，跳过当前店铺继续下一家")
                store_skip = True
                break
            time.sleep(1.2)
            poster_cen = _find_product_poster_center_with_scroll(
                u,
                product_keyword=product_keyword,
                expected_price=price_str,
                screen_w=w,
                screen_h=h,
                max_scrolls=10,
            )
            if not poster_cen:
                _log(f"dump_products: 未找到商品 {product_keyword!r} 卡片，记录空链接并继续下一个商品")
                rows.append({"store": store_name, "product": product_keyword, "price": price_str, "link": ""})
                continue
            u.click(x=poster_cen[0], y=poster_cen[1])
            time.sleep(1.5)
            link = _click_share_and_copy_link_pdd(u, w, h)
            rows.append({"store": store_name, "product": product_keyword, "price": price_str, "link": link or ""})
            if link:
                _log(f"dump_products: 链接长度 {len(link)}")
            u.press("back")
            time.sleep(0.8)
            # 同一店铺还有下一个商品时只回到搜索结果页；最后一个商品再返回一次回到店铺首页
            if idx >= len(items) - 1:
                u.press("back")
                time.sleep(0.6)
        if store_skip:
            _log(f"dump_products: 店铺 {store_name!r} 已提前结束，准备进入下一家店铺")
            continue
        _log(f"dump_products: 店铺 {store_name!r} 处理完成，进入下一家店铺")
    out_path = output_csv
    if not out_path:
        out_path = "banya_hotspots_" + datetime.now().strftime("%y%m%d") + ".csv"
    if rows:
        write_store_csv(rows, out_path)
        _log(f"dump_products: 已写入 {out_path}，{len(rows)} 行")
    return {"ok": True, "result": {"rows": rows, "output_csv": out_path}}


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
    use_detail_flow: bool = False,
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
        use_detail_flow=use_detail_flow,
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
            use_detail_flow=True,
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
