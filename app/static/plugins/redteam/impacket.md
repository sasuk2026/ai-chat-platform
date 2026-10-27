# Impacket — 网络协议与 Active Directory 工具集

GitHub: https://github.com/fortra/impacket

## 定位
Python 实现的 SMB/NTLM/Kerberos/LDAP 等网络协议库，附带大量内网渗透/AD 测试脚本，是 Windows 域环境研究的事实标准工具。

## 核心能力
- secretsdump.py：导出 SAM/NTDS 哈希
- psexec.py / wmiexec.py / smbexec.py：远程执行
- ntlmrelayx.py：NTLM 中继
- GetUserSPNs.py / ticketer.py：Kerberos 相关
- smbclient.py / lookupsid.py：枚举

## 典型用法
impacket-secretsdump 域控/ip
impacket-psexec 域/用户:密@靶标
impacket-ntlmrelayx -tf targets.txt

## 在 harness 研究中的用法
在内网授权靶场中研究横向移动与凭据防护；把检测思路（如 NTLM 中继防护）写成蓝队规则/服务。

## 授权使用提醒
仅限授权内网靶场；涉及凭据的操作极敏感，必须获书面授权。
