# OWASP ZAP — Web 应用安全代理与扫描器

GitHub: https://github.com/zaproxy/zaproxy

## 定位
OWASP 维护的开源 Web 安全测试代理，集被动/主动扫描、爬虫、fuzzer、API 扫描于一身，适合对授权 Web 应用做系统化安全评估。

## 核心能力
- 拦截代理（手动测试、改包）
- 主动/被动扫描，内置规则库
- 爬虫与 AJAX 爬虫
- 基线扫描脚本（CI 集成）
- REST API 可编程驱动

## 典型用法
./zap.sh -daemon -port 8080
zap-baseline.py -t https://靶标 -r report.html
通过 API：/JSON/ascan/action/scan/?url=...

## 在 harness 研究中的用法
把 ZAP 作为"前端/后端输入校验"测试的核心：对模型服务的 Web 前端、API 网关做扫描，发现注入/XSS/越权等；用 DEV 把扫描能力封装成 API。

## 授权使用提醒
仅对授权应用扫描；主动扫描可能扰动目标，需在授权窗口内、控制强度。
