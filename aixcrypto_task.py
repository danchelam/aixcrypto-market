"""
AixCrypto Prediction Market 自动化任务 (Playwright 版本 2.0)
─────────────────────────────────────────────────────────────
基于 base_module 通用底层，仅包含 AixCrypto 业务逻辑：
  1. 登录（Connect Wallet → Continue → OKX Wallet）
  2. Prediction Market 下注循环（随机 Long/Short）
  3. Claim Rewards
"""

__version__ = "2026.03.21.6"

import asyncio
import random
import re
import sys
import time as _time
from typing import Optional

from playwright.async_api import Page, BrowserContext

from base_module import (
    AccountInfo,
    WalletPopupHandler,
    _click_wallet_button,
    load_accounts,
    run_batch,
    log,
    perf_log,
    STOP_FLAG,
    PERF_DEBUG,
    ADSPOWER_API_KEY,
)

# ─── 页面 URL ──────────────────────────────────────────

HOME_URL = "https://hub.aixcrypto.ai/#home"
MARKET_URL = "https://hub.aixcrypto.ai/#prediction-market"
TASKS_URL = "https://hub.aixcrypto.ai/#tasks"


# ════════════════════════════════════════════════════════
#  登录状态检测
# ════════════════════════════════════════════════════════

async def check_login_state(page: Page, account_id: str) -> str:
    """返回 'logged_in' | 'not_logged_in' | 'unknown'"""
    try:
        if page.is_closed():
            return "unknown"

        nc = page.locator("div.text-neutral-500:has-text('Not Connected')")
        if await nc.count() > 0:
            return "not_logged_in"

        nc2 = page.locator("text=Not Connected")
        if await nc2.count() > 0:
            return "not_logged_in"

        addr = page.locator(
            "span.text-xs.font-medium.leading-tight.cursor-pointer"
        )
        if await addr.count() > 0:
            return "logged_in"

        addr2 = page.locator("span:has-text('0x')")
        if await addr2.count() > 0:
            return "logged_in"

        return "unknown"
    except Exception:
        return "unknown"


# ════════════════════════════════════════════════════════
#  登录流程
#  前端按钮点击由本模块负责，钱包弹窗确认由 WalletPopupHandler 自动处理
# ════════════════════════════════════════════════════════

