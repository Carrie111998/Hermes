#!/usr/bin/env python3
"""
Seed Data for Team Memory - 芯相业务场景
"""

SEED_MEMORIES = [
    {
        "category": "architecture_decision",
        "title": "选择JWT而非Session认证",
        "content": """决策时间: 2025-11-03

决策: 采用JWT token进行用户认证，放弃传统Session方案

理由:
1. 支持跨服务认证（微服务架构需求）
2. 无状态设计，易于水平扩展
3. 移动端和Web端统一认证方式
4. 减少服务器内存压力（无需存储session）

反对意见:
- Session更简单，更易于撤销
- JWT token size较大

最终结论: JWT的扩展性优势超过实现复杂度，适合我们的微服务架构

参与者: backend_agent, devops_agent
相关文档: docs/architecture/auth-design.md""",
        "author": "backend_agent",
        "tags": ["auth", "jwt", "architecture", "microservices"]
    },
    {
        "category": "api_contract",
        "title": "User API v1 契约",
        "content": """Endpoint: POST /api/users

Request Body:
{
    "name": "string (required, 2-50 chars)",
    "email": "string (required, valid email)",
    "password": "string (required, min 8 chars)",
    "role": "string (optional, default: 'user')"
}

Response 201 Created:
{
    "id": "uuid",
    "name": "string",
    "email": "string",
    "role": "string",
    "created_at": "ISO8601 timestamp"
}

Error Responses:
- 400: Invalid input
- 409: Email already exists

认证: 不需要（注册接口）

最后更新: 2026-07-15
维护者: backend_agent""",
        "author": "backend_agent",
        "tags": ["api", "user", "contract", "rest"]
    },
    {
        "category": "best_practice",
        "title": "错误处理最佳实践",
        "content": """团队约定的错误处理规范:

1. 使用统一错误格式:
{
    "error": {
        "code": "USER_NOT_FOUND",
        "message": "用户不存在",
        "details": {...}
    }
}

2. 错误码命名规范:
- 大写蛇形: USER_NOT_FOUND, INVALID_INPUT
- 语义明确: 一看就懂问题

3. 日志记录:
- 用户错误(4xx): info级别
- 系统错误(5xx): error级别
- 包含request_id用于追踪

4. 不暴露内部细节:
- ❌ "Database connection failed"
- ✅ "服务暂时不可用，请稍后重试"

参考: docs/coding-standards/error-handling.md""",
        "author": "backend_agent",
        "tags": ["error-handling", "best-practice", "coding-standard"]
    },
    {
        "category": "architecture_decision",
        "title": "数据库选型：PostgreSQL",
        "content": """决策时间: 2025-10-15

决策: 主数据库使用PostgreSQL 14+

考虑的选项:
1. PostgreSQL ✅ 最终选择
2. MySQL - 熟悉但功能受限
3. MongoDB - 不适合关系型数据

选择PostgreSQL的理由:
1. JSONB支持 - 灵活处理半结构化数据
2. 全文搜索 - 内置FTS功能
3. 复杂查询 - CTE、Window Functions
4. 成熟稳定 - 企业级可靠性
5. 扩展性好 - PostGIS、pg_vector等

Trade-offs:
- 学习曲线略高
- 但长期收益大于成本

参与者: backend_agent, devops_agent""",
        "author": "devops_agent",
        "tags": ["database", "postgresql", "architecture"]
    },
    {
        "category": "api_contract",
        "title": "Product API v1 契约",
        "content": """GET /api/products

Query Parameters:
- page: integer (default: 1)
- limit: integer (default: 20, max: 100)
- category: string (optional)
- search: string (optional, fuzzy search)

Response 200:
{
    "data": [
        {
            "id": "uuid",
            "name": "string",
            "price": "decimal",
            "category": "string",
            "stock": "integer"
        }
    ],
    "pagination": {
        "page": 1,
        "limit": 20,
        "total": 100
    }
}

认证: Bearer token required

最后更新: 2026-07-20
维护者: backend_agent""",
        "author": "backend_agent",
        "tags": ["api", "product", "contract", "pagination"]
    },
    {
        "category": "best_practice",
        "title": "React状态管理最佳实践",
        "content": """团队约定的React状态管理规范:

1. 状态分类:
- Local state: useState (组件私有)
- Shared state: Context (跨组件共享)
- Server state: React Query (服务器数据)

2. 避免prop drilling:
- 使用Context而非层层传递
- 或使用composition（children）

3. 性能优化:
- 使用useMemo缓存计算结果
- 使用useCallback缓存回调函数
- 避免在render中创建对象/数组

4. 命名规范:
- state变量: camelCase
- setter: set + PascalCase (e.g., setUserName)

参考: docs/frontend/state-management.md""",
        "author": "frontend_agent",
        "tags": ["react", "state-management", "best-practice", "frontend"]
    },
    {
        "category": "architecture_decision",
        "title": "前后端部署分离",
        "content": """决策时间: 2025-12-01

决策: 前端和后端独立部署，通过Nginx反向代理

架构:
- Frontend: Vercel/Netlify (静态托管)
- Backend: AWS ECS (容器化部署)
- Nginx: 统一入口，反向代理

优势:
1. 独立扩展 - 前后端可独立scale
2. 独立发布 - 前端更新不影响后端
3. CDN优化 - 前端静态资源全球加速
4. 技术栈灵活 - 可选择最佳工具

Trade-offs:
- CORS配置需要额外注意
- 部署流程相对复杂

参与者: devops_agent, frontend_agent, backend_agent""",
        "author": "devops_agent",
        "tags": ["deployment", "architecture", "nginx", "cdn"]
    },
    {
        "category": "api_contract",
        "title": "Auth API v1 契约",
        "content": """POST /api/auth/login

Request Body:
{
    "email": "string (required)",
    "password": "string (required)"
}

Response 200:
{
    "token": "jwt_token_string",
    "user": {
        "id": "uuid",
        "name": "string",
        "email": "string",
        "role": "string"
    },
    "expires_at": "ISO8601 timestamp"
}

Error Responses:
- 401: Invalid credentials
- 429: Too many attempts

POST /api/auth/refresh

Request Headers:
- Authorization: Bearer <refresh_token>

Response 200:
{
    "token": "new_jwt_token",
    "expires_at": "ISO8601 timestamp"
}

最后更新: 2026-07-18""",
        "author": "backend_agent",
        "tags": ["api", "auth", "jwt", "contract"]
    },
    {
        "category": "best_practice",
        "title": "Git分支管理规范",
        "content": """团队Git工作流:

1. 分支命名:
- feature/功能名 (新功能)
- fix/bug描述 (bug修复)
- hotfix/紧急修复 (生产问题)

2. Commit message规范:
格式: <type>(<scope>): <subject>

Types:
- feat: 新功能
- fix: bug修复
- docs: 文档更新
- refactor: 代码重构
- test: 测试相关

示例:
- feat(auth): 添加JWT认证
- fix(api): 修复用户查询bug

3. PR流程:
- 至少1人review
- CI通过
- 冲突解决后merge

参考: docs/workflow/git-convention.md""",
        "author": "backend_agent",
        "tags": ["git", "workflow", "best-practice"]
    },
    {
        "category": "architecture_decision",
        "title": "API版本管理策略",
        "content": """决策时间: 2026-01-10

决策: 采用URL路径版本管理

格式: /api/v1/resource, /api/v2/resource

理由:
1. 简单直观 - URL一看就知道版本
2. 易于路由 - 不同版本可以独立部署
3. 向后兼容 - 旧版本可以长期维护

弃用的方案:
- Header versioning (不够直观)
- Query parameter (容易遗漏)

版本策略:
- v1: 当前稳定版
- v2: 引入breaking changes时升级
- 旧版本至少维护6个月

参与者: backend_agent, frontend_agent""",
        "author": "backend_agent",
        "tags": ["api", "versioning", "architecture"]
    }
]


