# Responder — LLMNR/NBT-NS/mDNS 投毒

GitHub: https://github.com/lgandx/Responder

## 定位
内网名称解析投毒利器，劫持 LLMNR/NBT-NS/mDNS 请求，诱导受害者把 NTLM 凭据发到攻击机，用于内网凭据捕获与中继研究。

## 核心能力
- 伪造权威响应，捕获 NTLMv2 哈希
- 支持 SMB/HTTP/SQL/WPAD 等协议诱饵
- 可与 ntlmrelayx 配合做中继

## 典型用法
Responder.py -I eth0
Responder.py -I eth0 -wrf   # 开启 WPAD/HTTP/SMB

## 在 harness 研究中的用法
在隔离授权靶场内演示"名称解析投毒 -> 凭据泄露"链路，用于验证网络分段、SMB 签名、WPAD 禁用等防御是否有效。

## 授权使用提醒
仅在完全隔离的授权靶场运行；会干扰正常名称解析，禁止在共享/生产网络使用。