async def login_if_needed(page: Page, account_id: str) -> bool:
    try:
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log(account_id, f"打开首页超时: {e}")
        return False
    await asyncio.sleep(3)

    state = await check_login_state(page, account_id)
    if state == "logged_in":
        log(account_id, "已是登录状态")
        return True

    log(account_id, "未登录，执行 Connect Wallet 流程...")

    # ── 1. Connect Wallet ───────────────────────
    try:
        btn = page.locator("button:has-text('Connect Wallet')").first
        await btn.click(timeout=8000)
        log(account_id, "已点击 Connect Wallet")
    except Exception:
        try:
            btn = page.locator("button:has-text('Login')").first
            await btn.click(timeout=5000)
            log(account_id, "已点击 Login")
        except Exception:
            log(account_id, "未找到 Connect Wallet / Login 按钮")
            return False

    await asyncio.sleep(2)
    if await check_login_state(page, account_id) == "logged_in":
        log(account_id, "点击后已自动登录")
        return True

    # ── 2. Continue with a wallet ───────────────
    try:
        cw = page.locator("text=Continue with a wallet").first
        await cw.click(timeout=5000)
        log(account_id, "已点击 Continue with a wallet")
        await asyncio.sleep(1)
    except Exception:
        if await check_login_state(page, account_id) == "logged_in":
            log(account_id, "未弹出钱包选择，但已处于登录状态")
            return True

    # ── 3. OKX Wallet ──────────────────────────
    try:
        okx = page.locator("text=OKX Wallet").first
        await okx.click(timeout=8000)
        log(account_id, "已点击 OKX Wallet")
    except Exception:
        if await check_login_state(page, account_id) == "logged_in":
            log(account_id, "未弹出钱包选择，但已处于登录状态")
            return True
        log(account_id, "未找到 OKX Wallet 选项")
        return False

    # ── 4. 主动搜索钱包弹窗并点击确认 ─────────
    #    不完全依赖异步 WalletPopupHandler（可能因时序问题漏掉弹窗）
    await asyncio.sleep(3)
    context = page.context
    wallet_confirmed = False
    for p in context.pages:
        try:
            url = p.url or ""
        except Exception:
            continue
        if "chrome-extension://" in url and "offscreen" not in url:
            log(account_id, f"发现钱包弹窗: {url[-60:]}")
            try:
                clicked = await _click_wallet_button(p, account_id)
                if clicked:
                    wallet_confirmed = True
            except Exception as e:
                log(account_id, f"处理弹窗异常: {e}")
            break

    # ── 5. 等待登录生效 ───────────────────────
    for i in range(20):
        await asyncio.sleep(2)
        if page.is_closed():
            log(account_id, "主页面被关闭，登录中断")
            return False

        # 前几轮如果还没确认弹窗，继续搜索
        if not wallet_confirmed and i < 5:
            for p in context.pages:
                try:
                    url = p.url or ""
                except Exception:
                    continue
                if "chrome-extension://" in url and "offscreen" not in url:
                    log(account_id, f"重试发现钱包弹窗: {url[-60:]}")
                    try:
                        clicked = await _click_wallet_button(p, account_id)
                        if clicked:
                            wallet_confirmed = True
                    except Exception:
                        pass
                    break

        state = await check_login_state(page, account_id)
        if state == "logged_in":
            log(account_id, "登录成功")
            return True
        if i == 10:
            log(account_id, "等待较久，刷新首页再检测...")
            try:
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(2)

    log(account_id, "登录流程超时")
    return False


# ════════════════════════════════════════════════════════
#  Prediction Market 工具函数
# ════════════════════════════════════════════════════════

async def get_market_status(page: Page) -> str:
    """返回 'live' | 'offline' | 'unknown'"""
    try:
        if await page.locator("span.text-emerald-400:has-text('Live')").count() > 0:
            return "live"
        if await page.locator("span:has-text('Live')").count() > 0:
            return "live"
        if await page.locator("span.text-red-400:has-text('Offline')").count() > 0:
            return "offline"
        if await page.locator("span:has-text('Offline')").count() > 0:
            return "offline"
    except Exception:
        pass
    return "unknown"


async def get_remaining_clicks(page: Page) -> Optional[int]:
    """从 'Place Long (94/100)' 提取剩余次数"""
    values = []
    for label in ("Place Long", "Place Short"):
        loc = page.locator(f"xpath=//div[contains(normalize-space(),'{label}')]")
        try:
            cnt = await loc.count()
        except Exception:
            continue
        for i in range(cnt):
            try:
                text = await loc.nth(i).inner_text(timeout=1000)
                m = re.search(r"\((\d+)\s*/\s*\d+\)", text)
                if m:
                    values.append(int(m.group(1)))
            except Exception:
                pass
    return min(values) if values else None


async def is_countdown_state(page: Page) -> bool:
    """'100 chances in 06:30:15' 表示今日次数已用完"""
    try:
        return (
            await page.locator(
                "xpath=//div[contains(normalize-space(),'chances in')]"
            ).count()
            > 0
        )
    except Exception:
        return False


