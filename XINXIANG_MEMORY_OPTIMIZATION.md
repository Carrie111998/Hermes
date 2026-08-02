# 芯相业务场景：多Agent协作记忆机制优化方案

**业务背景**: 多机器人(agent profile) + 前后端应用系统 + 数据 的协作体系

---

## 一、业务场景分析

### 1.1 芯相业务特点

```
┌─────────────────────────────────────────────────────────────┐
│                    芯相协作生态系统                          │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │ Agent Profile│  │ Agent Profile│  │ Agent Profile│      │
│  │   Frontend   │  │   Backend    │  │   DevOps     │      │
│  │   Specialist │  │   Specialist │  │   Specialist │      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
│         │                  │                  │              │
│         └──────────────────┴──────────────────┘              │
│                            │                                 │
│                    ┌───────▼────────┐                        │
│                    │  Shared Memory  │                       │
│                    │    Knowledge    │                       │
│                    │   Graph Layer   │                       │
│                    └───────┬────────┘                        │
│                            │                                 │
│         ┌──────────────────┼──────────────────┐              │
│         │                  │                  │              │
│  ┌──────▼───────┐  ┌──────▼──────┐  ┌────────▼──────┐      │
│  │  前端代码库  │  │  后端API     │  │  数据库/架构  │      │
│  │  React/Vue   │  │  Node/Python │  │  PostgreSQL   │      │
│  └──────────────┘  └─────────────┘  └───────────────┘      │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### 1.2 核心需求

#### **协作场景**
1. **前端Agent**: "用户反馈登录页加载慢"
2. **后端Agent**: "检查auth API，发现N+1查询问题"
3. **DevOps Agent**: "数据库索引缺失，已添加"
4. **Frontend Agent需要知道**: 后端修复进度，无需重复报告

#### **知识共享需求**
- **跨Agent**: Frontend修改的组件，Backend需要知道API契约
- **跨会话**: 上周讨论的架构决策，本周实施时要recall
- **跨项目**: ProjectA的解决方案，适用于ProjectB类似问题
- **跨时间**: 3个月前的设计文档，现在重构时需要理解context

#### **数据关联需求**
- **代码↔️对话**: "这个bug在哪次会话中讨论过？"
- **架构↔️决策**: "为什么选择微服务而非单体？"
- **性能↔️变更**: "哪次提交导致了响应时间增加？"
- **用户反馈↔️改进**: "这个功能请求的原始需求是什么？"

---

## 二、当前Hermes架构的Gap

### 2.1 单Agent导向设计
**现状**: Hermes设计为单用户↔️单Agent  
**问题**: 
- 无多Agent协作的记忆同步机制
- Agent间无法共享知识图谱
- 无团队级记忆空间

### 2.2 无结构化数据关联
**现状**: 主要文本记忆，代码/DB/架构图作为文件路径存储  
**问题**:
- 无法关联"这段代码 + 那次讨论 + 这个PR"
- 无法查询"所有关于认证模块的知识"
- 无法追溯"这个API endpoint的完整演化史"

### 2.3 无角色专业化
**现状**: 单一记忆空间，所有知识混在一起  
**问题**:
- Frontend Agent看到大量Backend细节（噪音）
- Backend Agent无法过滤只看架构级决策
- DevOps Agent被业务逻辑讨论淹没

---

## 三、芯相定制化记忆架构

### 3.1 整体架构：四层记忆 + 知识图谱

```
┌─────────────────────────────────────────────────────────────┐
│  Layer 4: 团队共享记忆 (Team Shared Memory)                 │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ • 架构决策记录 (ADR)                                    │ │
│  │ • 技术选型文档                                          │ │
│  │ • API契约/接口规范                                      │ │
│  │ • 通用解决方案库                                        │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Layer 3: Agent Profile记忆 (Role-Specific Memory)          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │ Frontend │  │ Backend  │  │ DevOps   │  │ PM/QA    │   │
│  │ Expertise│  │ Expertise│  │ Expertise│  │ Expertise│   │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘   │
└─────────────────────────────────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Layer 2: 项目记忆 (Project Memory)                         │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ • 代码库历史 (git log + discussions)                    │ │
│  │ • Issue/PR关联的上下文                                  │ │
│  │ • 性能指标 + 变更追踪                                    │ │
│  │ • 用户反馈 + 功能迭代                                    │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  Layer 1: 个人工作记忆 (Personal Working Memory)            │
│  • 当前会话上下文                                            │
│  • 临时笔记/草稿                                             │
│  • 待办任务                                                  │
└─────────────────────────────────────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────────────────────┐
│       中心知识图谱 (Central Knowledge Graph)                 │
│  ┌────────────────────────────────────────────────────────┐ │
│  │ 实体:                                                   │ │
│  │  - Agent (frontend_agent, backend_agent, ...)          │ │
│  │  - User (developer_xinxin, pm_alice, ...)             │ │
│  │  - Project (xinxiang_web, xinxiang_api, ...)          │ │
│  │  - Component (AuthModule, UserService, ...)           │ │
│  │  - File (src/auth.ts, api/user.py, ...)               │ │
│  │  - Concept (Microservices, JWT, React Hook, ...)      │ │
│  │  - Issue (bug-1234, feature-567, ...)                 │ │
│  │  - Decision (ADR-001, ADR-002, ...)                    │ │
│  │                                                         │ │
│  │ 关系:                                                   │ │
│  │  - WORKS_ON: Agent → Project                           │ │
│  │  - SPECIALIZES_IN: Agent → Concept                     │ │
│  │  - DEPENDS_ON: Component → Component                   │ │
│  │  - DISCUSSES: Agent → Issue (in Session)               │ │
│  │  - MODIFIES: Agent → File (with timestamp)             │ │
│  │  - RELATES_TO: Issue → Decision                        │ │
│  │  - IMPLEMENTS: File → Concept                          │ │
│  │  - CAUSED_BY: Performance_Issue → Code_Change          │ │
│  └────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 核心设计原则

