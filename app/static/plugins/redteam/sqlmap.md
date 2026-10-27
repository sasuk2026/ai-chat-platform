# sqlmap — 自动化 SQL 注入检测与利用

GitHub: https://github.com/sqlmapproject/sqlmap

## 定位
开源、最成熟的自动化 SQL 注入（SQLi）工具，支持 MySQL、PostgreSQL、MSSQL、Oracle、SQLite 等绝大多数数据库，能检测并利用注入点、拖库、获取 shell。

## 核心能力
- 自动识别注入类型（布尔盲注、时间盲注、报错注入、联合查询、堆叠）
- 枚举数据库/表/列、dump 数据
- --os-shell 写 WebShell、--file-read 读文件（需对应权限）
- Tamper 脚本绕过 WAF（如 charencode、space2comment）

## 典型用法（授权靶标内）
sqlmap -u "http://靶标/vuln.php?id=1" --batch --dbs
sqlmap -u "http://靶标/vuln.php?id=1" -D db --dump
sqlmap -u "URL" --risk=3 --level=5 --technique=BEUST
sqlmap -r request.txt --level=3   # 从抓包文件测

## 在 harness 研究中的用法
当 probe 发现某参数返回异常/报错，用 intel 搜用法、用 DEV 把测试脚本写成服务；或直接让 harness 基于 sqlmap 思路构造针对性探测请求，验证注入是否存在。

## 授权使用提醒
仅在用户声明并授权的靶标上使用；不得对未授权第三方站点运行；破坏性动作（--os-shell/--file-write）前必须暂停等确认。