async def wait_until_live(page: Page, account_id: str) -> bool:
    log(account_id, "市场 Offline，等待恢复...")
    last_refresh = 0.0
    while not STOP_FLAG:
        status = await get_market_status(page)
        if status == "live":
            log(account_id, "市场恢复 Live")
            return True
        now = _time.time()
        if now - last_refresh >= 30:
            try:
                await page.reload(wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            last_refresh = now
        await asyncio.sleep(1)
    return False


# ════════════════════════════════════════════════════════
#  Prediction Market 主循环
# ════════════════════════════════════════════════════════

async def run_prediction_market(
    page: Page, account_id: str, max_timeout: int = 360,
) -> bool:
    try:
        await page.goto(MARKET_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log(account_id, f"打开 Market 超时: {e}")
        return False
    await asyncio.sleep(2)
    log(account_id, "进入 Prediction Market")

    last_progress = _time.time()
    stage = "wait_open"
    stage_start = _time.time()
    none_count = 0
    prev_remaining: Optional[int] = None
    seen_success = False
    first_click_done = False

    while not STOP_FLAG:
        # 总超时保护
        if _time.time() - last_progress > max_timeout:
            log(account_id, f"超时 ({max_timeout}s)，结束。")
            return False

        # 市场状态
        status = await get_market_status(page)
        if status == "offline":
            if not await wait_until_live(page, account_id):
                return False
            last_progress = _time.time()
            stage = "wait_open"
            stage_start = _time.time()
            continue

        # 倒计时 → 今日用完
        if await is_countdown_state(page):
            log(account_id, "倒计时，结束。")
            return True

        # 剩余次数
        remaining = await get_remaining_clicks(page)
        if remaining is None:
            none_count += 1
            if none_count >= 120:
                log(account_id, "长时间无法读取剩余次数，结束。")
                return False
            await asyncio.sleep(0.15)
            continue
        none_count = 0
        if remaining <= 0:
            log(account_id, "剩余次数 0，结束。")
            return True

        # 页面状态
        placing_open = await page.locator("text=Placing Open").count() > 0
        success = await page.locator("text=Place Success!").count() > 0
        won = (
            await page.locator(
                "xpath=//*[contains(normalize-space(),'You Won')]"
            ).count()
            > 0
        )
        lost = (
            await page.locator(
                "xpath=//*[contains(normalize-space(),'You Lost')]"
            ).count()
            > 0
        )

        # ─── 等待开盘 → 下注 ─────────────────────
        if stage == "wait_open":
            if status == "live" and placing_open:
                choice = random.choice(["Long", "Short"])

                # 精确匹配实际可点击按钮（Tailwind class 的 div）
                btn = None
                for sel in (
                    f'div.rounded-lg.py-3:has-text("Place {choice}")',
                    f'div.rounded-lg:has-text("Place {choice}")',
                    f'text=Place {choice}',
                ):
                    loc = page.locator(sel)
                    try:
                        c = await loc.count()
                        if c > 0:
                            btn = loc.last  # .last 取最内层匹配
                            break
                    except Exception:
                        continue

                if not btn:
                    btn = page.locator(
                        f"xpath=//div[contains(normalize-space(),'Place {choice}')]"
                    ).last

                try:
                    await btn.click(timeout=4000)
                    log(account_id, f"Place {choice}（剩余 {remaining}）")
                    last_progress = _time.time()
                    stage = "wait_result"
                    stage_start = _time.time()
                    prev_remaining = remaining
                    seen_success = False

                    if not first_click_done:
                        log(account_id, "首次下注，等待弹窗处理...")
                        await asyncio.sleep(8)
                        first_click_done = True

                except Exception as e:
                    log(account_id, f"点击下注按钮失败: {e}")

        # ─── 等待结果 ────────────────────────────
        elif stage == "wait_result":
            if success and not seen_success:
                seen_success = True
                log(account_id, "Place Success!")
                last_progress = _time.time()

            if won or lost:
                log(account_id, f"结果: {'You Won!' if won else 'You Lost'}")
                last_progress = _time.time()
                stage = "wait_open"
                stage_start = _time.time()
                prev_remaining = None
                seen_success = False
                continue

            if prev_remaining is not None and remaining < prev_remaining:
                log(
                    account_id,
                    f"次数下降 {prev_remaining}→{remaining}，本轮完成。",
                )
                last_progress = _time.time()
                stage = "wait_open"
                stage_start = _time.time()
                prev_remaining = None
                seen_success = False
                continue

            if placing_open and _time.time() - stage_start > 4:
                log(account_id, "已重新开盘，本轮结束。")
                last_progress = _time.time()
                stage = "wait_open"
                stage_start = _time.time()
                prev_remaining = None
                seen_success = False
                continue

            if _time.time() - stage_start > 40:
                log(account_id, "等待结果超时，重置。")
                stage = "wait_open"
                stage_start = _time.time()
                prev_remaining = None
                seen_success = False
                continue

        if PERF_DEBUG:
            perf_log(
                account_id,
                f"stage={stage} market={status} remain={remaining} "
                f"open={placing_open} success={success} won={won} lost={lost}",
            )

        await asyncio.sleep(0.15)

    return False


# ════════════════════════════════════════════════════════
#  Claim Rewards
# ════════════════════════════════════════════════════════

async def claim_all_rewards(page: Page, account_id: str) -> bool:
    try:
        await page.goto(TASKS_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log(account_id, f"打开任务页超时: {e}")
        return False
    await asyncio.sleep(2)

    for _ in range(20):
        if STOP_FLAG:
            return False
        buttons = page.locator("button:has-text('Claim Reward')")
        count = await buttons.count()
        if count == 0:
            log(account_id, "无 Claim Reward 按钮，完成。")
            return True

        log(account_id, f"找到 {count} 个 Claim Reward")
        for i in range(count):
            try:
                btn = buttons.nth(i)
                await btn.scroll_into_view_if_needed(timeout=3000)
                await btn.click(timeout=3000)
            except Exception:
                pass
            await asyncio.sleep(1)
        await asyncio.sleep(1)

    log(account_id, "Claim 循环达到上限，退出。")
    return True


# ════════════════════════════════════════════════════════
#  主任务函数（供 base_module.run_single_account 调用）
# ════════════════════════════════════════════════════════

async def aixcrypto_task(
    page: Page,
    context: BrowserContext,
    account_id: str,
    popup_handler: WalletPopupHandler,
    **kwargs,
) -> bool:
    """AixCrypto 完整任务流程"""

    # 1. 登录
    if not await login_if_needed(page, account_id):
        log(account_id, "登录失败")
        return False

    # 2. Prediction Market
    place_done = await run_prediction_market(page, account_id)
    if STOP_FLAG:
        return False

    # 3. Claim Rewards
    claim_done = await claim_all_rewards(page, account_id)
    if STOP_FLAG:
        return False

    return place_done and claim_done


# ════════════════════════════════════════════════════════
#  入口
# ════════════════════════════════════════════════════════

def main():
    accounts = load_accounts()
    if not accounts:
        print("未读取到任何账号，请检查 shuju.xlsx")
        sys.exit(1)

    print(f"共读取到 {len(accounts)} 个账号。")
    print("请选择运行模式:")
    print("1. 单窗口测试（默认第 2 个账号）")
    print("2. 批量运行")

    mode = input("请输入数字 (1/2): ").strip()

    if mode == "1":
        target = accounts[1] if len(accounts) > 1 else accounts[0]
        print(f"单窗口测试: {target.id}")
        asyncio.run(run_batch(
            [target],
            aixcrypto_task,
            max_workers=1,
            api_key=ADSPOWER_API_KEY,
        ))

    elif mode == "2":
        try:
            workers = int(input("请输入并发数（建议 1-5）: ").strip())
        except ValueError:
            workers = 1
        print(f"批量运行，并发: {workers}")
        asyncio.run(run_batch(
            accounts,
            aixcrypto_task,
            max_workers=workers,
            api_key=ADSPOWER_API_KEY,
        ))
    else:
        print("无效输入。")


if __name__ == "__main__":
    main()