#### **原则1: 分层可见性 (Layered Visibility)**
```python
# 配置示例
agent_memory_config = {
    "frontend_agent": {
        "primary": ["personal", "project_frontend"],  # 自己的记忆
        "shared_read": ["team_architecture", "team_api_contracts"],  # 共享架构
        "filtered_access": {
            "project_backend": {
                "filter": "only API contracts, not implementation"
            }
        }
    },
    "backend_agent": {
        "primary": ["personal", "project_backend"],
        "shared_read": ["team_architecture", "team_decisions"],
        "filtered_access": {
            "project_frontend": {
                "filter": "component interactions, state management patterns"
            }
        }
    }
}
```

#### **原则2: 关联为王 (Relationships First)**
每条记忆必须关联到知识图谱节点：
```cypher
// 示例：记录一次性能优化
CREATE (s:Session {id: "sess_2026_08_02_001"})
CREATE (a:Agent {name: "backend_agent"})
CREATE (i:Issue {id: "perf-456", title: "API响应慢"})
CREATE (f:File {path: "api/user.py"})
CREATE (c:Commit {sha: "abc123", message: "优化N+1查询"})
CREATE (perf:PerformanceMetric {
    endpoint: "/api/users",
    latency_before: "500ms",
    latency_after: "50ms"
})

CREATE (a)-[:DISCUSSES]->(i)
CREATE (a)-[:MODIFIES {timestamp: "2026-08-02T10:30:00"}]->(f)
CREATE (c)-[:FIXES]->(i)
CREATE (c)-[:IMPROVES]->(perf)
CREATE (s)-[:CONTAINS]->(i)
```

#### **原则3: 多模态关联 (Multi-Modal Linking)**
```
代码片段 (Code Snippet)
    ↓ DISCUSSED_IN
会话记录 (Session Message)
    ↓ REFERENCES
架构图 (Architecture Diagram)
    ↓ IMPLEMENTS
架构决策 (ADR Document)
    ↓ MOTIVATED_BY
性能数据 (Performance Metrics)
```

---

## 四、具体技术实现方案

### 4.1 扩展Memory Provider接口

