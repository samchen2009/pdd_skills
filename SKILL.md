# pdd_skills (Pinduoduo PDDAgent)

This skill describes how to use **pdd_skills_cli.py** to open the Pinduoduo app, search, parse products and stores, and **fully collect a store by keyword and output CSV**.

## Architecture

- **PDDAgent** extends **MobileAgent** and uses **UIAutomator** (backed by **UIAutomatorAndroid**).
- CLI entry: `pdd_skills_cli.py`; see `requirements.txt` for dependencies.

## How to run

From the repo root or from the `pdd_skills` directory:

```bash
python pdd_skills/pdd_skills_cli.py <command> [args]
# or
python -m pdd_skills.pdd_skills_cli <command> [args]
```

You can also run the library module (it delegates to the CLI):

```bash
python pdd_skills/pdd_skills.py <command> [args]
```

## Commands

| Command | Description |
|---------|-------------|
| `open` | Open Pinduoduo (optional `--stop` to stop then start) |
| `ensure-search-page` | Open Pinduoduo and go to search page (clicks "搜索" if not already there) |
| `search KEYWORD` | Search inside the app (optional `--search-button` for button text) |
| `dump-products` | Parse product list from current page (`--limit N`, default 10) |
| `dump-stores STORE1 [STORE2 ...] -o FILE` | Collect multiple stores in sequence and merge into one CSV |

## dump-store-page full flow (when store keyword is given)

1. **open**: Open Pinduoduo and go to search page (or simulate click to enter if not there).
2. **search(STORE)**: Search for the store keyword; result is a **product list** (not store list).
3. Click products one by one to open **product detail**; each time run **dump_product_detail** (scroll detail page to get store info); if store name matches target, click "进店" (enter store), otherwise back and try next product.
4. **After entering store**: Scroll to the end (stop when 5 consecutive scrolls yield no new content or when "本店暂无更多商品" appears).
5. While scrolling, dump the page repeatedly and keep collecting if not yet done or incomplete.
6. **Structure** the result (store + products, dedup) and optionally output **CSV** (`--output-csv FILE`).

## Global options

- `--device` / `-d`: Device (Android serial or `IP:port`); omit for auto-detect.
- `--json`: Output full JSON (including `ok`/`result`); otherwise only print `result` or `ok`.

## dump-store-page options (full flow)

- `store`: Store keyword (required to run full flow).
- `--output-csv`: Path to write the structured CSV.
- `--max-products`: Max number of products to click when looking for the target store (default 20).
- `--no-new-limit`: Stop after this many scrolls with no new content inside the store (default 5).

## Examples

```bash
# Open Pinduoduo
python pdd_skills/pdd_skills_cli.py open

# Go to search page
python pdd_skills/pdd_skills_cli.py ensure-search-page

# Search
python pdd_skills/pdd_skills_cli.py search 3CE

# Parse products on current page (max 5)
python pdd_skills/pdd_skills_cli.py dump-products --limit 5

```

## Dependencies

See `requirements.txt` (includes mobile_agent, uiautomator_android, etc.).
