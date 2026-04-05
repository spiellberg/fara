"""
Fara 外链批量提交脚本 (V2.0 Hybrid-Driven)
用法: python submit.py
"""

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import requests
import yaml
from playwright.sync_api import sync_playwright

from utils import extract_domain

# ---------------------------------------------------------------------------
# 路径配置（所有文件相对于本脚本所在目录）
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
PROJECT_ROOT = BASE_DIR.parent

CONFIG_YAML = PROJECT_ROOT / "config.yaml"
CONFIG_JSON = BASE_DIR / "config.json"
MY_SITES_FILE = BASE_DIR / "my_sites.json"
TARGET_SITES_FILE = BASE_DIR / "target_sites.json"
LOG_JSON_OLD = BASE_DIR / "log.json"
LOG_JSONL = BASE_DIR / "log.jsonl"
AUTH_DIR = BASE_DIR / "auth_states"


# ---------------------------------------------------------------------------
# 加载配置与日志
# ---------------------------------------------------------------------------
def load_configs():
    with open(CONFIG_YAML, encoding="utf-8") as f:
        lark_cfg = yaml.safe_load(f)
    with open(CONFIG_JSON, encoding="utf-8") as f:
        fara_cfg = json.load(f)
    return lark_cfg, fara_cfg

def load_json(path: Path):
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def migrate_old_log():
    if LOG_JSON_OLD.exists() and not LOG_JSONL.exists():
        print("[System] 检测到旧版 log.json，正在进行第一次迁移...")
        data = load_json(LOG_JSON_OLD)
        with open(LOG_JSONL, "w", encoding="utf-8") as f:
            for item in data:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        # 由于文件重要，仅重命名使其失效
        LOG_JSON_OLD.rename(BASE_DIR / "log.json.bak")
        print("[System] 迁移完成。")