```python
# plugins/memory/xinxiang_team_memory/__init__.py

from agent.memory_provider import MemoryProvider
from typing import Dict, List, Any, Optional

class XinXiangTeamMemoryProvider(MemoryProvider):
    """
    芯相团队协作记忆Provider
    
    特性:
    - 多Agent共享知识图谱
    - 角色专业化过滤
    - 项目/组件级记忆空间
    - 跨模态关联 (代码+对话+文档+数据)
    """
    
    @property
    def name(self) -> str:
        return "xinxiang_team"
    
    def initialize(self, session_id: str, **kwargs) -> None:
        # 从kwargs提取agent身份
        self.agent_profile = kwargs.get("agent_identity", "general")  # frontend/backend/devops
        self.agent_workspace = kwargs.get("agent_workspace", "xinxiang")
        self.user_id = kwargs.get("user_id", "default_user")
        
        # 连接知识图谱
        self._kg = self._init_knowledge_graph()
        
        # 加载角色专业化配置
        self._role_config = self._load_role_config(self.agent_profile)
        
        # 初始化多层记忆
        self._personal_memory = PersonalMemoryStore()
        self._project_memory = ProjectMemoryStore(workspace=self.agent_workspace)
        self._team_memory = TeamSharedMemoryStore(workspace=self.agent_workspace)
        self._role_memory = RoleSpecificMemoryStore(role=self.agent_profile)
    
    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """
        多层记忆融合检索
        """
        results = []
        
        # 1. 个人工作记忆 (最高优先级)
        personal_hits = self._personal_memory.search(query, limit=5)
        results.extend(self._format_hits(personal_hits, source="personal"))
        
        # 2. 项目记忆 (根据角色过滤)
        project_hits = self._project_memory.search(
            query, 
            limit=10,
            role_filter=self._role_config.get("project_filters", [])
        )
        results.extend(self._format_hits(project_hits, source="project"))
        
        # 3. 角色专业记忆
        role_hits = self._role_memory.search(query, limit=5)
        results.extend(self._format_hits(role_hits, source="role_expertise"))
        
        # 4. 团队共享记忆 (架构/决策)
        team_hits = self._team_memory.search(
            query,
            categories=self._role_config.get("team_categories", ["architecture", "api_contracts"])
        )
        results.extend(self._format_hits(team_hits, source="team_shared"))
        
        # 5. 知识图谱推理增强
        kg_context = self._kg.contextual_reasoning(
            query=query,
            agent=self.agent_profile,
            recent_entities=self._extract_entities_from_session(session_id)
        )
        
        # 融合 + 去重 + 排序
        final_context = self._fusion_and_rank(results, kg_context)
        
        return self._render_context(final_context)
    
    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """
        多层级同步写入 + 知识图谱更新
        """
        # 1. 个人记忆 (当前会话)
        self._personal_memory.add_turn(user_content, assistant_content, session_id)
        
        # 2. 实体抽取 (NER + 关系提取)
        entities = self._extract_entities(user_content, assistant_content)
        
        # 3. 更新知识图谱
        self._kg.update_from_turn(
            agent=self.agent_profile,
            session_id=session_id,
            entities=entities,
            user_msg=user_content,
            asst_msg=assistant_content,
            timestamp=self._get_timestamp()
        )
        
        # 4. 智能分类存储
        classification = self._classify_turn_content(user_content, assistant_content)
        
        if classification.is_architecture_decision:
            self._team_memory.add_decision(
                content=classification.decision_text,
                category="architecture",
                related_entities=entities
            )
        
        if classification.is_expertise_knowledge:
            self._role_memory.add_expertise(
                content=classification.expertise_text,
                domain=classification.domain,  # e.g. "react_hooks", "sql_optimization"
                related_entities=entities
            )
        
        if classification.mentions_code:
            self._project_memory.link_code_discussion(
                files=classification.mentioned_files,
                discussion=assistant_content,
                session_id=session_id
            )
    
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            TEAM_MEMORY_SEARCH_SCHEMA,
            KNOWLEDGE_GRAPH_QUERY_SCHEMA,
            CROSS_AGENT_SHARE_SCHEMA,
            PROJECT_MEMORY_TIMELINE_SCHEMA,
            ARCHITECTURE_DECISION_RECORD_SCHEMA,
        ]
    
    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "team_memory_search":
            return self._handle_team_search(args)
        elif tool_name == "knowledge_graph_query":
            return self._handle_kg_query(args)
        elif tool_name == "cross_agent_share":
            return self._handle_cross_agent_share(args)
        elif tool_name == "project_memory_timeline":
            return self._handle_project_timeline(args)
        elif tool_name == "architecture_decision_record":
            return self._handle_adr(args)
        else:
            raise NotImplementedError(f"Tool {tool_name} not implemented")
```

### 4.2 知识图谱核心实现

