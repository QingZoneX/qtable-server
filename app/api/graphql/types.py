from typing import List, Optional

import strawberry
from strawberry.scalars import JSON


@strawberry.type
class WorkspaceMemberInfo:
    user_id: int
    name: str
    email: str
    role: str


@strawberry.type
class WorkspaceItemAccessInfo:
    user_id: int
    name: str
    email: str
    role: str
    permission: str
    inherited: bool


@strawberry.input(name="RecordPatchInput")
class RecordPatchInput:
    record_id: strawberry.ID
    field_id: str
    value: Optional[JSON]


@strawberry.type
class SkillCategoryInfo:
    id: strawberry.ID
    slug: str
    name: str
    description: Optional[str] = None
    parentId: Optional[str] = None
    sortOrder: int = 0


@strawberry.type
class SkillPermissionRequirementInfo:
    resource: str
    action: str
    targetParam: Optional[str] = None
    optional: bool = False


@strawberry.type
class SkillMetadataInfo:
    name: str
    version: str
    title: str
    description: str
    tags: List[str]
    sideEffect: str
    confirmationRequired: bool
    idempotent: bool
    supportsDryRun: bool
    visibility: str
    permissions: List[SkillPermissionRequirementInfo]


@strawberry.type
class SkillRegistryEntryInfo:
    id: strawberry.ID
    workspaceId: Optional[str]
    category: Optional[SkillCategoryInfo]
    metadata: SkillMetadataInfo
    inputSchema: JSON
    outputSchema: JSON
    visibility: str
    sourceType: str
    runtimeKind: str
    entrypoint: Optional[str] = None
    modulePath: Optional[str] = None
    handlerName: Optional[str] = None
    icon: Optional[str] = None
    latestVersion: str
    status: str
    isEnabled: bool
    openaiToolSchema: Optional[JSON] = None
    mcpToolSchema: Optional[JSON] = None
    transportConfig: JSON
    cacheTtlSeconds: int
    embeddingStatus: str
    embeddingText: Optional[str] = None
    startedAt: Optional[str] = None
    stoppedAt: Optional[str] = None
    createdAt: Optional[str] = None
    updatedAt: Optional[str] = None


@strawberry.type
class SkillCallErrorInfo:
    code: str
    message: str
    retryable: bool
    details: Optional[JSON] = None


@strawberry.type
class SkillCallResultInfo:
    callId: strawberry.ID
    skillName: str
    state: str
    output: Optional[JSON] = None
    error: Optional[SkillCallErrorInfo] = None
    requiresConfirmation: bool = False
    metadata: JSON


@strawberry.input
class SkillCallInput:
    skillName: str
    input: JSON
    dryRun: bool = False
    confirmed: bool = False
    origin: str = "graphql"
    traceId: Optional[str] = None


@strawberry.input
class SkillRegistryInput:
    name: str
    version: str = "1.0.0"
    title: str
    description: str
    category: str = "general"
    namespace: Optional[str] = None
    workspaceId: Optional[str] = None
    tags: List[str]
    visibility: str = "internal"
    sourceType: str = "plugin"
    runtimeKind: str = "python_module"
    sideEffect: str = "none"
    confirmationRequired: bool = False
    idempotent: bool = True
    supportsDryRun: bool = True
    entrypoint: Optional[str] = None
    modulePath: Optional[str] = None
    handlerName: Optional[str] = None
    icon: Optional[str] = None
    inputSchema: JSON
    outputSchema: JSON
    permissions: List[JSON]
    transportConfig: JSON = strawberry.field(default_factory=dict)
    openaiToolSchema: Optional[JSON] = None
    mcpToolSchema: Optional[JSON] = None
    cacheTtlSeconds: int = 300
    embeddingText: Optional[str] = None
    changelog: Optional[str] = None


@strawberry.input
class SkillStatusInput:
    isEnabled: bool
    status: str = "active"


@strawberry.type
class TaskSplitResultInfo:
    traceId: str
    provider: str
    model: str
    attempts: int
    persisted: bool
    planId: Optional[str] = None
    result: JSON
    error: Optional[JSON] = None
    createdAt: Optional[str] = None


@strawberry.input
class TaskSplitRetryInput:
    maxAttempts: int = 2
    maxOutputRetries: int = 2
    maxToolRetries: int = 1
    backoffMs: int = 300


@strawberry.input
class TaskSplitRunInput:
    prompt: str
    model: Optional[str] = None
    autoCreateRecords: bool = True
    dryRun: bool = False
    maxDepth: int = 3
    maxChildrenPerNode: int = 8
    workspaceId: Optional[str] = None
    sessionId: Optional[str] = None
    conversationId: Optional[str] = None
    projectId: Optional[str] = None
    tableIds: List[str] = strawberry.field(default_factory=list)
    viewId: Optional[str] = None
    taskId: Optional[str] = None
    teamId: Optional[str] = None
    organizationId: Optional[str] = None
    workflowId: Optional[str] = None
    agentId: Optional[str] = None
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    retry: TaskSplitRetryInput = strawberry.field(default_factory=TaskSplitRetryInput)


@strawberry.type
class PMAgentPhaseInfo:
    phase: str
    status: str
    startedAt: Optional[str] = None
    completedAt: Optional[str] = None
    data: Optional[JSON] = None
    error: Optional[JSON] = None


@strawberry.type
class PMAgentStatusResponse:
    workflowId: strawberry.ID
    status: str
    pmPhase: Optional[str] = None
    phaseSequence: List[str] = strawberry.field(default_factory=list)
    completedPhases: int = 0
    totalPhases: int = 0
    phaseResults: List[PMAgentPhaseInfo] = strawberry.field(default_factory=list)
    requirements: List[JSON] = strawberry.field(default_factory=list)
    modules: List[JSON] = strawberry.field(default_factory=list)
    taskTree: List[JSON] = strawberry.field(default_factory=list)
    workloadEstimates: List[JSON] = strawberry.field(default_factory=list)
    memberAssignments: List[JSON] = strawberry.field(default_factory=list)
    milestones: List[JSON] = strawberry.field(default_factory=list)
    ganttTasks: List[JSON] = strawberry.field(default_factory=list)
    risks: List[JSON] = strawberry.field(default_factory=list)
    projectReport: Optional[JSON] = None
    finalResponse: Optional[str] = None
    error: Optional[JSON] = None
    createdAt: Optional[str] = None
    updatedAt: Optional[str] = None


@strawberry.type
class PMAgentListResponse:
    workflows: List[PMAgentStatusResponse]
    total: int
    offset: int = 0
    limit: int = 20


@strawberry.type
class PMAgentRunResponse:
    workflowId: str
    status: str
    message: str = ""


@strawberry.input
class PMAgentRunInput:
    message: str
    model: Optional[str] = None
    sessionId: Optional[str] = None
    conversationId: Optional[str] = None
    workspaceId: Optional[str] = None
    projectId: Optional[str] = None
    tableIds: List[str] = strawberry.field(default_factory=list)
    viewId: Optional[str] = None
    taskId: Optional[str] = None
    teamId: Optional[str] = None
    organizationId: Optional[str] = None
    workflowDefId: Optional[str] = None
    agentId: Optional[str] = None
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    streamingEnabled: bool = True
    autoApproveReadonly: bool = True
