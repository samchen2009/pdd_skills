# pdd_skills

拼多多应用定制能力，基于 mobile_agent 单例设备。

## 能力

- **open_app()**：打开拼多多
- **search(keyword, ...)**：在应用内搜索
- **dump_products(limit=10)**：从当前页面层级 dump 中解析商品列表（标题、价格），返回 `[{"title": "...", "price": "..."}, ...]`
- **parse_store_page_xml(xml_str)**：解析店铺首页 hierarchy XML（可从文件读入），返回 `{"store": {...}, "products": [...]}`。店铺含：store_name、total_sales、good_reviews、recommend_text、visit_count、guarantees；商品含：title、title_short、price、sales、tags。
- **dump_store_page()**：从当前设备 dump 店铺首页并解析，返回 `{"ok": True, "result": {"store": {...}, "products": [...]}}`

## 依赖

- 已连接 Android 设备（uiautomator2 + atx-agent）
- 本仓库中 `mobile_agent`、`uiautomator_android` 在 Python 路径下

## 运行测试

在仓库根目录（skills）执行：

```bash
PYTHONPATH=. python pdd_skills/test_dump_products.py
```

测试流程：初始化设备 → 打开拼多多 → 关闭弹窗 → 搜索「3CE」→ `dump_products(10)` 并打印前 10 条商品。

### 店铺首页解析（无需设备）

用 mock 的 `mocks/store.xml` 测试解析逻辑：

```bash
PYTHONPATH=. python pdd_skills/test_store_page.py
# 或指定 XML：PYTHONPATH=. python pdd_skills/test_store_page.py path/to/store.xml
```