```python
# plugins/memory/xinxiang_team_memory/knowledge_graph.py

from typing import List, Dict, Any
import neo4j  # 或 SQLite + graph extension

class XinXiangKnowledgeGraph:
    """
    芯相团队知识图谱
    
    核心能力:
    - 实体/关系建模
    - 路径查询 (transitive reasoning)
    - 时间线追踪
    - 影响分析
    """
    
    def __init__(self, db_url: str):
        self.driver = neo4j.GraphDatabase.driver(db_url)
    
    def contextual_reasoning(
        self,
        query: str,
        agent: str,
        recent_entities: List[str]
    ) -> Dict[str, Any]:
        """
        基于查询和上下文的图谱推理
        
        示例场景:
        - 查询: "为什么选择JWT而不是session?"
        - Agent: backend_agent
        - recent_entities: ["AuthModule", "UserService"]
        
        推理:
        1. 找到 AuthModule 相关的所有架构决策
        2. 找到 JWT 相关的历史讨论
        3. 找到做出决策的Agent和时间
        4. 返回完整决策链
        """
        with self.driver.session() as session:
            # Cypher查询示例
            result = session.run("""
                // 找到与query相关的决策
                MATCH (d:Decision)
                WHERE d.title CONTAINS $keyword OR d.content CONTAINS $keyword
                
                // 找到决策关联的组件
                OPTIONAL MATCH (d)-[:AFFECTS]->(c:Component)
                WHERE c.name IN $recent_entities
                
                // 找到做出决策的Agent和会话
                OPTIONAL MATCH (a:Agent)-[:MADE_DECISION]->(d)
                OPTIONAL MATCH (s:Session)-[:CONTAINS]->(d)
                
                // 找到反对意见和替代方案
                OPTIONAL MATCH (d)-[:REJECTS]->(alt:Alternative)
                
                RETURN d, c, a, s, alt
                ORDER BY d.timestamp DESC
                LIMIT 5
            """, keyword="JWT", recent_entities=recent_entities)
            
            return self._format_reasoning_result(result)
    
    def update_from_turn(
        self,
        agent: str,
        session_id: str,
        entities: Dict[str, Any],
        user_msg: str,
        asst_msg: str,
        timestamp: str
    ) -> None:
        """
        从对话turn更新知识图谱
        """
        with self.driver.session() as session:
            # 创建或更新实体
            for entity_type, entity_list in entities.items():
                for entity in entity_list:
                    session.run("""
                        MERGE (e:%s {name: $name})
                        ON CREATE SET e.created = $timestamp, e.first_mentioned_by = $agent
                        ON MATCH SET e.last_updated = $timestamp
                    """ % entity_type, name=entity['name'], timestamp=timestamp, agent=agent)
            
            # 创建关系
            for relation in entities.get('relations', []):
                session.run("""
                    MATCH (a {name: $from_entity})
                    MATCH (b {name: $to_entity})
                    MERGE (a)-[r:%s]->(b)
                    SET r.discovered_in = $session_id, r.timestamp = $timestamp
                """ % relation['type'], 
                from_entity=relation['from'],
                to_entity=relation['to'],
                session_id=session_id,
                timestamp=timestamp)
            
            # 链接Agent和Session
            session.run("""
                MERGE (a:Agent {name: $agent})
                MERGE (s:Session {id: $session_id})
                MERGE (a)-[:PARTICIPATED_IN {timestamp: $timestamp}]->(s)
            """, agent=agent, session_id=session_id, timestamp=timestamp)
    
    def query_related_discussions(self, entity_name: str, agent_role: str = None) -> List[Dict]:
        """
        查询某个实体的所有相关讨论
        
        示例: "AuthModule被哪些Agent在哪些Session中讨论过?"
        """
        with self.driver.session() as session:
            query = """
                MATCH (e {name: $entity_name})
                MATCH (s:Session)-[:MENTIONS]->(e)
                MATCH (a:Agent)-[:PARTICIPATED_IN]->(s)
            """
            if agent_role:
                query += " WHERE a.role = $agent_role"
            
            query += """
                RETURN a.name as agent, s.id as session_id, s.timestamp as when,
                       [(s)-[:CONTAINS]->(m:Message) | m.content][0..3] as excerpts
                ORDER BY s.timestamp DESC
                LIMIT 20
            """
            
            result = session.run(query, entity_name=entity_name, agent_role=agent_role)
            return [record.data() for record in result]
    
    def trace_change_impact(self, file_path: str, timestamp: str) -> Dict[str, Any]:
        """
        追踪代码变更的影响链
        
        示例: "api/user.py 在2026-08-02的修改影响了哪些组件?"
        
        返回:
        - 直接依赖的文件
        - 间接依赖的组件
        - 相关的讨论和决策
        - 可能影响的Agent
        """
        with self.driver.session() as session:
            result = session.run("""
                MATCH (f:File {path: $file_path})
                MATCH (c:Commit)-[:MODIFIES]->(f)
                WHERE c.timestamp >= $timestamp
                
                // 直接依赖
                OPTIONAL MATCH (f)-[:IMPORTS|CALLS]->(dep:File)
                
                // 实现的组件
                OPTIONAL MATCH (f)-[:IMPLEMENTS]->(comp:Component)
                
                // 组件的间接依赖
                OPTIONAL MATCH (comp)-[:DEPENDS_ON*1..3]->(indirect:Component)
                
                // 相关讨论
                OPTIONAL MATCH (s:Session)-[:DISCUSSES]->(f)
                OPTIONAL MATCH (a:Agent)-[:PARTICIPATED_IN]->(s)
                
                RETURN f, c, collect(DISTINCT dep) as direct_deps,
                       collect(DISTINCT comp) as components,
                       collect(DISTINCT indirect) as indirect_deps,
                       collect(DISTINCT {agent: a.name, session: s.id}) as discussions
            """, file_path=file_path, timestamp=timestamp)
            
            return result.single().data()
```

### 4.3 角色专业化配置

