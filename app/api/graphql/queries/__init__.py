import strawberry
from strawberry.types import Info

from app.schemas.ai import AiQuery
from app.api.graphql.queries.table import TableQueries
from app.api.graphql.queries.skill import SkillQueries
from app.api.graphql.queries.task_split import TaskSplitQueries
from app.api.graphql.queries.workspace import WorkspaceQueries
from app.api.graphql.queries.workspace_experience import WorkspaceExperienceQueries
from app.api.graphql.queries.pm_agent import PMAgentQueries
from app.api.graphql.queries.dashboard import DashboardQueries
from app.api.graphql.queries.search import SearchQueries
from app.api.graphql.queries.workload_planning import WorkloadPlanningQueries
from app.api.graphql.queries.project_steward import ProjectStewardQueries
from app.api.graphql.queries.member_assignment import MemberAssignmentQueries
from app.api.graphql.queries.ai_action_plan import AiActionPlanQueries
from app.api.graphql.queries.ai_visual_design import AiVisualDesignQueries
from app.api.graphql.queries.source_inbox import SourceInboxQueries
from app.api.graphql.queries.task_duplicates import TaskDuplicateQueries
from app.api.graphql.queries.task_profile import TaskProfileQueries
from app.api.graphql.queries.my_work import MyWorkQueries
from app.api.graphql.queries.board import BoardQueries
from app.api.graphql.queries.collaboration import CollaborationQueries
from app.api.graphql.queries.automation import AutomationQueries


@strawberry.type
class Query(AiQuery, TableQueries, WorkspaceQueries, WorkspaceExperienceQueries, DashboardQueries, SearchQueries, WorkloadPlanningQueries, MemberAssignmentQueries, ProjectStewardQueries, AiActionPlanQueries, AiVisualDesignQueries, SourceInboxQueries, TaskDuplicateQueries, TaskProfileQueries, MyWorkQueries, BoardQueries, CollaborationQueries, AutomationQueries, SkillQueries, TaskSplitQueries, PMAgentQueries):
    """Main Query class combining product query modules."""

    @strawberry.field
    def hello(self) -> str:
        return "Hello QTable!"