def load_seed_data(workspace_id="xinxiang", db_path=None):
    """Load seed data idempotently into one explicit workspace.

    Seeds are operator-controlled fixtures, not an agent write path. The
    memory key makes repeated loads a no-op instead of duplicating entries.
    """
    from plugins.team_memory.storage import add_memory, list_all_memories

    existing = {
        row.get("memory_key")
        for row in list_all_memories(
            workspace_id=workspace_id,
            db_path=db_path,
            include_drafts=True,
            include_expired=True,
        )
    }
    count = 0
    for memory in SEED_MEMORIES:
        memory_key = f"seed-{memory['title']}"
        try:
            add_memory(
                category=memory["category"],
                title=memory["title"],
                content=memory["content"],
                author=memory["author"],
                tags=memory["tags"],
                workspace_id=workspace_id,
                db_path=db_path,
                memory_key=memory_key,
            )
            if memory_key not in existing:
                count += 1
        except Exception as e:
            print(f"Warning: Failed to add '{memory['title']}': {e}")

    return count


if __name__ == "__main__":
    # Direct execution is an operator fixture loader and requires an explicit
    # workspace so it cannot accidentally seed the wrong profile.
    import argparse

    from plugins.team_memory.storage import init_database

    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--db", default=None)
    args = parser.parse_args()
    init_database(args.db, workspace_id=args.workspace)
    count = load_seed_data(workspace_id=args.workspace, db_path=args.db)
    print(f"Loaded {count} seed memories")