```yaml
# config/agent_profiles/frontend_agent.yaml

agent_profile: frontend_agent

memory_config:
  # 个人记忆容量
  personal_memory_limit: 50000  # 50KB (工作记忆更大)
  
  # 项目记忆访问权限
  project_memory:
    primary: ["xinxiang_web_frontend"]
    readable: ["xinxiang_web_backend", "xinxiang_api"]
    filters:
      xinxiang_web_backend:
        - "only API endpoints and contracts"
        - "exclude internal implementation"
      xinxiang_api:
        - "only data models and GraphQL schemas"
  
  # 团队共享记忆订阅
  team_memory:
    subscribed_categories:
      - "architecture_decisions"
      - "api_contracts"
      - "design_system"
      - "frontend_best_practices"
    excluded_categories:
      - "database_schemas"  # DevOps专属
      - "infrastructure"    # DevOps专属
  
  # 角色专业知识
  role_expertise:
    domains:
      - "react"
      - "vue"
      - "typescript"
      - "css_animations"
      - "webpack"
      - "state_management"
    
    # 关注的实体类型
    entity_focus:
      - "Component"
      - "Hook"
      - "Route"
      - "UI_Pattern"
    
    # 自动过滤掉的噪音
    noise_filter:
      - entity_types: ["DatabaseTable", "K8sDeployment"]
      - keywords: ["SQL query", "docker image", "terraform"]

# 知识图谱查询模板
kg_query_templates:
  - name: "find_component_discussions"
    description: "查找某个React组件的所有讨论"
    cypher: |
      MATCH (c:Component {name: $component_name, type: 'react'})
      MATCH (s:Session)-[:DISCUSSES]->(c)
      MATCH (a:Agent)-[:PARTICIPATED_IN]->(s)
      WHERE a.role IN ['frontend_agent', 'pm_agent', 'designer_agent']
      RETURN s, a, c
      ORDER BY s.timestamp DESC

  - name: "find_related_state_management"
    description: "查找状态管理相关的最佳实践"
    cypher: |
      MATCH (concept:Concept {name: 'state_management'})
      MATCH (pattern:Pattern)-[:IMPLEMENTS]->(concept)
      MATCH (file:File)-[:USES]->(pattern)
      OPTIONAL MATCH (d:Decision)-[:RECOMMENDS]->(pattern)
      RETURN pattern, file, d
      ORDER BY d.timestamp DESC

# 工具定义
tools:
  - name: "frontend_memory_search"
    description: |
      搜索前端相关的记忆（组件、hooks、路由、状态管理等）
      自动过滤后端实现细节，只返回API契约和前端相关内容
    parameters:
      query: "搜索词或自然语言问题"
      scope: "personal | project | team | all"
      entity_types: ["Component", "Hook", "Route", "Pattern"]
  
  - name: "component_evolution_timeline"
    description: "查看某个组件的完整演化历史"
    parameters:
      component_name: "组件名称 (如 'UserProfile', 'LoginForm')"
      include_discussions: true
      include_related_issues: true

---

## 五、关键工具设计

### 5.1 团队记忆搜索工具

```python
TEAM_MEMORY_SEARCH_SCHEMA = {
    "name": "team_memory_search",
    "description": (
        "搜索团队共享记忆，包括架构决策、技术选型、API契约、最佳实践等。"
        "自动根据你的Agent角色过滤相关内容。"
        "示例查询："
        "- '为什么选择PostgreSQL而不是MongoDB?'"
        "- '用户认证的API契约是什么?'"
        "- '所有关于状态管理的讨论'"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索词或自然语言问题"
            },
            "category": {
                "type": "string",
                "enum": [
                    "all",
                    "architecture_decisions",
                    "api_contracts",
                    "best_practices",
                    "design_patterns",
                    "troubleshooting_guides"
                ],
                "description": "记忆分类，默认all搜索所有类别"
            },
            "agent_perspective": {
                "type": "string",
                "enum": ["my_role", "frontend", "backend", "devops", "all_roles"],
                "description": "从哪个角色视角检索，默认my_role（你的角色）"
            },
            "include_related_discussions": {
                "type": "boolean",
                "description": "是否包含相关的历史讨论，默认true"
            }
        },
        "required": ["query"]
    }
}
```

### 5.2 知识图谱查询工具

```python
KNOWLEDGE_GRAPH_QUERY_SCHEMA = {
    "name": "knowledge_graph_query",
    "description": (
        "查询团队知识图谱，进行复杂的关联推理。"
        "适用场景："
        "- '这个组件被哪些其他组件依赖?'"
        "- '找出AuthModule的所有相关讨论和决策'"
        "- '追踪这次代码变更的影响范围'"
        "- '谁在什么时候讨论过这个话题?'"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query_type": {
                "type": "string",
                "enum": [
                    "entity_history",         # 实体的完整历史
                    "dependency_chain",       # 依赖链
                    "impact_analysis",        # 影响分析
                    "related_discussions",    # 相关讨论
                    "decision_rationale",     # 决策推理
                    "custom_cypher"           # 自定义Cypher查询
                ],
                "description": "查询类型"
            },
            "entity": {
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["Component", "File", "Concept", "Issue", "Decision", "Agent"],
                        "description": "实体类型"
                    },
                    "name": {
                        "type": "string",
                        "description": "实体名称"
                    }
                },
                "description": "要查询的实体"
            },
            "custom_cypher": {
                "type": "string",
                "description": "自定义Cypher查询（仅当query_type=custom_cypher时）"
            },
            "max_depth": {
                "type": "integer",
                "description": "图遍历最大深度，默认3",
                "default": 3
            }
        },
        "required": ["query_type"]
    }
}
```

### 5.3 跨Agent共享工具

```python
CROSS_AGENT_SHARE_SCHEMA = {
    "name": "cross_agent_share",
    "description": (
        "与其他Agent共享信息或查询其他Agent的视角。"
        "适用场景："
        "- 通知Frontend Agent: 'API已修复，可以重新测试'"
        "- 查询Backend Agent: '用户认证的实现细节是什么?'"
        "- 请求DevOps Agent: '能否检查生产环境的数据库连接?'"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["notify", "query", "request_help"],
                "description": "操作类型"
            },
            "target_agent": {
                "type": "string",
                "enum": ["frontend_agent", "backend_agent", "devops_agent", "pm_agent", "all"],
                "description": "目标Agent"
            },
            "message": {
                "type": "string",
                "description": "消息内容或查询问题"
            },
            "priority": {
                "type": "string",
                "enum": ["low", "normal", "high", "urgent"],
                "default": "normal",
                "description": "优先级"
            },
            "related_entities": {
                "type": "array",
                "items": {"type": "string"},
                "description": "相关实体列表（如组件名、文件路径等）"
            }
        },
        "required": ["action", "target_agent", "message"]
    }
}
```

### 5.4 项目记忆时间线工具

```python
PROJECT_MEMORY_TIMELINE_SCHEMA = {
    "name": "project_memory_timeline",
    "description": (
        "查看项目或组件的完整演化时间线，包括讨论、决策、代码变更、性能变化等。"
        "适用于理解'为什么会变成现在这样'的问题。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "enum": ["project", "component", "file", "concept"],
                "description": "时间线范围"
            },
            "target": {
                "type": "string",
                "description": "目标名称（项目名/组件名/文件路径/概念名）"
            },
            "time_range": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "开始时间 (ISO 8601)"},
                    "end": {"type": "string", "description": "结束时间 (ISO 8601)"}
                },
                "description": "时间范围，可选，默认查询全部历史"
            },
            "include": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": ["discussions", "decisions", "code_changes", "performance_metrics", "issues"]
                },
                "description": "包含的事件类型，默认全部"
            },
            "agent_filter": {
                "type": "array",
                "items": {"type": "string"},
                "description": "只看特定Agent的活动"
            }
        },
        "required": ["scope", "target"]
    }
}
```

---

## 六、实施路线图

### 阶段1: 基础架构搭建 (4-6周)

#### Week 1-2: 知识图谱基础设施
**目标**: 建立中心知识图谱存储

**任务**:
- [ ] 选择图数据库 (Neo4j Community vs SQLite+graph extension)
- [ ] 设计实体/关系schema
- [ ] 实现基础CRUD API
- [ ] 编写Cypher查询模板库
- [ ] 集成到Hermes MemoryProvider接口

**交付**:
- `plugins/memory/xinxiang_team_memory/knowledge_graph.py`
- 初始schema迁移脚本
- 单元测试 (覆盖率>80%)

#### Week 3-4: 多层记忆存储
**目标**: 实现四层记忆架构

**任务**:
- [ ] PersonalMemoryStore (基于现有MemoryStore扩展)
- [ ] ProjectMemoryStore (代码关联 + 会话链接)
- [ ] TeamSharedMemoryStore (分类存储 + 权限过滤)
- [ ] RoleSpecificMemoryStore (领域知识库)
- [ ] 实现融合检索算法 (RRF + 权重)

**交付**:
- 四层存储实现
- 融合检索算法
- 性能测试报告 (< 500ms p95 latency)

#### Week 5-6: Agent Profile配置系统
**目标**: 角色专业化配置和过滤

**任务**:
- [ ] YAML配置schema设计
- [ ] 配置加载和验证
- [ ] 基于角色的过滤器实现
- [ ] 预定义profile模板 (frontend/backend/devops/pm)

**交付**:
- 配置系统
- 4个预定义profile
- 配置文档

### 阶段2: 核心功能实现 (6-8周)

#### Week 7-9: 实体抽取和关系提取
**目标**: 自动从对话中构建知识图谱

**任务**:
- [ ] 集成NER模型 (spaCy + domain fine-tuning)
- [ ] 关系抽取模型 (RE transformer)
- [ ] 代码实体识别 (AST parsing + 语义分析)
- [ ] 实体消歧 (entity resolution)
- [ ] 置信度评分

**交付**:
- 实体抽取pipeline
- 准确率>85%的评估报告
- 实时抽取性能<200ms

#### Week 10-12: 智能分类和路由
**目标**: 自动将内容路由到合适的记忆层

**任务**:
- [ ] 内容分类模型训练 (架构决策/最佳实践/故障排查...)
- [ ] 重要性评分算法
- [ ] 自动ADR生成
- [ ] 噪音过滤规则

**交付**:
- 分类模型 (F1>0.9)
- 自动路由逻辑
- ADR模板生成器

#### Week 13-14: 工具集成
**目标**: 5个核心工具可用

**任务**:
- [ ] 实现5个工具的handler
- [ ] 工具schema注册到MemoryProvider
- [ ] 错误处理和降级策略
- [ ] 工具使用文档和示例

**交付**:
- 5个工具完全实现
- 集成测试套件
- 用户指南

### 阶段3: 高级特性 (4-6周)

#### Week 15-17: 跨Agent协作机制
**目标**: Agent间信息共享和通知

**任务**:
- [ ] Agent消息队列 (Redis pub/sub or in-memory)
- [ ] 通知触发规则引擎
- [ ] Agent间上下文传递
- [ ] 协作日志和审计

**交付**:
- 消息队列系统
- 通知规则引擎
- 协作demo视频

#### Week 18-20: 多模态关联
**目标**: 代码+对话+文档+数据的深度关联

**任务**:
- [ ] Git集成 (commit → discussion linking)
- [ ] Issue/PR关联 (GitHub/GitLab API)
- [ ] 性能监控集成 (metrics → code change)
- [ ] 文档embedding和关联 (markdown/PDF)

**交付**:
- Git hooks集成
- Issue tracker同步
- 性能回归检测
- 文档语义搜索

### 阶段4: 优化和polish (2-3周)

#### Week 21-22: 性能优化
**任务**:
- [ ] 查询性能profiling
- [ ] 图查询优化 (索引 + query plan)
- [ ] Embedding缓存
- [ ] 预热常见查询

**目标**:
- Prefetch < 300ms p95
- 知识图谱查询 < 500ms p95
- 内存占用 < 500MB (单agent)

#### Week 23: 用户体验polish
**任务**:
- [ ] 错误消息优化
- [ ] 工具使用引导
- [ ] 示例和最佳实践文档
- [ ] 配置向导

---

## 七、技术选型建议

### 7.1 知识图谱存储

#### **选项A: Neo4j Community Edition**
**优势**:
- 成熟的图数据库，Cypher查询强大
- 可视化工具丰富 (Neo4j Browser)
- 社区支持好，文档完善
- 复杂图推理性能优秀

**劣势**:
- 额外依赖，需要独立进程
- 内存占用较大 (~512MB基础)
- 配置和维护复杂度

**适用场景**: 企业级部署，复杂图查询需求高

#### **选项B: SQLite + AGE (Apache AGE Graph Extension)**
**优势**:
- 无额外依赖，Hermes已用SQLite
- 轻量级，适合嵌入式
- 事务支持好
- 熟悉的SQL + Cypher混合查询

**劣势**:
- AGE相对年轻，社区较小
- 复杂图查询性能不如Neo4j
- 可视化工具有限

**适用场景**: 个人/小团队，追求轻量级部署

#### **推荐**: 
- **MVP阶段**: SQLite + AGE (快速验证)
- **生产阶段**: Neo4j Community (迁移工具提供)

### 7.2 实体抽取

#### **选项A: spaCy + domain fine-tuning**
**优势**:
- 开源，本地运行
- 速度快 (<100ms per doc)
- 可自定义训练

**劣势**:
- 准确率中等 (~80-85%)
- 需要标注数据fine-tuning

#### **选项B: LLM-based extraction (GPT-4/Claude)**
**优势**:
- 准确率高 (>90%)
- 零样本或少样本
- 关系提取能力强

**劣势**:
- API调用成本
- 延迟较高 (1-3s)

#### **推荐**: 
- **Hybrid approach**: 
  - spaCy快速粗提取 (实时)
  - LLM精细增强 (后台异步)
  - 用户可手动确认低置信度实体

### 7.3 向量搜索

#### **选项A: Qdrant (已有Mem0支持)**
**优势**:
- 高性能向量搜索
- 丰富的过滤能力
- 支持多租户

**劣势**:
- 额外服务依赖

#### **选项B: SQLite-VSS**
**优势**:
- 集成到现有SQLite
- 轻量级

**劣势**:
- 性能和功能不如专用向量DB

#### **推荐**: 
- 复用Mem0的Qdrant集成
- 或使用SQLite-VSS (简化部署)

---

## 八、成本效益分析

### 8.1 开发成本

**人力投入**: 
- 1名全职后端工程师 (知识图谱 + API)
- 1名全职AI工程师 (NER/RE模型)
- 0.5名DevOps (部署和监控)

**时间**: 18-23周 (~5-6个月)

**总成本**: ~$150K-200K (人力 + 基础设施)

### 8.2 预期收益

#### **定量收益**:
1. **减少重复工作**: 
   - Agent间减少30-50%的重复问题
   - 节省开发者时间: ~5-10 hrs/week/developer
   
2. **加速上下文切换**:
   - 从"这个bug之前讨论过吗"到找到答案: 5分钟 → 30秒
   - 年节省: ~50 hrs/developer

3. **降低错误率**:
   - 架构决策可追溯，减少"为什么当时这么做"的困惑
   - 估计减少20%的架构返工

#### **定性收益**:
- **知识沉淀**: 离职员工的知识不流失
- **新人onboarding**: 快速理解项目历史和决策
- **跨团队协作**: Frontend/Backend/DevOps无缝对接
- **决策透明**: 所有架构决策有据可查

#### **ROI**:
假设10人团队:
- 年节省时间: 10 * (5 hrs/week * 50周) = 2500 hrs
- 按$100/hr计: **$250K/年**
- ROI: **$250K / $200K = 125%**

---

## 九、风险和缓解

### 风险1: 知识图谱维护成本高
**描述**: 图谱随时间增长，质量下降（重复实体、错误关系）

**缓解**:
- 实体消歧算法 (entity resolution)
- 定期图谱清理cron job
- 用户反馈修正机制
- 置信度阈值过滤

### 风险2: Agent角色划分不清晰
**描述**: 实际业务中角色overlap，配置复杂

**缓解**:
- 提供灵活的角色组合配置
- 默认profile作为模板，可自定义
- 动态调整过滤规则
- 用户反馈迭代优化

### 风险3: 性能瓶颈
**描述**: 大规模团队和项目时查询变慢

**缓解**:
- 分片策略 (按项目/时间分片)
- 图数据库索引优化
- 热点数据缓存
- 异步预热常见查询

### 风险4: 隐私和权限控制
**描述**: 敏感信息泄露到不该看的Agent

**缓解**:
- 细粒度ACL (实体级权限)
- 敏感信息自动检测和脱敏
- 审计日志记录所有访问
- 定期权限审查

---

## 十、总结和下一步

### 核心价值主张

**为芯相业务定制的记忆机制解决了三大痛点**:

1. **多Agent协作混乱** → 共享知识图谱 + 角色专业化
2. **知识孤岛严重** → 四层记忆 + 跨模态关联
3. **上下文丢失** → 完整时间线 + 决策可追溯

### 与通用Hermes的差异

| 维度 | 通用Hermes | 芯相定制版 |
|------|------------|------------|
| 设计目标 | 单用户↔️单Agent | 多Agent团队协作 |
| 记忆结构 | 扁平文本 | 四层+知识图谱 |
| 知识共享 | 无 | 跨Agent共享+通知 |
| 角色专业化 | 无 | 基于profile过滤 |
| 数据关联 | 文件路径 | 深度关联(代码+对话+数据) |
| 查询能力 | FTS5关键词 | 图推理+语义搜索 |

### 下一步行动

**立即行动** (本周):
1. [ ] 与团队review本方案，确认需求对齐
2. [ ] 技术选型决策 (Neo4j vs SQLite+AGE)
3. [ ] 创建GitHub project tracking board
4. [ ] 启动Phase 1 Week 1-2任务

**短期目标** (1个月):
- [ ] 完成知识图谱基础设施
- [ ] 实现前端Agent profile demo
- [ ] 可演示简单的跨Agent查询

**中期目标** (3个月):
- [ ] 四层记忆架构完全可用
- [ ] 5个核心工具集成到生产
- [ ] 至少2个真实项目pilot测试

**长期愿景** (6-12个月):
- [ ] 成为芯相团队的"组织知识大脑"
- [ ] 开源核心组件回馈Hermes社区
- [ ] 探索记忆增强推理 (Memory-Augmented Reasoning)
- [ ] 跨公司知识联邦 (如适用)

---

**文档版本**: v1.0  
**最后更新**: 2026-08-02  
**作者**: Claude Fable 5 for 芯相团队  
**状态**: 待Review
