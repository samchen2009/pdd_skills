#!/usr/bin/env python3
"""
用本地 mock/store.xml 测试店铺首页解析，无需设备。
  PYTHONPATH=. python pdd_skills/test_store_page.py
  或指定 XML 文件：PYTHONPATH=. python pdd_skills/test_store_page.py path/to/store.xml
"""

import json
import sys
from pathlib import Path

# 仓库根在 path 中以便 import pdd_skills
_repo = Path(__file__).resolve().parent.parent
if str(_repo) not in sys.path:
    sys.path.insert(0, str(_repo))

from pdd_skills import parse_store_page_xml


def main():
    xml_path = Path(__file__).resolve().parent / "mocks" / "store.xml"
    if len(sys.argv) > 1:
        xml_path = Path(sys.argv[1])
    if not xml_path.exists():
        print("XML not found:", xml_path, file=sys.stderr)
        sys.exit(1)
    xml_str = xml_path.read_text(encoding="utf-8")
    out = parse_store_page_xml(xml_str)
    print("=== 店铺信息 ===")
    print(json.dumps(out["store"], ensure_ascii=False, indent=2))
    print("\n=== 商品 (%d 个) ===" % len(out["products"]))
    for i, p in enumerate(out["products"], 1):
        print("%d. %s" % (i, p.get("title_short") or p.get("title")))
        print("   价格: %s  销量: %s  标签: %s" % (p.get("price") or "-", p.get("sales") or "-", p.get("tags")))


if __name__ == "__main__":
    main()
