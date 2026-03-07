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
    _log("ensure_search_page: 调用通用 ensure_search_page (dump 解析)...")
    from mobile_agent import ensure_search_page as ma_ensure_search_page
    out = ma_ensure_search_page(app=PDD_PACKAGE, package=PDD_PACKAGE)
    r = out.get("result") or out.get("parsed") or {}
    _log(f"ensure_search_page: search_input={bool(r.get('search_input'))} search_button={bool(r.get('search_button'))} search_entry={bool(r.get('search_entry'))}")
    if r:
        _log("ensure_search_page: parsed detail: " + json.dumps(r, ensure_ascii=False, indent=2, default=str))
    return out


# 搜索中间页：搜索框左侧有「商品」/「店铺」选择器；目标为店铺则要点「店铺」，目标为商品则要点「商品」
STORE_TAB_TEXT = "店铺"
PRODUCT_TAB_TEXT = "商品"


def select_search_tab(store: bool = True) -> dict:
    """
    当前页为搜索中间页时，先点当前 tab 弹出下拉，再点目标选项：
    - 目标为店铺(store=True)：点击「商品」→ 出现下拉 → 点击「店铺」；
    - 目标为商品(store=False)：点击「店铺」→ 出现下拉 → 点击「商品」。
    返回 {"ok": True} 或错误 dict。
    """
    u = _u()
    # 第一步：点「商品」或「店铺」打开下拉（与目标相反，点当前显示的才能弹出）
    open_tab = PRODUCT_TAB_TEXT if store else STORE_TAB_TEXT
    target_tab = STORE_TAB_TEXT if store else PRODUCT_TAB_TEXT
    _log(f"select_search_tab: 目标={'店铺' if store else '商品'}，先点 {open_tab!r} 弹出下拉...")
    u.click(text=open_tab)
    if not u.last_result.get("ok"):
        u.click(description=open_tab)
    if not u.last_result.get("ok"):
        _log(f"select_search_tab: 未找到 {open_tab!r}")
        return u.last_result
    time.sleep(0.5)
    # 第二步：点目标「店铺」或「商品」
    _log(f"select_search_tab: 点选项 {target_tab!r}...")
    u.click(text=target_tab)
    if not u.last_result.get("ok"):
        u.click(description=target_tab)
    if not u.last_result.get("ok"):
        _log(f"select_search_tab: 未找到选项 {target_tab!r}")
        return u.last_result
    time.sleep(0.8)
    _log(f"select_search_tab: 已选 {target_tab!r}")
    return {"ok": True}


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
    time.sleep(2.5)
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
            time.sleep(2)
            return {"ok": True, "result": {"store_name": name}}
    _log("search_store: 未找到目标店铺，尝试下滑再找")
    win = u.window_size()
    w = (win.get("result") or {}).get("width") or 540
    h = (win.get("result") or {}).get("height") or 960
    for _ in range(5):
        u.swipe(w // 2, int(h * 0.7), w // 2, int(h * 0.3), duration=0.25)
        time.sleep(1.2)
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
                time.sleep(2)
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
            u.swipe(fx, fy, tx, ty, duration=0.2)
            time.sleep(0.8)
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
) -> dict:
    """
    当前页为店铺首页时：不停下滑直到满足任一停止条件：
    1. 连续 no_new_limit 次滑不动（滑动前后屏内容相同，说明到底了）
    2. 底部出现结束提示词 end_marker
    返回合并后的 {store, products} 及 parsed_list。
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
    parsed_list: List[Dict[str, Any]] = []
    no_slide_count = 0
    round_no = 0
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
        parsed_list.append(parsed)
        products_this = parsed.get("products") or []
        cur_count = len(products_this)
        first_title = (products_this[0].get("title") or products_this[0].get("title_short") or "").strip() if products_this else ""

        # 停止条件 2：底部出现结束提示词
        if _xml_has_end_marker_in_bottom(xml_str, end_marker, h):
            _log(f"scroll_store_to_end: 底部出现结束文案 {end_marker!r}，停止")
            break

        merged = merge_store_parsed_list(parsed_list)
        total_now = len(merged.get("products") or [])

        _log(f"scroll_store_to_end: 本屏商品数={cur_count}, 合并总数={total_now}, 连续滑不动={no_slide_count}/{no_new_limit}")

        # 下滑
        _log("scroll_store_to_end: 下滑...")
        u.swipe(fx, fy, tx, ty, duration=0.2)
        time.sleep(1.0)

        # 停止条件 1：连续 no_new_limit 次滑不动（滑动后屏内容与滑动前相同）
        u.dump()
        out2 = u.last_result
        if not out2.get("ok"):
            break
        xml_str2 = (out2.get("result") or {}).get("xml") or ""
        parsed2 = parse_store_page_xml(xml_str2)
        parsed_list.append(parsed2)
        cur_count2 = len(parsed2.get("products") or [])
        products2 = parsed2.get("products") or []
        first_title2 = (products2[0].get("title") or products2[0].get("title_short") or "").strip() if products2 else ""
        if cur_count2 == cur_count and first_title2 == first_title and cur_count > 0:
            no_slide_count += 1
        else:
            no_slide_count = 0
        if no_slide_count >= no_new_limit:
            _log("scroll_store_to_end: 连续滑不动达到上限，停止")
            break

    merged = merge_store_parsed_list(parsed_list)
    _log(f"scroll_store_to_end: 共 {len(parsed_list)} 屏，合并商品数={len(merged.get('products') or [])}，完成")
    return {"ok": True, "result": {"merged": merged, "parsed_list": parsed_list}}


def merge_store_parsed_list(parsed_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """合并多次 parse_store_page_xml 的结果：店铺取首个非空，商品按 title+price 去重（同标题同价视为同一商品）。"""
    store: Dict[str, Any] = {}
    products: List[Dict[str, Any]] = []
    seen_keys: Set[str] = set()
    for p in parsed_list:
        s = p.get("store") or {}
        if s and not store:
            store = s
        for prod in p.get("products") or []:
            title = (prod.get("title") or prod.get("title_short") or "").strip()
            price = prod.get("price") or ""
            key = (title or "") + "|" + str(price)
            if key not in seen_keys:
                seen_keys.add(key)
                products.append(prod)
            else:
                """
                _log(f"merge_store: 去重跳过 标题={title[:40]}{'...' if len(title) > 40 else ''!r} 价格={price!r} key={key[:60]}{'...' if len(key) > 60 else ''!r}")
                """
    return {"store": store, "products": products}


# tags 中出现以下任一关键词则标记为缺货
OUT_OF_STOCK_KEYWORDS = ("售罄", "缺货", "仅剩", "最后")


def _out_of_stock_flag(tags: List[str]) -> str:
    """若 tags 中出现售罄/缺货/仅剩/最后等则返回 'Y'，否则返回 ''。"""
    if not tags:
        return ""
    joined = "|".join(tags) if isinstance(tags, list) else str(tags)
    return "Y" if any(kw in joined for kw in OUT_OF_STOCK_KEYWORDS) else ""


def to_csv_rows(store: Dict[str, Any], products: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """将店铺 + 商品列表转为可写 CSV 的扁平行列表。每行包含店铺字段 + 单商品字段 + 缺货标志。"""
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
            products.append(_extract_product_from_card(card, title_node))
        else:
            products.append({
                "title": (title_node.get("content-desc") or title_node.get("text") or "").strip(),
                "title_short": (title_node.get("text") or "").strip(),
                "price": None,
                "sales": None,
                "tags": [],
            })
    return {"store": store, "products": products}


def dump_store_page(
    store_keyword: Optional[str] = None,
    *,
    max_products_to_try: int = 20,
    store_scroll_no_new_limit: int = 5,
    end_marker: str = STORE_END_MARKER,
    output_csv: Optional[str] = None,
) -> dict:
    """
    若提供 store_keyword：search_store(店铺名) 进入目标店铺 → 滑到底 dump 并合并 → 结构化（可输出 CSV）。
    流程：ensure_search_page → 点「店铺」→ 输入关键词 → 点「搜索」→ 店铺列表找目标并点击 → scroll_store_to_end。
    若不提供 store_keyword：仅从当前页 dump 并解析店铺首页。
    max_products_to_try：CLI 兼容参数，本流程走店铺搜索故忽略。
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

    _log(f"dump_store_page: 开始（店铺搜索流程），目标={store_keyword!r}")
    search_out = search_store(store_keyword.strip())
    if not search_out.get("ok"):
        _log(f"dump_store_page: search_store 失败 {search_out}")
        return search_out
    _log("dump_store_page: 已进入目标店铺，等待 2s 后 scroll_store_to_end...")
    time.sleep(2)
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
    _log("dump_store_page_by_product: 步骤 1 打开搜索页并获取搜索框/按钮位置")
    page_out = ensure_search_page()
    if not page_out.get("ok"):
        _log(f"dump_store_page_by_product: ensure_search_page 失败 {page_out}")
        return page_out
    page_el = page_out.get("result") or {}
    _log("dump_store_page_by_product: 切换到「商品」搜索")
    select_search_tab(store=False)
    time.sleep(0.5)
    _log("dump_store_page_by_product: 步骤 2 搜索 store_keyword（商品列表）")
    search(
        store_keyword.strip(),
        search_input=page_el.get("search_input"),
        search_button=page_el.get("search_button"),
    )
    time.sleep(2)
    u = _u()
    _log("dump_store_page: 步骤 3 获取商品可点击列表（可下滑多屏凑满最多 {} 个）".format(max_products_to_try))
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
        _log(f"dump_store_page: 本屏 {len(new_list)} 个，新增 {added}，累计 {len(targets)} 个")
        if len(targets) >= max_products_to_try:
            break
        if scroll_round < max_scrolls - 1:
            u.swipe(fx, fy, tx, ty, duration=0.25)
            time.sleep(1.5)
    targets = targets[:max_products_to_try]
    _log(f"dump_store_page: 共 {len(targets)} 个商品，依次点击找进店（最多 {max_products_to_try}）")
    entered = False
    for idx, t in enumerate(targets):
        _log(f"dump_store_page: 商品 {idx+1}/{len(targets)} 点击 ({t.get('title', '')[:20]}...)")
        cx, cy = t.get("center") or (0, 0)
        u.click(x=cx, y=cy)
        time.sleep(2)
        _log(f"dump_store_page: 商品 {idx+1} 进入详情页，dump_product_detail...")
        detail_out = dump_product_detail(max_scrolls=max_detail_scrolls)
        if not detail_out.get("ok"):
            _log(f"dump_store_page: 商品 {idx+1} dump_product_detail 失败，返回列表")
            u.press("back")
            time.sleep(1)
            continue
        detail = detail_out.get("result") or {}
        store_block = detail.get("store") or {}
        store_name = (store_block.get("name") or "").strip()
        _log(f"dump_store_page: 商品 {idx+1} 店铺名={store_name!r}, 目标={store_keyword!r}")
        if store_name and (store_keyword in store_name or store_name in store_keyword):
            cen = store_block.get("enter_center")
            if cen:
                _log(f"dump_store_page: 匹配目标店铺，点击进店")
                u.click(x=cen[0], y=cen[1])
                entered = True
                break
        _log(f"dump_store_page: 非目标店铺，返回列表")
        u.press("back")
        time.sleep(1)
    if not entered:
        _log("dump_store_page: 未找到目标店铺，退出")
        return {
            "ok": False,
            "error": "store_not_found",
            "detail": f"在 {max_products_to_try} 个商品详情页未找到目标店铺「{store_keyword}」",
        }
    _log("dump_store_page: 步骤 4 进店完成，等待 2s 后 scroll_store_to_end...")
    time.sleep(2)
    scroll_out = scroll_store_to_end(no_new_limit=store_scroll_no_new_limit, end_marker=end_marker)
    if not scroll_out.get("ok"):
        _log(f"dump_store_page: scroll_store_to_end 失败: {scroll_out}")
        return scroll_out
    _log("dump_store_page: 步骤 5 合并并结构化")
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
