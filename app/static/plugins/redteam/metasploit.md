# Metasploit Framework — 漏洞利用与后渗透框架

GitHub: https://github.com/rapid7/metasploit-framework

## 定位
业界标准的开源渗透测试框架，内置数千个 exploit/auxiliary/post 模块，覆盖远程利用、payload 生成、后渗透（提权、横向、凭据窃取）。

## 核心能力
- exploit 模块：远程代码执行、注入等
- auxiliary：扫描、爆破、嗅探
- post：后渗透（权限维持、信息收集）
- meterpreter：强交互式 payload

## 典型用法
msfconsole
use exploit/multi/handler
set RHOSTS 靶标; set PAYLOAD ...; run
search cve:2021 type:exploit

## 在 harness 研究中的用法
当研究确认某漏洞存在，用 Metasploit 对应模块做"概念验证（PoC）"，验证可利用性；结果用于授权测试报告或防御加固建议。

## 授权使用提醒
仅对授权靶标运行 exploit；不得在真实生产/未授权系统上触发利用；高风险模块先确认。