def load_jsonl_logs():
    entries = []
    if LOG_JSONL.exists():
        with open(LOG_JSONL, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    entries.append(json.loads(line))
    return entries

def append_log(entry: dict):
    with open(LOG_JSONL, "a", encoding="utf-8") as f:
         f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def make_log_key(target_id: str, site_id: str) -> str:
    return f"{target_id}::{site_id}"

# ---------------------------------------------------------------------------
# Lark 通知
# ---------------------------------------------------------------------------
def send_lark_alert(lark_cfg: dict, target_site_name: str, my_site_name: str,
                    reason: str, submit_url: str):
    webhook_url = lark_cfg["lark"]["webhook_url"]
    text = (
        f"⚠️ Fara外链提交需人工介入\n"
        f"导航站：{target_site_name}\n"
        f"提交网站：{my_site_name}\n"
        f"原因：{reason}\n"
        f"提交地址：{submit_url}"
    )
    try:
        resp = requests.post(
            webhook_url,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=10,
        )
        resp.raise_for_status()
        print(f"  [Lark] 通知已发送")
    except Exception as e:
        print(f"  [Lark] 发送失败: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Fara 提示词与调用
# ---------------------------------------------------------------------------
def build_task_prompt(target: dict, site: dict, real_url: str) -> str:
    keywords_str = ", ".join(site.get("keywords", []))
    notes_str = f"Additional notes: {target['notes']}" if target.get("notes") else ""

    return f"""Go to {real_url} and submit a website listing using the information below.

Website details:
- Name: {site['name']}
- URL: {site['url']}
- Category: {site['category']}
- Keywords: {keywords_str}
- Contact email: {site['contact_email']}
- Logo URL (fill in if the form has a logo/icon field): {site['logo_url']}
- Description: Look at the description input field carefully.
  If it has a character limit of 100 or fewer, or if the field appears short,
  use this short description: "{site['description_short']}"
  Otherwise use this long description: "{site['description_long']}"

{notes_str}

After you finish, write exactly one of the following on the last line of your output:
- SUCCESS   (if the form was submitted successfully)
- BLOCKED: CAPTCHA   (if you encountered a CAPTCHA you cannot solve)
- BLOCKED: <brief reason>   (if you cannot proceed for any other reason)
"""

def run_fara_vision_agent(task_prompt: str, fara_cfg: dict) -> tuple[str, str]:
    """使用 CLI 调用 fara"""
    cmd = [
        "fara-cli",
        "--task", task_prompt,
        "--base_url", fara_cfg.get("fara_base_url", "http://localhost:8000"),
        "--model", fara_cfg.get("fara_model", "gpt-4o"),
        "--api_key", fara_cfg.get("fara_api_key", ""),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = result.stdout + result.stderr
        tail = "\n".join(output.splitlines()[-20:])

        if re.search(r"\bSUCCESS\b", tail):
            return "success", ""
        blocked = re.search(r"BLOCKED:\s*(.+)", tail)
        if blocked:
            return "manual_required", blocked.group(1).strip()
        last_line = output.strip().splitlines()[-1] if output.strip() else "no output"
        return "failed", f"no clear result — last line: {last_line}"
    except subprocess.TimeoutExpired:
        return "manual_required", "task timeout (>300s)"
    except FileNotFoundError:
        return "failed", "fara-cli not found, check PATH"
    except Exception as e:
        return "failed", str(e)


# ---------------------------------------------------------------------------
# 前置 DOM 寻路拦截器 (Phase 2 & 3)
# ---------------------------------------------------------------------------
# 精确匹配付费墙短语，避免误报（如 "Free Premium Tools" 之类）
_PAID_PATTERNS = re.compile(
    r"(submit.*?\$\s*\d|paid submission|upgrade to submit|pro plan required"
    r"|this listing is paid|\$\d+.*?(?:submit|list|add))",
    re.I | re.S,
)

def check_if_paid_required(page) -> bool:
    try:
        text = page.locator("body").inner_text(timeout=3000)
        if _PAID_PATTERNS.search(text):
            return True
    except Exception:
        pass
    return False

def sniff_submit_entry(page) -> bool:
    """尝试在当前页面找到提交入口 <a> 并点击，成功返回 True。"""
    # 精确短语，避免 "Add to Cart" / "Add Bookmark" 等无关链接被误命中
    SUBMIT_SELECTOR = (
        "a:has-text('Submit Site'), a:has-text('Submit Tool'), a:has-text('Submit Tool'),"
        "a:has-text('Submit a Site'), a:has-text('Submit Your Site'),"
        "a:has-text('Add Site'), a:has-text('Add Tool'), a:has-text('Add Your Site'),"
        "a:has-text('List Your Site'), a:has-text('Get Listed'),"
        "a:has-text('Submit'), a:has-text('Get Featured')"
    )
    try:
        loc = page.locator(SUBMIT_SELECTOR).first
        if loc.is_visible(timeout=2000):
            loc.click()
            page.wait_for_load_state("domcontentloaded", timeout=5000)
            return True
    except Exception:
        pass
    return False

def is_login_page(page) -> bool:
    """判断当前是否被阻拦在登录界面。合并 DOM 查询以减少开销。"""
    try:
        # 检查 URL（纯 Python，零开销）
        url = page.url.lower()
        # 加 (/|$|?) 边界，防止 /author/ 或 /authentic-* 被误匹配
        if re.search(r"/(login|signin|sign-in|auth|signup|register)(/|$|\?)", url):
            return True

        # 一次性抓取页面文本 + 同时检查 password 字段，减少 DOM 往返
        has_password = page.locator("input[type='password']").count() > 0
        if has_password:
            return True

        text = page.locator("body").inner_text(timeout=2000)
        if re.search(
            r"\b(log in to submit|please log in|sign in to continue"
            r"|login required|create an account to submit)\b",
            text, re.I
        ):
            return True
    except Exception:
        pass
    return False

def is_form_page(page) -> bool:
    """判断当前页面是否已经是表单页（排除登录页，且含 ≥2 个输入框）。"""
    if is_login_page(page):
        return False
    try:
        count = page.locator("input[type='text'], input[type='url'], textarea").count()
        return count >= 2
    except Exception:
        return False

def process_single_site(browser, target: dict, site: dict, fara_cfg: dict) -> tuple[str, str]:
    domain = extract_domain(target['submit_url'])
    state_file = AUTH_DIR / f"{domain}.json"
    
    context_options = {}
    if state_file.exists():
        context_options["storage_state"] = state_file
        
    context = browser.new_context(**context_options)
    page = context.new_page()
    
    try:
        page.goto(target['submit_url'], wait_until='domcontentloaded', timeout=15000)

        # 落地判断：如果被强制跳转到了登录页
        if is_login_page(page):
            context.close()
            return "manual_required", f"遭遇登录墙拦截，请使用 login_helper.py 录入 {domain} 凭证"

        # 寻路：如果当前不是表单页，尝试点击提交入口
        if not is_form_page(page):
            found = sniff_submit_entry(page)
            
            # 点击之后有可能弹出了登录框，或者跳转到了登录注册页
            if is_login_page(page):
                context.close()
                return "manual_required", f"尝试进入表单时遇到登录墙，请使用 login_helper.py 录入 {domain} 凭证"
                
            if not found:
                context.close()
                return "entry_not_found", "首页及子页未找到提交入口(表单)"

        # 验资拦截（放在寻路之后，确保检测的是最终落地页）
        if check_if_paid_required(page):
            context.close()
            return "skipped_paid", "检测到付费墙关键字"

        real_url = page.url
    except Exception as e:
        context.close()
        return "entry_not_found", f"访问失败或超时: {e}"

    # 关闭 Page 及其所属的 Context，减轻资源并清理内存
    context.close()

    # 如果通过了所有的前置挑战，我们唤醒昂贵的 Fara 完成填写
    task_prompt = build_task_prompt(target, site, real_url)
    status, reason = run_fara_vision_agent(task_prompt, fara_cfg)
    
    return status, reason


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    migrate_old_log()
    
    lark_cfg, fara_cfg = load_configs()
    my_sites = load_json(MY_SITES_FILE)
    target_sites = load_json(TARGET_SITES_FILE)
    log_history = load_jsonl_logs()

    # 免扰白名单：key -> 上次状态，dict 便于打印上次结果
    ignore_statuses = {"success", "manual_required", "skipped_paid", "entry_not_found"}
    # 同一 key 可能多次出现（重试写入），取最新一条
    done_keys: dict[str, str] = {}
    for e in log_history:
        if e.get("status") in ignore_statuses:
            done_keys[make_log_key(e["target_site_id"], e["my_site_id"])] = e["status"]

    total = len(target_sites) * len(my_sites)
    print(f"共 {len(target_sites)} 个导航站 × {len(my_sites)} 个网站 = {total} 个任务")

    new_entries: list[dict] = []   # 仅记录本次运行新增的条目
    log_entries = log_history.copy()
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        
        for target in target_sites:
            for site in my_sites:
                key = make_log_key(target["id"], site["id"])
                if key in done_keys:
                    print(f"[跳过] {target['name']} ← {site['name']} (上次状态: {done_keys[key]})")
                    continue

                print(f"\n[开始] {target['name']} ← {site['name']}")
                
                status, reason = process_single_site(browser, target, site, fara_cfg)
                
                print(f"  结果: {status}" + (f"  原因: {reason}" if reason else ""))

                entry = {
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "target_site_id": target["id"],
                    "target_site_name": target["name"],
                    "my_site_id": site["id"],
                    "my_site_name": site["name"],
                    "submit_url": target["submit_url"],
                    "status": status,
                    "reason": reason,
                }
                append_log(entry)
                log_entries.append(entry)
                new_entries.append(entry)
                done_keys[key] = status  # 本 session 内立刻写入，避免重复执行

                # 仅对首次新出现的 manual_required 报警
                if status == "manual_required":
                    send_lark_alert(lark_cfg, target["name"], site["name"], reason, target["submit_url"])
                
        browser.close()

    # 本次新增统计
    def _count(entries, s): return sum(1 for e in entries if e["status"] == s)
    success     = _count(new_entries, "success")
    manual      = _count(new_entries, "manual_required")
    failed      = _count(new_entries, "failed")
    skipped_paid = _count(new_entries, "skipped_paid")
    not_found   = _count(new_entries, "entry_not_found")

    print(f"\n=======================================================")
    print(f"本次运行 — 成功: {success} | 需人工介入: {manual} | 失败: {failed}")
    print(f"付费墙过滤: {skipped_paid} | 入口未找到: {not_found}")
    print(f"历史累计记录 {len(log_entries)} 条，详见: {LOG_JSONL}")

if __name__ == "__main__":
    main()
