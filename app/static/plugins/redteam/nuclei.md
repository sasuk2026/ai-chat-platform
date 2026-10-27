# Nuclei — 基于模板的漏洞扫描器

GitHub: https://github.com/projectdiscovery/nuclei

## 定位
由 ProjectDiscovery 出品的高速、基于 YAML 模板的漏洞/错误配置/misconfig 扫描器，社区模板库覆盖 CVE、暴露面板、弱口令、TLS 问题等。

## 核心能力
- 海量社区模板（nuclei-templates 仓库），每日更新
- 支持 TCP/HTTP/DNS/File/Headless 多协议
- 自定义模板（YAML），匹配请求/响应即可
- 可与 subfinder、httpx 串联做资产测绘

## 典型用法
nuclei -u https://靶标 -silent
nuclei -l targets.txt -t cves/ -sev critical
nuclei -u https://靶标 -t exposures/ -t misconfiguration/

## 在 harness 研究中的用法
对授权靶标批量跑已知 CVE/暴露面模板，快速收敛攻击面；把命中结果整理成情报；用 DEV 把检测模板封装成团队可调用的 API 服务。

## 授权使用提醒
仅对授权资产扫描；扫描频率与并发需克制，避免对目标造成压力。
