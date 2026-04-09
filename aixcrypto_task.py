"""
AixCrypto Prediction Market 自动化任务 (Playwright 版本 2.0)
─────────────────────────────────────────────────────────────
基于 base_module 通用底层，仅包含 AixCrypto 业务逻辑：
  1. 登录（Connect Wallet → Continue → OKX Wallet）
  2. Prediction Market 下注循环（随机 Long/Short）
  3. Claim Rewards
"""

__version__ = "2026.04.09.1"

import asyncio
import random
import re
import sys
import time as _time
from datetime import datetime, timezone
from typing import Optional

from playwright.async_api import Page, BrowserContext

from base_module import (
    AccountInfo,
    WalletPopupHandler,
    _click_wallet_button,
    _find_and_fill_password,
    _click_unlock_button,
    OKX_DEFAULT_PASSWORD,
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

async def _find_wallet_popup(context: BrowserContext):
    """在 context.pages 中查找钱包扩展弹窗"""
    for p in context.pages:
        try:
            url = p.url or ""
        except Exception:
            continue
        if "chrome-extension://" in url and "offscreen" not in url:
            return p
    return None


async def _handle_wallet_popup(popup: Page, context: BrowserContext, account_id: str) -> bool:
    """
    处理钱包弹窗：
      - 有密码框 → 填密码 → 点解锁 → 等变成确认页 → 点确认
      - 无密码框 → 直接点确认/连接
    """
    try:
        await popup.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    await asyncio.sleep(1.5)

    found_pwd = await _find_and_fill_password(popup, context, account_id, OKX_DEFAULT_PASSWORD)
    if found_pwd:
        log(account_id, "钱包已锁定，正在解锁...")
        await asyncio.sleep(0.5)
        await _click_unlock_button(popup, context, account_id)
        await asyncio.sleep(3)

    try:
        if popup.is_closed():
            return True
    except Exception:
        return True

    clicked = await _click_wallet_button(popup, account_id)
    return found_pwd or clicked


async def login_if_needed(page: Page, account_id: str) -> bool:
    context = page.context

    for attempt in range(3):
        # ── 导航到首页 ──
        try:
            if attempt == 0:
                await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=30000)
            else:
                log(account_id, f"刷新重试 ({attempt}/2)...")
                await page.reload(wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            log(account_id, f"打开首页超时: {e}")
            continue
        await asyncio.sleep(3)

        # ── 1. 点击 Connect Wallet ──
        logged_in = await check_login_state(page, account_id) == "logged_in"
        clicked_connect = False

        if not logged_in:
            log(account_id, "未登录，点击 Connect Wallet...")
            try:
                btn = page.locator("button:has-text('Connect Wallet')").first
                await btn.click(timeout=8000)
                clicked_connect = True
            except Exception:
                try:
                    btn = page.locator("button:has-text('Login')").first
                    await btn.click(timeout=5000)
                    clicked_connect = True
                except Exception:
                    log(account_id, "未找到 Connect Wallet 按钮")
                    continue

            await asyncio.sleep(2)

            # ── 2. Continue with a wallet ──
            try:
                cw = page.locator("text=Continue with a wallet").first
                await cw.click(timeout=5000)
                log(account_id, "已点击 Continue with a wallet")
                await asyncio.sleep(1)
            except Exception:
                pass

            # ── 3. 选择 OKX Wallet ──
            try:
                okx = page.locator("text=OKX Wallet").first
                await okx.click(timeout=8000)
                log(account_id, "已点击 OKX Wallet")
            except Exception:
                log(account_id, "未找到 OKX Wallet，刷新重试...")
                continue
        else:
            log(account_id, "页面显示已登录，点击 Connect Wallet 触发钱包...")
            try:
                btn = page.locator("button:has-text('Connect Wallet')").first
                await btn.click(timeout=5000)
                clicked_connect = True
            except Exception:
                pass

        # ── 4. 处理钱包弹窗（解锁 + 确认），无论登录状态都要处理 ──
        await asyncio.sleep(3)
        handled = False
        for wait_round in range(8):
            popup = await _find_wallet_popup(context)
            if popup:
                log(account_id, f"捕获钱包弹窗: {popup.url[-50:]}")
                try:
                    await _handle_wallet_popup(popup, context, account_id)
                    handled = True
                except Exception as e:
                    log(account_id, f"处理弹窗异常: {e}")
                break
            await asyncio.sleep(1)

        if not handled and clicked_connect:
            log(account_id, "未捕获到钱包弹窗")

        # ── 5. 清理所有残留弹窗 ──
        await asyncio.sleep(2)
        for p in context.pages:
            try:
                url = p.url or ""
            except Exception:
                continue
            if "chrome-extension://" in url and "offscreen" not in url:
                try:
                    await _click_wallet_button(p, account_id)
                except Exception:
                    pass

        # ── 6. 等待登录生效 ──
        for i in range(10):
            await asyncio.sleep(2)
            if page.is_closed():
                log(account_id, "主页面被关闭")
                return False
            if await check_login_state(page, account_id) == "logged_in":
                log(account_id, "登录成功")
                return True

        log(account_id, "本轮登录超时")

    log(account_id, "登录失败（已重试 3 次）")
    return False


# ════════════════════════════════════════════════════════
#  Prediction Market — API 响应拦截（零 DOM 轮询）
#
#  前端 JS 自己会定时请求 /api/game/current-round，
#  我们被动监听这些 HTTP 响应，从 JSON 中读取精确状态，
#  只在下注瞬间做一次 DOM 按钮点击。
# ════════════════════════════════════════════════════════

class RoundWatcher:
    """
    被动监听浏览器自己的 API 网络响应。
    前端 JS 自己会定时请求 current-round / bet 等接口，
    我们只"偷听"这些已有的 HTTP 响应，不发任何额外请求。
    """

    def __init__(self, account_id: str):
        self.account_id = account_id
        self.round_data: dict = {}
        self.bet_result: dict = {}
        self._round_event = asyncio.Event()
        self._bet_event = asyncio.Event()
        self._active = True
        self.wallet_address = ""

    async def _on_response(self, response):
        if not self._active:
            return
        url = response.url
        try:
            if '/api/game/current-round' in url and response.status == 200:
                self.round_data = await response.json()
                if 'address=' in url:
                    self.wallet_address = url.split('address=')[1].split('&')[0]
                self._round_event.set()
            elif '/api/game/bet' in url and response.status == 200:
                self.bet_result = await response.json()
                self._bet_event.set()
        except Exception:
            pass

    def attach(self, page: Page):
        page.on('response', self._on_response)

    def detach(self, page: Page):
        self._active = False
        try:
            page.remove_listener('response', self._on_response)
        except Exception:
            pass

    async def wait_for_round(self, page: Page, address: str,
                             timeout: float = 10) -> dict:
        """
        等待下一次 current-round 响应。
        - 如果 sleep 期间已有数据到达 → 立即返回
        - 超时 → 在页面内执行 fetch 作为 fallback（保持 fingerprint）
        """
        if self._round_event.is_set():
            self._round_event.clear()
            return self.round_data

        try:
            await asyncio.wait_for(self._round_event.wait(), timeout=timeout)
            self._round_event.clear()
            return self.round_data
        except asyncio.TimeoutError:
            addr = address or self.wallet_address
            data = await _fetch_round_in_page(page, addr)
            if data and data.get('round'):
                self.round_data = data
            return self.round_data

    async def wait_for_bet(self, timeout: float = 10) -> bool:
        """等待下注 API 响应，返回是否成功"""
        self._bet_event.clear()
        try:
            await asyncio.wait_for(self._bet_event.wait(), timeout=timeout)
            return self.bet_result.get('success', False)
        except asyncio.TimeoutError:
            return False


def _parse_utc(s: str) -> Optional[float]:
    """ISO 时间字符串 → Unix 时间戳（秒）"""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp()
    except Exception:
        return None


async def _get_wallet_address(page: Page) -> str:
    """从页面钱包 provider 或 DOM 获取当前地址"""
    try:
        return await page.evaluate("""() => {
            try {
                if (window.okxwallet && window.okxwallet.selectedAddress)
                    return window.okxwallet.selectedAddress;
            } catch(e) {}
            try {
                for (const s of document.querySelectorAll('span')) {
                    const t = (s.textContent || '').trim();
                    if (/^0x[0-9a-fA-F]{6,}/.test(t)) return t;
                }
            } catch(e) {}
            return '';
        }""") or ""
    except Exception:
        return ""


async def _fetch_round_in_page(page: Page, address: str) -> dict:
    """
    Fallback: 在浏览器内调用 fetch 获取回合数据。
    请求从浏览器发出 → 自动携带 cookies / fingerprint / proxy。
    """
    try:
        qs = f'?address={address}' if address else ''
        return await page.evaluate("""async (qs) => {
            try {
                const r = await fetch('/api/game/current-round' + qs);
                if (!r.ok) return {};
                return await r.json();
            } catch(e) { return {}; }
        }""", qs)
    except Exception:
        return {}


async def _click_bet_button(page: Page, choice: str, account_id: str) -> bool:
    """点击 Place Long / Place Short 按钮"""
    btn = None
    for sel in (
        f'div.rounded-lg.py-3:has-text("Place {choice}")',
        f'div.rounded-lg:has-text("Place {choice}")',
        f'text=Place {choice}',
    ):
        loc = page.locator(sel)
        try:
            if await loc.count() > 0:
                btn = loc.last
                break
        except Exception:
            continue
    if not btn:
        btn = page.locator(
            f"xpath=//div[contains(normalize-space(),'Place {choice}')]"
        ).last
    try:
        await btn.click(timeout=4000)
        return True
    except Exception as e:
        log(account_id, f"点击 Place {choice} 失败: {e}")
        return False


async def _handle_first_bet_wallet(
    page: Page, context: BrowserContext, account_id: str,
):
    """首次下注后检查是否弹出钱包解锁弹窗"""
    log(account_id, "首次下注，检查钱包状态...")
    await asyncio.sleep(5)
    for _ in range(3):
        wallet_popup = await _find_wallet_popup(context)
        if wallet_popup:
            has_pwd = False
            for frame in wallet_popup.frames:
                try:
                    if await frame.locator('input[type="password"]').count() > 0:
                        has_pwd = True
                        break
                except Exception:
                    continue
            if has_pwd:
                log(account_id, "钱包未解锁，正在解锁...")
                await _handle_wallet_popup(wallet_popup, context, account_id)
                await asyncio.sleep(3)
            else:
                await _click_wallet_button(wallet_popup, account_id)
            break
        await asyncio.sleep(1)


# ════════════════════════════════════════════════════════
#  Prediction Market 主循环 — API 响应拦截版
# ════════════════════════════════════════════════════════

async def run_prediction_market(
    page: Page, account_id: str, max_timeout: int = 600,
) -> bool:
    """
    Prediction Market 下注循环。

    原理：
    1. 前端 JS 自己会定时请求 /api/game/current-round
    2. 我们用 page.on('response') 被动监听这些响应
    3. 从 JSON 中读取 phase / dailyBetRemaining / settleTime
    4. 只在 BETTING 阶段做一次按钮点击
    5. 用 settleTime 精确计算 sleep → 零轮询
    """
    try:
        await page.goto(MARKET_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        log(account_id, f"打开 Market 超时: {e}")
        return False
    await asyncio.sleep(3)
    log(account_id, "进入 Prediction Market")

    watcher = RoundWatcher(account_id)
    watcher.attach(page)

    wallet_addr = await _get_wallet_address(page)
    if wallet_addr:
        log(account_id, f"钱包地址: {wallet_addr[:8]}...{wallet_addr[-4:]}")

    last_progress = _time.time()
    last_bet_round_id = None
    first_click_done = False
    consecutive_empty = 0

    try:
        while not STOP_FLAG:
            if _time.time() - last_progress > max_timeout:
                log(account_id, f"超时 ({max_timeout}s)，结束。")
                return False

            # ── 1. 等待回合数据（被动监听 + 超时 fallback）──
            data = await watcher.wait_for_round(page, wallet_addr, timeout=10)

            if not data or not data.get('round'):
                consecutive_empty += 1
                if consecutive_empty >= 6:
                    log(account_id, "持续无法获取数据，刷新页面...")
                    try:
                        await page.reload(
                            wait_until="domcontentloaded", timeout=15000,
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(3)
                    consecutive_empty = 0
                continue
            consecutive_empty = 0

            # ── 2. 市场离线 ──
            if not data.get('hasActiveRound'):
                log(account_id, "市场 Offline，等待恢复...")
                await asyncio.sleep(10)
                last_progress = _time.time()
                continue

            round_info = data.get('round', {})
            remaining = data.get('dailyBetRemaining')
            phase = round_info.get('phase', '')
            round_id = round_info.get('id')

            if PERF_DEBUG:
                perf_log(
                    account_id,
                    f"phase={phase} round={round_id} remain={remaining}",
                )

            # ── 3. 次数用完 ──
            if remaining is not None and remaining <= 0:
                log(account_id, "剩余次数 0，结束。")
                return True

            # ── 4. BETTING 阶段 + 本轮未下注 → 下注 ──
            if phase == 'BETTING' and round_id != last_bet_round_id:
                bet_end_ts = _parse_utc(round_info.get('betEndTime', ''))
                now_ts = _time.time()
                time_left = (bet_end_ts - now_ts) if bet_end_ts else 3.0

                if time_left < 0.5:
                    continue

                await asyncio.sleep(random.uniform(0.3, min(1.5, time_left - 0.5)))

                choice = random.choice(["Long", "Short"])
                clicked = await _click_bet_button(page, choice, account_id)

                if clicked:
                    log(account_id,
                        f"Place {choice}（剩余 {remaining}）round#{round_id}")
                    last_bet_round_id = round_id
                    last_progress = _time.time()

                    if not first_click_done:
                        await _handle_first_bet_wallet(
                            page, page.context, account_id,
                        )
                        first_click_done = True
                    else:
                        bet_ok = await watcher.wait_for_bet(timeout=10)
                        if bet_ok:
                            bet_rem = (
                                watcher.bet_result.get('dailyBetLimit', 10)
                                - watcher.bet_result.get('dailyBetCount', 0)
                            )
                            log(account_id, f"下注确认成功！剩余 {bet_rem}")
                            last_progress = _time.time()
                            if bet_rem <= 0:
                                log(account_id, "次数用完，结束。")
                                return True
                        else:
                            log(account_id, "等待下注确认超时")

                # 精确 sleep 到结算时间，避免无意义轮询
                settle_ts = _parse_utc(round_info.get('settleTime', ''))
                if settle_ts:
                    sleep_s = max(0, settle_ts - _time.time() + 1)
                    await asyncio.sleep(min(sleep_s, 15))
                else:
                    await asyncio.sleep(5)

            # ── 5. 非 BETTING 或已下注 → 自动回到顶部等下一条数据 ──

    finally:
        watcher.detach(page)

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

    # 1. 登录（登录期间 handler 关闭，由 login_if_needed 手动处理弹窗）
    if not await login_if_needed(page, account_id):
        log(account_id, "登录失败")
        return False

    # 登录成功，启用自动弹窗处理器（下注时的签名弹窗）
    popup_handler.enabled = True

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
            unlock_target_url=HOME_URL,
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
            unlock_target_url=HOME_URL,
        ))
    else:
        print("无效输入。")


if __name__ == "__main__":
    main()
