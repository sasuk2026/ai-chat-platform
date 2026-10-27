# ffuf — 快速 Web 模糊测试器

GitHub: https://github.com/ffuf/ffuf

## 定位
Go 编写的高性能 Web 模糊测试工具，用于目录/文件/参数/vhost/POST 数据爆破与内容发现。

## 核心能力
- 目录与文件枚举（-u URL/FUZZ -w 字典）
- 参数爆破、vhost 发现
- 基于响应特征（status/size/word）过滤
- 支持递归、POST、Header 注入

## 典型用法
ffuf -u https://靶标/FUZZ -w /usr/share/wordlists/dir.txt
ffuf -u https://靶标 -H "Host: FUZZ.target" -w sub.txt
ffuf -u https://靶标/api -X POST -d "q=FUZZ" -w params.txt

## 在 harness 研究中的用法
对授权靶标做"攻击面暴露"测试：发现隐藏端点/管理接口/调试参数；结果用于收敛攻击面或加固。

## 授权使用提醒
控制请求速率与字典规模，避免对目标造成拒绝服务。
