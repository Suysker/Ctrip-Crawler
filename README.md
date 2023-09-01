# Ctrip-Crawler

##概述
Ctrip-Crawler 是一个携程航班信息的专业爬虫工具，主要基于 Selenium 框架进行实现。
request 方法访问携程 API 的方法，由于 IP 限制和 JS 逆向工程的挑战，该途径已不再适用。（报错）

##主要特性
Selenium 自动化框架：与直接请求 API 的方法不同，该项目基于 Selenium，提供高度可定制和交互式的浏览器模拟。

灵活的错误处理机制：针对不同类型的异常（如超时、验证码出现、未知错误等），实施相应的处理策略，包括重试和人工干预。

IP限制解决方案：利用页面特性和用户模拟，规避了 IP 限制，提高了爬取稳定性。

数据校验与解析：对获取的数据进行严格的数据质量和完整性校验，包括 gzip 解压缩和 JSON 格式解析。

版本迭代与优化：V2版本解决了验证码问题；V3版本提高了系统的稳定性和可用性。

##文档和教程
详细的使用指南和开发文档可在以下博客中查看：

[基于selenium的携程机票爬取程序](https://blog.suysker.xyz/archives/35)
[基于selenium的携程机票爬取程序V2](https://blog.suysker.xyz/archives/139)

[基于request的携程机票爬取程序](https://blog.suysker.xyz/archives/37)
[基于request的航班历史票价爬取](https://blog.suysker.xyz/archives/36)


##贡献与反馈
如果你有更好的优化建议或发现任何 bug，请通过 Issues 或 Pull Requests 与我们交流。我们非常欢迎各种形式的贡献！
