import strawberry
from app.schemas.ai import AiMutation
from app.api.graphql.mutations.skill import SkillMutations
from app.api.graphql.mutations.table import TableMutations
from app.api.graphql.mutations.task_split import TaskSplitMutations
from app.api.graphql.mutations.workspace import WorkspaceMutations
from app.api.graphql.mutations.workspace_experience import WorkspaceExperienceMutations
from app.api.graphql.mutations.pm_agent import PMAgentMutations
from app.api.graphql.mutations.dashboard import DashboardMutations
from app.api.graphql.mutations.template import TemplateMutations
from app.api.graphql.mutations.workspace_blueprint import WorkspaceBlueprintMutations
from app.api.graphql.mutations.workload_planning import WorkloadPlanningMutations
from app.api.graphql.mutations.project_steward import ProjectStewardMutations
from app.api.graphql.mutations.member_assignment import MemberAssignmentMutations
from app.api.graphql.mutations.ai_action_plan import AiActionPlanMutations
from app.api.graphql.mutations.ai_visual_design import AiVisualDesignMutations
from app.api.graphql.mutations.source_inbox import SourceInboxMutations
from app.api.graphql.mutations.task_profile import TaskProfileMutations
from app.api.graphql.mutations.my_work import MyWorkMutations
from app.api.graphql.mutations.board import BoardMutations
from app.api.graphql.mutations.collaboration import CollaborationMutations
from app.api.graphql.mutations.automation import AutomationMutations


@strawberry.type
class Mutation(AiMutation, TableMutations, WorkspaceMutations, WorkspaceExperienceMutations, WorkspaceBlueprintMutations, WorkloadPlanningMutations, MemberAssignmentMutations, ProjectStewardMutations, AiActionPlanMutations, AiVisualDesignMutations, SourceInboxMutations, TaskProfileMutations, MyWorkMutations, BoardMutations, CollaborationMutations, AutomationMutations, DashboardMutations, TemplateMutations, SkillMutations, TaskSplitMutations, PMAgentMutations):
    """Main Mutation class combining product mutation modules."""
    pass
