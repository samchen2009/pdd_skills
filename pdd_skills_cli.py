#!/usr/bin/env python3
"""
PDDAgent 命令行入口：调用拼多多定制能力（open、search、dump-products、dump-store-page、parse-store-xml）。
依赖见 requirements.txt（mobile_agent、uiautomator_android）。
"""
import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# 确保仓库根在 path 中
_repo = Path(__file__).resolve().parent.parent
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))

from pdd_skills import (
    dump_product_detail,
    dump_products,
    dump_products_by_list,
    dump_store_page,
    dump_stores_to_csv,
    ensure_search_page,
    get_product_click_targets,
    open_app,
    parse_store_page_xml,
    search,
)


def _load_product_list_file(path: Path) -> List[Dict[str, Any]]:
    """
    支持 JSON 或文本文件。
    JSON： [{"store":"x","product":"y","price":"z"}, ...]
    文本：每行三列逗号分隔，可带引号，如 "acj","3ce","32"
    """
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        return []
    if content.startswith("["):
        data = json.loads(content)
        return data if isinstance(data, list) else [data]
    rows: List[Dict[str, Any]] = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            reader = csv.reader(io.StringIO(line))
            row = next(reader)
        except Exception:
            continue
        if len(row) >= 2:
            rows.append({
                "store": (row[0] or "").strip(),
                "product": (row[1] or "").strip(),
                "price": (row[2] or "").strip() if len(row) >= 3 else "",
            })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="pdd_skills 命令行（PDDAgent）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--device", "-d", default=None, help="设备：Android 序列号或 IP:port，省略则自动检测")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("open", help="打开拼多多").add_argument("--stop", action="store_true", help="先结束再启动")
    sub.add_parser("ensure-search-page", help="打开拼多多并进入搜索页（若不在则点击进入）")
    p_search = sub.add_parser("search", help="在应用内搜索")
    p_search.add_argument("keyword", help="搜索关键词")
    p_search.add_argument("--search-button", default="搜索", dest="search_button", help="搜索按钮文案")
    p_dump_p = sub.add_parser("dump-products", help="从当前页解析商品列表")
    p_dump_p.add_argument("--limit", type=int, default=10, help="最多解析条数")
    p_dump_detail = sub.add_parser("dump-product-detail", help="从当前商品详情页解析商品+店铺（进店按钮）")
    p_dump_detail.add_argument("--max-scrolls", type=int, default=5, help="详情页下滑最多次数以找进店")
    p_store = sub.add_parser("dump-store-page", help="dump 店铺：无参则仅解析当前页；有 store 则完整流程并可选 CSV")
    p_store.add_argument("store", nargs="?", default=None, help="店铺关键词（可选；无则仅解析当前页）")
    p_store.add_argument("--output-csv", dest="output_csv", default=None, help="完整流程时写入 CSV 路径")
    p_store.add_argument("--max-products", type=int, default=20, dest="max_products", help="最多点击多少个商品找进店")
    p_store.add_argument("--no-new-limit", type=int, default=5, dest="no_new_limit", help="店内滑到底：连续几次无新内容则停")
    p_store.add_argument("--max-scrolls", type=int, default=2, dest="max_scrolls", help="加购弹窗内规格区竖滑次数（默认2），横滑固定5次")
    p_store.add_argument("--max-products-scrolls", type=int, default=None, dest="max_products_scrolls", help="店内最多下滑次数（默认无限制，直到底）")
    p_stores = sub.add_parser("dump-stores", help="依次获取多个店铺商品信息并汇总到一个 CSV")
    p_stores.add_argument("stores", nargs="+", help="店铺关键词（可多个，如：店铺A 店铺B）")
    p_stores.add_argument("--output", "-o", dest="output_csv", required=True, help="汇总表格输出路径（CSV）")
    p_stores.add_argument("--max-products", type=int, default=20, dest="max_products", help="单店最多点击多少个商品找进店（店铺搜索流程中未用）")
    p_stores.add_argument("--no-new-limit", type=int, default=5, dest="no_new_limit", help="店内滑到底：连续几次无新内容则停")
    p_click = sub.add_parser("get-product-click-targets", help="从当前页解析可点击商品位置（用于进详情）")
    p_click.add_argument("--limit", type=int, default=20, help="最多返回条数")
    p_parse = sub.add_parser("parse-store-xml", help="解析本地店铺首页 XML（无需设备）")
    p_parse.add_argument("file", help="store.xml 文件路径")
    p_by_list = sub.add_parser("dump-products-by-list", help="按列表进店搜商品、进详情、分享复制链接，生成 banya_hotspots_YYMMDD.csv")
    p_by_list.add_argument("list_file", help="JSON 或文本文件：JSON 为 [{\"store\",\"product\",\"price\"},...]；文本为每行三列逗号分隔，如 \"acj\",\"3ce\",\"32\"")
    p_by_list.add_argument("--output-csv", "-o", dest="output_csv", default=None, help="输出 CSV 路径（默认 banya_hotspots_YYMMDD.csv）")

    args = parser.parse_args()
    use_json = args.json

    def out(obj: Any) -> None:
        if use_json:
            print(json.dumps(obj, ensure_ascii=False, indent=2))
        else:
            if isinstance(obj, dict) and "ok" in obj:
                if not obj.get("ok"):
                    print("error:", obj.get("error"), obj.get("detail", ""), file=sys.stderr)
                    return
                obj = obj.get("result")
            if obj is not None:
                print(json.dumps(obj, ensure_ascii=False, indent=2))
            else:
                print("ok")

    try:
        need_device = args.command in (
            "open", "ensure-search-page", "search", "dump-products", "dump-product-detail",
            "dump-store-page", "dump-stores", "dump-products-by-list", "get-product-click-targets",
        )
        if need_device:
            from mobile_agent import init as mobile_init
            mobile_init(device=args.device)
        if args.command == "open":
            out(open_app(stop=getattr(args, "stop", False)))
        elif args.command == "ensure-search-page":
            out(ensure_search_page())
        elif args.command == "search":
            page_out = ensure_search_page()
            if not page_out.get("ok"):
                out(page_out)
                if "parsed" in page_out:
                    print("[pdd_store] parsed:", json.dumps(page_out["parsed"], ensure_ascii=False, indent=2, default=str), flush=True)
                return 1
            page_el = page_out.get("result") or {}
            out(search(
                args.keyword,
                search_button_text=args.search_button,
                search_input=page_el.get("search_input"),
                search_button=page_el.get("search_button"),
            ))
        elif args.command == "dump-products":
            out(dump_products(limit=args.limit))
        elif args.command == "dump-product-detail":
            out(dump_product_detail(max_scrolls=getattr(args, "max_scrolls", 5)))
        elif args.command == "dump-store-page":
            store_kw = getattr(args, "store", None)
            out(dump_store_page(
                store_keyword=store_kw,
                max_products_to_try=getattr(args, "max_products", 20),
                store_scroll_no_new_limit=getattr(args, "no_new_limit", 5),
                output_csv=getattr(args, "output_csv", None),
                max_scrolls=getattr(args, "max_scrolls", 2),
                max_products_scrolls=getattr(args, "max_products_scrolls", None),
            ))
        elif args.command == "dump-stores":
            result = dump_stores_to_csv(
                store_keywords=getattr(args, "stores", []),
                output_csv=getattr(args, "output_csv", ""),
                max_products_to_try=getattr(args, "max_products", 20),
                store_scroll_no_new_limit=getattr(args, "no_new_limit", 5),
            )
            out(result)
            failed = (result.get("result") or {}).get("failed_stores") or []
            if failed and not use_json:
                for f in failed:
                    print(f"[pdd_store] 失败店铺: {f.get('store')!r} — {f.get('error')}: {f.get('detail', '')}", file=sys.stderr)
        elif args.command == "get-product-click-targets":
            out(get_product_click_targets(limit=getattr(args, "limit", 20)))
        elif args.command == "parse-store-xml":
            xml_str = Path(args.file).read_text(encoding="utf-8")
            out(parse_store_page_xml(xml_str))
        elif args.command == "dump-products-by-list":
            list_path = Path(getattr(args, "list_file", ""))
            list_data = _load_product_list_file(list_path)
            result = dump_products_by_list(
                list_data,
                output_csv=getattr(args, "output_csv", None),
            )
            out(result)
    except Exception as e:
        err = {"ok": False, "error": type(e).__name__, "detail": str(e)}
        if use_json:
            print(json.dumps(err, ensure_ascii=False, indent=2))
        else:
            print("error:", e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
