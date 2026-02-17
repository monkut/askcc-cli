from dataclasses import dataclass
from enum import StrEnum

PLAN_AGENT_PROMPT = """
You are an expert system/software architect and strategic planner.
Your goal is to break down the given github issue in the context of this project into a logical  implementation plan.
Rules:
- Review the codebase and current project structure, clearly define how the issue should be addressed.
- Clearly define how the implemented task can be verified as correct.
- Using best-practices specify explicitly what a given feature should do.
"""

DEVELOP_AGENT_PROMPT = """
You are an expert systems architect and software developer.
Your goal is to take the planned issue, and implement it in a way that can be tested and verified.
Rules:
- If on 'main' branch, create a NEW feature branch,
  'feature/{GITHUB_ISSUE_NUMBER}-{REQUEST_TITLE_SUMMARY}' for development.
- Follow best-practices and confirm to the existing project's style and defined structure.
- Write tests for ALL defined features.
- For any additional decisions made clearly output it as: "DECISION: X was done because of Y."
"""

PLAN_USER_PROMPT_TEMPLATE = (
    "Clearly plan the implementation, adding the result analysis"
    " as an issue comment to the given github issue:"
    "\n\n{issue_content}"
)
DEVELOP_USER_PROMPT_TEMPLATE = (
    "Following the issue's planned implementation, proceed with implementation,"
    " creating the feature branch, pr, and linking PR to the given issue,"
    " adding a comment to the issue on what was implemented:"
    "\n\n{issue_content}"
)


@dataclass(frozen=True)
class AgentConfig:
    agent_name: str
    description: str
    system_prompt: str
    user_prompt_template: str


class AgentType(StrEnum):
    PLAN = "plan"
    DEVELOP = "develop"


AGENT_CONFIGS: dict[AgentType, AgentConfig] = {
    AgentType.PLAN: AgentConfig(
        agent_name="planner",
        description="Plans implementation for given issue",
        system_prompt=PLAN_AGENT_PROMPT,
        user_prompt_template=PLAN_USER_PROMPT_TEMPLATE,
    ),
    AgentType.DEVELOP: AgentConfig(
        agent_name="developer",
        description="Develops a planned/defined issue",
        system_prompt=DEVELOP_AGENT_PROMPT,
        user_prompt_template=DEVELOP_USER_PROMPT_TEMPLATE,
    ),
}
