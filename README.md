# Ctrip-Crawler

1.采用request方法直接请求携程的api接口，不但要考虑IP被限制，更要进行js逆向，成本巨大

2.以下程序基于selenium，获取api请求结果，并利用页面特性解决IP限制问题

3.程序为应对各种可能问题的产生，保证7*24小时运行，进行了大量的嵌套和try,excpet

4.程序执行为保证正确运行加入了sleep，运行效率尚未优化（单IP一天10000条航线不是问题，不建议高强度）

5.如果有优化或者bug，请不吝赐教!

## 具体详见bolg:

[基于selenium的携程机票爬取程序](https://blog.suysker.xyz/archives/35)

[基于request的航班历史票价爬取](https://blog.suysker.xyz/archives/36)
