# Telegram 群组「上车」自动提醒机器人（Neon 持久化版）

多群组隔离 · 群主/管理员可管理 · Neon Postgres 持久化 · Render 免费部署。

## ✨ 功能

- 群里发送「上车」，自动 @ 下一位接车人（按顺序轮询）
- 每个群组的接车人名单 / 轮询顺序完全独立
- 数据存储在 Neon Postgres，Render 重启 / 重新部署不丢失
- 主菜单 4 大按钮：添加 / 删除 / 我的名单 / 我所在的群组

## 🚀 部署步骤

### 第 1 步：创建 Neon 数据库（免费）

1. 访问 [neon.tech](https://neon.tech) 注册（无需信用卡）
2. 创建 Project，选择离你最近的区域
3. 在 **Dashboard → Connect** 中复制连接字符串，格式类似：

```
postgresql://user:password@ep-xxx.region.aws.neon.tech/neondb?sslmode=require
```

### 第 2 步：创建 Telegram Bot

1. 找 [@BotFather](https://t.me/BotFather) 发送 `/newbot`
2. 记下 **BOT_TOKEN**

### 第 3 步：上传代码到 GitHub

```bash
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/你的用户名/telegram-che-bot.git
git push -u origin main
```

### 第 4 步：在 Render 创建 Web Service

1. 登录 [render.com](https://render.com) → **New +** → **Web Service**
2. 连接你的 GitHub 仓库
3. 配置：
   - **Runtime**：`Python`
   - **Build Command**：`pip install -r requirements.txt`
   - **Start Command**：`python che.py`
   - **Instance Type**：`Free`
4. 在 **Environment Variables** 添加两个变量：
   - `BOT_TOKEN` = 你的 Bot Token
   - `DATABASE_URL` = 第 1 步复制的 Neon 连接字符串
5. 点 **Create Web Service**

### 第 5 步：保活

在 `.github/workflows/keep-alive.yml` 中把 `你的服务名.onrender.com` 替换成实际域名，GitHub Actions 会每 14 分钟自动 ping 一次。

### 第 6 步：群里启用

1. 把机器人拉进群，设为**群管理员**
2. 群里发一条消息（如「上车」）
3. 私聊机器人 `/start`

## 🗄️ 数据库表结构

程序会自动创建三张表（无需手动建表）：

| 表名 | 用途 |
|------|------|
| `che_groups` | 群组信息 |
| `che_drivers` | 接车人名单 |
| `che_state` | 轮询索引状态 |

每张表结构相同：`id`（固定为 1）、`data`（JSONB）、`updated_at`。

## 📝 常用命令

| 命令 | 说明 |
|------|------|
| `/start` `/menu` | 打开主菜单 |
| `/add` | 添加接车人 |
| `/remove` | 删除接车人 |
| `/my` | 查看自己管理的接车人 |
| `/list` | 查看我所在的群组 |
| `/cancel` | 取消当前操作 |

## 🔧 本地运行

```bash
pip install -r requirements.txt
export BOT_TOKEN="你的token"
export DATABASE_URL="postgresql://..."
python che.py
```

本地运行不需要 `PORT` 环境变量，会自动跳过 HTTP 服务。

## 🛡️ 安全建议

- **不要** 把 `BOT_TOKEN` 或 `DATABASE_URL` 提交到 GitHub
- 在 Render 的 Environment Variables 中配置，`.env` 已在 `.gitignore` 中

## 📄 许可证

MIT
