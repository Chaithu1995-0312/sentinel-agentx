# GitHub setup — Sentinel (AgentX)

**Suggested repo name:** `sentinel-agentx`  
**Visibility:** Public (hackathon judges need link access)  
**License:** MIT

## Exact steps

1. Sign in to GitHub → **New repository**
2. Name: `sentinel-agentx` (or `chaithu1705-sentinel` if that name is taken)
3. Description: `AgentX 2026 PS-02 — Sentinel paper-trading agent (Chaithu1705)`
4. Public · **Do not** add README / .gitignore / license via the GitHub UI if you will push this tree (avoids merge noise). Or add them empty — then pull first.
5. Create repository
6. Invite Naveena: Settings → Collaborators → Add (Write)
7. On your machine:

```bash
cd /path/to/workspace/agentx/build   # or copy build contents to a clean folder
git init
git branch -M main
# add .gitignore and LICENSE and README (templates below if missing)
git add .
git status   # confirm no .env, no keys
git commit -m "chore: pre-build scaffold for AgentX Sentinel"
git remote add origin https://github.com/<YOUR_USER>/sentinel-agentx.git
git push -u origin main
```

8. Verify: open the GitHub URL in a private window (public OK)
9. Pin the repo or star it for quick find during the hackathon

## `.gitignore` (create at repo root)

```
.env
.env.*
!.env.example
__pycache__/
*.py[cod]
.venv/
venv/
.pytest_cache/
.mypy_cache/
.DS_Store
*.mp4
.idea/
.vscode/
dist/
build_artifacts/
```

(Keep `env.example`; never commit `.env`.)

## `LICENSE` (MIT)

```
MIT License

Copyright (c) 2026 Chaithu1705 (Chaithanya Kumar, Naveena)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## First `README.md` placeholder (repo root — replace compose path if needed)

```markdown
# Sentinel (AgentX 2026 PS-02)

Paper-trading agentic assistant — **no real money**. Team **Chaithu1705**.

## Quick start

```bash
cp env.example .env
docker compose up --build
```

Health checks: ports 8001–8006. See `PREBUILD_RUNBOOK.md` and `ARCHITECTURE.md`.

## Status

Pre-build scaffold. Strategy + LLM agent brain land during the 24h build (11–12 Nov 2026 IST).
```

## Security check before every push

```bash
git status
grep -R "API_KEY\|sk-" --exclude-dir=.git --exclude=env.example . || true
```
