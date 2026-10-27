# BloodHound — Active Directory 攻击路径分析

GitHub: https://github.com/BloodHoundAD/BloodHound

## 定位
将 AD 对象与关系导入图数据库，用图算法找出"任意用户到域管"的攻击路径，是 AD 安全评估的标配。

## 核心能力
- SharpHound 收集器采集 AD 数据
- 图可视化与 Cypher 查询
- 一键分析最短提权路径、委派、ACL 风险
- 支持 Azure（BloodHound CE）

## 典型用法
运行 SharpHound.ps1 收集 -> 导入 BloodHound -> 查询 "Shortest Paths to Domain Admins"
Cypher: MATCH p=shortestPath((u)-[:*1..]->(:Group {name:'DOMAIN ADMINS@x'})) RETURN p

## 在 harness 研究中的用法
在授权域环境中绘制攻击路径，识别过度授权/危险委派；蓝队据此生成"路径阻断"检测与服务。

## 授权使用提醒
仅限授权的域实验环境；采集数据可能含敏感结构，妥善隔离保管。
