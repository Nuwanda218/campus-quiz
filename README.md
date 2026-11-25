# 校园在线刷题与复习系统

面向校内期末复习开发的在线答题网站，提供轮次进度、错题专项训练及题库更新，并完成部署与维护。

这是合作项目，本人参与开发；以下介绍系统整体功能，不代表全部功能由本人独立完成，具体分工将在整理协作记录后补充。

历史归档数据库累计记录 **2868 个注册账户**、约 **84.9 万条答题记录**。

## 答题与复习

学生可以按正式轮次答题、查看进度，也可以进入错题专项练习。两类活动分别保存，专项训练不会覆盖正式轮次的答题卡；通过提交标识识别重复提交，避免同一次作答被重复记录。

## 题库更新时保留历史

题库范围和题目会发生变化，直接替换数据库会丢失已有进度。更新流程先做 SQLite 一致性备份，再在事务中迁移数据：

1. 保留仍在新题库中的题目身份及其答题进度。
2. 将退役题目及相关历史记录归档，再调整在线进度和统计。
3. 为新增题目建立可继续作答的位置，保留可沿用的历史记录。

网站以 Python、Flask、SQLite 和 JavaScript 实现，配套部署、备份与升级脚本支持维护。

## 项目成果与公开范围

账户数与答题记录来自历史归档，不表示同时在线人数或 HTTP 请求量。当前仓库提供历史业务源码及合成数据演示，不提供历史用户数据库或在线演示。

真实用户资料、数据库备份、原课程题库、`.env` 和现网配置保留在原环境。公开版本附带 8 道新编写的软件工程基础演示题及历史规模的汇总数字，启动时创建本地空数据库。详见 [数据说明](data/README.md)。

## 文件结构

```text
app.py                       Flask 路由、SQLite Schema 和答题业务
templates/                   登录、注册和答题页面，含原生 JavaScript
data/questions.json          8 道合成演示题，包含选项、答案和解析
data/historical-metrics.json 历史汇总数字，不含个人记录
manage.py                    本地账号查询和删除工具
backup.py                    SQLite Backup API 备份工具
migrate_question_scope.py    题库范围迁移与退役数据归档
validate_question_bank.py    题库格式校验工具
requirements.txt            Python 依赖
Dockerfile                   可选容器运行配置
docs/                        整理说明与源文件哈希清单
```

业务逻辑和页面脚本仍较集中，本次整理保留历史结构，没有重新拆分模块。

## 本地运行

建议 Python 3.12。在仓库根目录执行：

### Windows / PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:SECRET_KEY = & .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))"
.\.venv\Scripts\python.exe -m flask --app app run --host 127.0.0.1 --port 8000
```

### macOS / Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export SECRET_KEY="$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))')"
.venv/bin/python -m flask --app app run --host 127.0.0.1 --port 8000
```

打开 <http://127.0.0.1:8000>，注册虚构的 10 位数字账号，例如 `0000000001`。请勿输入真实学号。

**历史登录方式只有学号，没有密码验证。此版本用于本地演示，不适合直接用于公网真实账户。** 页面保留“毛概”“学号”等历史称呼，并显示合成题库演示提示。

`.env.example` 仅说明环境变量，应用不会自动读取 `.env`。请使用上面的命令设置本地密钥；重新生成密钥会使旧会话失效。默认数据库为 `instance/quiz.db`，可通过 `DATABASE_PATH` 指定自己的演示库，不要指向历史数据库。

不要直接用历史入口 `python app.py` 对外演示：该入口监听 `0.0.0.0`。上面的命令明确绑定本机回环地址。Gunicorn 用于 Linux 或容器，不在 Windows 原生环境直接运行。

## 数据维护与验证边界

`python backup.py` 会备份当前 `DATABASE_PATH` 指向的演示库；`manage.py` 可以列出或删除本地账号。操作前需确认目标路径。

题库迁移接受位置参数，例如：

```bash
python migrate_question_scope.py instance/quiz.db data/questions.json
```

迁移会修改目标库，先备份，再对演示副本操作。归档表初始化在数据事务之前；数据修改失败时回滚，不宣称新增归档表也必然消失。

本次发布仅检查文件范围、Python 语法、题库格式及提交内容，没有运行完整业务测试、性能测试或 Docker 构建，也没有访问线上服务。

## 来源与公开整理

源文件及公开版调整记录见 [整理说明](docs/public-release.md) 和 [文件哈希清单](docs/source-manifest.json)。没有复制原项目的 Git 历史、环境文件或运行数据。

本次未自行给合作代码添加开源许可证；作者署名、分工与后续许可由参与者共同确认。公开可查看不代表获得任意再分发授权。

[返回项目索引](https://github.com/Nuwanda218/nuwanda218/blob/main/projects/README.md)

更新：2026-11-25
