# pdd_skills（拼多多 PDDAgent）

本技能描述 **pdd_skills_cli.py** 的用法，用于在拼多多 App 上执行打开、搜索、解析商品与店铺，以及**按店铺关键词完整采集店铺并输出 CSV**。

## 架构

- **PDDAgent** 继承 **MobileAgent**，使用 **UIAutomator**（底层为 **UIAutomatorAndroid**）。
- 命令行入口：`pdd_skills_cli.py`；依赖见 `requirements.txt`。

## 运行方式

在仓库根目录或 `pdd_skills` 目录下均可：

```bash
python pdd_skills/pdd_skills_cli.py <command> [args]
# 或
python -m pdd_skills.pdd_skills_cli <command> [args]
```

也可直接运行库文件（会委托给 CLI）：

```bash
python pdd_skills/pdd_skills.py <command> [args]
```

## 命令

| 命令 | 说明 |
|------|------|
| `open` | 打开拼多多（可选 `--stop` 先结束再启动） |
| `ensure-search-page` | 打开拼多多并进入搜索页（若不在则点击「搜索」进入） |
| `search KEYWORD` | 在应用内搜索（可选 `--search-button` 指定按钮文案） |
| `dump-products` | 从当前页解析商品列表（`--limit N` 默认 10） |
| `dump-product-detail` | 从当前**商品详情页**解析商品信息 + 店铺名 +「进店」位置（`--max-scrolls` 详情页下滑次数） |
| `get-product-click-targets` | 从当前页解析可点击商品位置列表（用于脚本依次点进详情） |
| `dump-store-page [STORE]` | **无 STORE**：仅从当前页 dump 并解析店铺首页。**有 STORE**：完整流程（见下方）并可选 `--output-csv` |
| `parse-store-xml FILE` | 解析本地 store.xml（无需设备） |

## dump-store-page 完整流程（传入店铺关键词时）

1. **open**：打开拼多多并进入搜索页（若当前不在则模拟点击进入）。
2. **search(STORE)**：在应用内搜索店铺关键词；结果为**商品列表**（非店铺列表）。
3. 依次点击商品进入**商品详情页**；每次执行 **dump_product_detail**（详情页可下滑几页以获取店铺信息）；若找到店铺名与目标一致则点击「进店」，否则返回继续点击下一商品。
4. **进店后**：不停下滑直到结尾（连续 5 次滑动无新内容，或出现「本店暂无更多商品」）。
5. 下滑过程中不断 dump 页面，若尚未采集或信息不全则继续采集。
6. 将采集结果做**结构化**处理（店铺 + 商品去重），并支持输出为 **CSV**（`--output-csv FILE`）。

## 通用参数

- `--device` / `-d`：设备（Android 序列号或 `IP:port`），省略则自动检测。
- `--json`：输出完整 JSON（含 `ok`/`result` 等），否则仅打印 `result` 或 `ok`。

## dump-store-page 专用参数（完整流程时）

- `store`：店铺关键词（必填时走完整流程）。
- `--output-csv`：将结构化结果写入 CSV 文件路径。
- `--max-products`：最多点击多少个商品以寻找目标店铺（默认 20）。
- `--no-new-limit`：店内下滑时，连续几次无新内容则停止（默认 5）。

## 示例

```bash
# 打开拼多多
python pdd_skills/pdd_skills_cli.py open

# 进入搜索页
python pdd_skills/pdd_skills_cli.py ensure-search-page

# 搜索
python pdd_skills/pdd_skills_cli.py search 3CE

# 解析当前页商品（最多 5 条）
python pdd_skills/pdd_skills_cli.py dump-products --limit 5

# 当前在商品详情页时，解析商品+店铺（进店按钮）
python pdd_skills/pdd_skills_cli.py dump-product-detail --max-scrolls 5

# 完整流程：搜店铺关键词 → 找进店 → 滑到底采集 → 输出 CSV
python pdd_skills/pdd_skills_cli.py dump-store-page "某店铺名" --output-csv store.csv

# 仅解析当前页为店铺首页（不传 store）
python pdd_skills/pdd_skills_cli.py dump-store-page

# 解析本地店铺 XML
python pdd_skills/pdd_skills_cli.py parse-store-xml pdd_skills/mocks/store.xml
```

## 依赖

见 `requirements.txt`（含 mobile_agent、uiautomator_android 等）。
