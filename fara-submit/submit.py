"""
Fara 外链批量提交脚本
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

# ---------------------------------------------------------------------------
# 路径配置（所有文件相对于本脚本所在目录）
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
PROJECT_ROOT = BASE_DIR.parent

CONFIG_YAML = PROJECT_ROOT / "config.yaml"
CONFIG_JSON = BASE_DIR / "config.json"
MY_SITES_FILE = BASE_DIR / "my_sites.json"
TARGET_SITES_FILE = BASE_DIR / "target_sites.json"
LOG_FILE = BASE_DIR / "log.json"


# ---------------------------------------------------------------------------
# 加载配置
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


def save_log(entries: list):
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


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
# Fara 调用与输出解析
# ---------------------------------------------------------------------------
def build_task_prompt(target: dict, site: dict) -> str:
    keywords_str = ", ".join(site.get("keywords", []))
    notes_str = f"Additional notes: {target['notes']}" if target.get("notes") else ""

    return f"""Go to {target['submit_url']} and submit a website listing using the information below.

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


def run_fara_task(task: str, fara_cfg: dict) -> tuple[str, str]:
    """
    返回 (status, reason)
    status: "success" | "failed" | "manual_required"
    """
    cmd = [
        "fara-cli",
        "--task", task,
        "--base_url", fara_cfg["fara_base_url"],
        "--model", fara_cfg["fara_model"],
        "--api_key", fara_cfg["fara_api_key"],
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = result.stdout + result.stderr
        # 从末尾 20 行找关键词，避免中间日志干扰
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
# 主流程
# ---------------------------------------------------------------------------
def make_log_key(target_id: str, site_id: str) -> str:
    return f"{target_id}::{site_id}"


def main():
    lark_cfg, fara_cfg = load_configs()
    my_sites = load_json(MY_SITES_FILE)
    target_sites = load_json(TARGET_SITES_FILE)
    log_entries: list = load_json(LOG_FILE)

    # 已成功的组合，跳过不重试
    done_keys = {
        make_log_key(e["target_site_id"], e["my_site_id"])
        for e in log_entries
        if e.get("status") == "success"
    }

    total = len(target_sites) * len(my_sites)
    print(f"共 {len(target_sites)} 个导航站 × {len(my_sites)} 个网站 = {total} 个任务")

    for target in target_sites:
        for site in my_sites:
            key = make_log_key(target["id"], site["id"])
            if key in done_keys:
                print(f"[跳过] {target['name']} ← {site['name']} (已成功)")
                continue

            print(f"\n[开始] {target['name']} ← {site['name']}")
            task_prompt = build_task_prompt(target, site)
            status, reason = run_fara_task(task_prompt, fara_cfg)

            print(f"  结果: {status}" + (f"  原因: {reason}" if reason else ""))

            # 记录日志
            log_entries.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "target_site_id": target["id"],
                "target_site_name": target["name"],
                "my_site_id": site["id"],
                "my_site_name": site["name"],
                "submit_url": target["submit_url"],
                "status": status,
                "reason": reason,
            })
            save_log(log_entries)

            # 需要人工介入时发 Lark 通知
            if status == "manual_required":
                send_lark_alert(lark_cfg, target["name"], site["name"],
                                reason, target["submit_url"])

    # 汇总
    success = sum(1 for e in log_entries if e["status"] == "success")
    manual = sum(1 for e in log_entries if e["status"] == "manual_required")
    failed = sum(1 for e in log_entries if e["status"] == "failed")
    print(f"\n完成。成功: {success}  需人工: {manual}  失败: {failed}")
    print(f"详细记录见: {LOG_FILE}")


if __name__ == "__main__":
    main()
