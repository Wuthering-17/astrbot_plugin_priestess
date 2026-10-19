# 更新记录

## v0.1.4

- 原项目为 [Billtranscend/Priestess-bot](https://github.com/Billtranscend/Priestess-bot)，
  本仓库仅提供 AstrBot 接口适配；补全 README 开头的原作者、参考项目与维护范围说明。
- 保留原作者 MIT 许可证、第三方许可和上游 Git 历史。
- 修复 AstrBot 4.28.2 事件类型的导入路径。
- 清理资料库包中残留的 NoneBot 初始化入口，修复插件包内模块导入层级。
- 小黑盒读取和本地渲染在 Chromium 缺失时，可使用 Windows 已安装的后台 Edge / Chrome。
- 对浏览器缺失、页面超时和小黑盒数据异常提供明确提示，并保留诊断日志。
- 在 Python 3.12 与 AstrBot 4.28.2 环境验证入口导入、实例创建、数据库初始化和后台浏览器启动。
