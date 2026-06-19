# 手动推送指南

## 仓库地址

- **GitHub 仓库**：https://github.com/chengwenchen06-sudo/stock-radar
- **Pages 预览**：https://chengwenchen06-sudo.github.io/stock-radar/

> 仓库在你个人账号 `chengwenchen06-sudo` 下，不在 `LearnPrompt` 组织（组织无创建权限）。

## 推送步骤

```bash
# 1. 进入项目
cd /Users/billchen/Documents/project/stock-radar

# 2. 临时绑定远程（仅一次）
git remote add origin https://github.com/chengwenchen06-sudo/stock-radar.git

# 3. 推送
git push -u origin main

# 4. 推完断开 origin（避免下次误推未审内容）
git remote remove origin
```

或者一键脚本（带二次确认）：

```bash
git remote add origin https://github.com/chengwenchen06-sudo/stock-radar.git && \
git push -u origin main && \
git remote remove origin
```

## 触发 Actions

仓库在 GitHub 端有 Actions workflow，push 完后会自动：
1. 跑 `python scripts/update_news.py` 抓 RSS/巨潮/SEC/港交所
2. 生成 `data/*.json`
3. 部署到 GitHub Pages

手动触发一次：

```bash
gh workflow run update-news.yml --repo chengwenchen06-sudo/stock-radar
```

## Pages 配置

- Source: GitHub Actions
- URL: https://chengwenchen06-sudo.github.io/stock-radar/
- 启用方式：`gh api -X POST /repos/chengwenchen06-sudo/stock-radar/pages -f build_type=workflow -f source.branch=main -f source.path=/`

## 私有信源（可选）

想加自己的 RSS 列表走私有通道，不要提交 `feeds/follow.opml`：

1. 本地写好 `feeds/follow.opml`
2. 仓库 Settings → Secrets → New repository secret
   - Name: `FOLLOW_OPML_B64`
   - Value: `base64 -i feeds/follow.opml`
3. Actions 自动解码并使用

## 注意

- **每次 push 前看一眼 `git diff --stat`**，避免误推隐私内容（如 `feeds/follow.opml`）
- 仓库已配置 `.gitignore` 忽略 `.venv/`、`__pycache__/`、`feeds/follow.opml`
