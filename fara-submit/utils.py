"""
公共工具函数
"""

from urllib.parse import urlparse


def extract_domain(url: str) -> str:
    """从 URL 中提取裸域名（去掉 www. 前缀）"""
    parsed = urlparse(url)
    domain = parsed.netloc
    if domain.startswith("www."):
        domain = domain[4:]
    return domain
