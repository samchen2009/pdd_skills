#!/usr/bin/env python3
"""
测试 pdd_skills：打开拼多多 -> 搜索「3CE」-> dump_products 取前 10 条商品。
需已连接 Android 设备（uiautomator2），且已安装拼多多。
运行方式（在 skills 仓库根目录）:
  PYTHONPATH=. python pdd_skills/test_dump_products.py
"""

import logging
import sys
import time

# 配置详细日志
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)


def main():
    log.info("========== 开始测试 pdd_skills dump_products ==========")

    # 1. 初始化设备
    log.info("步骤 1: 初始化设备 (mobile_agent.init)...")
    try:
        from mobile_agent import init
        u = init()
        log.info("步骤 1 完成: 设备单例已就绪")
    except Exception as e:
        log.error("步骤 1 失败: %s", e)
        sys.exit(1)

    # 2. 打开拼多多
    log.info("步骤 2: 打开拼多多 (pdd_skills.open_app)...")
    try:
        from pdd_skills import open_app, PDD_PACKAGE
        out = open_app(stop=False)
        if not out.get("ok"):
            log.warning("步骤 2 返回: %s", out)
        else:
            log.info("步骤 2 完成: 已拉起 %s", PDD_PACKAGE)
    except Exception as e:
        log.error("步骤 2 失败: %s", e)
        sys.exit(1)

    time.sleep(2)
    log.debug("等待 2s 让首页稳定")

    # 3. 关闭可能的弹窗
    log.info("步骤 3: 尝试关闭弹窗 (mobile_agent.dismiss_popup)...")
    try:
        from mobile_agent import mobile_agent
        out = mobile_agent.dismiss_popup()
        log.debug("dismiss_popup 返回: %s", out)
        if out.get("ok"):
            log.info("步骤 3 完成: 已关闭弹窗")
        else:
            log.info("步骤 3: 无弹窗或未匹配到关闭按钮，继续")
    except Exception as e:
        log.debug("步骤 3 异常（可忽略）: %s", e)

    time.sleep(1)

    # 4. 进入搜索并输入关键词
    log.info("步骤 4: 执行搜索「3CE」(pdd_skills.search)...")
    try:
        from pdd_skills import search
        out = search("3CE", search_button_text="搜索")
        log.debug("search 返回: %s", out)
        log.info("步骤 4 完成: 已发起搜索")
    except Exception as e:
        log.error("步骤 4 失败: %s", e)
        sys.exit(1)

    time.sleep(3)
    log.debug("等待 3s 让搜索结果加载")

    # 5. 抓取前 10 条商品
    log.info("步骤 5: 从当前页 dump_products(limit=10)...")
    try:
        from pdd_skills import dump_products
        out = dump_products(limit=10)
        if not out.get("ok"):
            log.error("步骤 5 失败: %s", out)
            sys.exit(1)
        products = out.get("result") or []
        log.info("步骤 5 完成: 共解析出 %d 条商品", len(products))
        for i, p in enumerate(products, 1):
            log.info("  商品 %d: title=%r  price=%r", i, p.get("title"), p.get("price"))
    except Exception as e:
        log.error("步骤 5 失败: %s", e)
        sys.exit(1)

    log.info("========== 测试结束 ==========")
    print("\n--- 前 10 条商品 ---")
    for i, p in enumerate(products, 1):
        print("%d. %s  |  %s" % (i, p.get("price") or "-", (p.get("title") or "").strip() or "-"))


if __name__ == "__main__":
    main()
