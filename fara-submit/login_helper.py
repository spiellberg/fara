import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

from utils import extract_domain

BASE_DIR = Path(__file__).parent
AUTH_DIR = BASE_DIR / "auth_states"


def main():
    if len(sys.argv) < 2:
        print("用法: python login_helper.py <目标网址>")
        sys.exit(1)
        
    url = sys.argv[1]
    if not url.startswith("http"):
        url = "https://" + url
        
    domain = extract_domain(url)
    state_file = AUTH_DIR / f"{domain}.json"
    
    print(f"准备针对域名 {domain} 收集凭证。")
    print("将开启浏览器窗口。请手动完成登录/各种人类验证后回到本终端即可保存。")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        # 如果之前曾保存过，则加载它，方便续签
        context = browser.new_context(
            storage_state=state_file if state_file.exists() else None
        )
        page = context.new_page()
        page.goto(url)
        
        input(f"登录完成且验证通过后，请按回车键保存凭证... ")
        
        context.storage_state(path=str(state_file))
        print(f"成功。凭证已保存至: {state_file}")
        
        browser.close()

if __name__ == "__main__":
    main()
